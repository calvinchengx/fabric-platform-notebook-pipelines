"""Point this repository at a specific fabric-emulator release.

WHY THIS EXISTS. The acceptance run is triggered by the emulator's release
workflow, and the whole claim being made is "the release that just shipped
carries a working platform". A run that fired on 0.13.1 but verified the 0.13.0
in `versions.env` would be worse than no run at all: it reports success for a
release nobody tested, and reports it in the emulator's own release history.

THREE IMAGES MOVE, not one. `sail` and `spark-agent` are rebuilt by the same
release workflow, but they are TAGGED for the dependency they carry, so what
moves for them is the digest and the release label rather than the tag. Sail is
the Spark engine, which decides how bronze and silver behave; spark-agent is
what the emulator drives to run a notebook. Leaving
either pinned while moving the emulator would verify a new emulator against an
old engine and call that a release test.

THEIR _VERSION MOVES WHEN THE RELEASE BUMPS THE DEPENDENCY. This script used to
hold it still, and v0.36.0 moved pysail 0.7.0 -> 0.7.1, leaving 0.7.0 written
beside the 0.36.0 digest. It is now read from fabric-emulator's pyproject.toml
at the release tag, the same pin that chose the image's tag.

Rewrites in place rather than exporting environment variables, because compose
reads `versions.env` via `--env-file` and `release_info` reads the same file.
One file changes, and every reader — Python, compose, the summary — agrees on
what was tested without any of them being told separately.
"""

import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.env"

# The keys the emulator's release tags in lockstep. Anything not listed here
# ships on its own cadence and must NOT be moved by a fabric-emulator release.
TRACKS_THE_RELEASE = ("FABRIC_EMULATOR_VERSION",)

# sail and spark-agent are ALSO rebuilt by every emulator release, but their
# tag names the dependency they carry, not the release -- so their digest and
# _RELEASE label follow the release, and their _VERSION follows the dependency
# pin the release shipped. Listing their _VERSION above would retag them onto
# the emulator's number and lose the one thing the tag is for: saying which
# Sail is inside.
#
# prefix -> the pin in fabric-emulator's pyproject.toml the image is tagged
# with. The same map as fabric-emulator's scripts/image_tags.py.
TAGGED_BY = {"SAIL_ENGINE": "pysail", "SPARK_CLIENT": "pyspark-client"}
CARRIES_A_DEPENDENCY_TAG = tuple(TAGGED_BY)

# A TAG, not a branch: what the release was built from, and it cannot move.
FABRIC_PYPROJECT = (
    "https://raw.githubusercontent.com/calvinchengx/"
    "fabric-emulator/v{release}/pyproject.toml"
)

# THE DIGEST MOVES WITH THE TAG, or the pin is worse than no pin at all. Docker
# ignores the tag in `repo:tag@sha256:...` and fetches the digest, so a run that
# bumped the versions and left the digests behind would pull the PREVIOUS images
# while the summary named the new release -- the same "verified a release nobody
# tested" failure this script was written to prevent, one level down.
#
# var prefix -> the image whose tag that prefix's _VERSION supplies.
PINS = {
    "FABRIC_EMULATOR": "ghcr.io/calvinchengx/fabric-emulator",
    "SAIL_ENGINE": "ghcr.io/calvinchengx/emulator-sail",
    "SPARK_CLIENT": "ghcr.io/calvinchengx/emulator-spark-agent",
}


def digest_of(image: str, tag: str) -> str:
    """Ask the registry what this tag points at RIGHT NOW.

    The INDEX digest, which is what `imagetools inspect` reports for a
    multi-arch tag. Pinning one platform's manifest instead would produce a
    stack that runs on the CI runner and fails to pull on a developer's laptop.
    """
    out = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            f"{image}:{tag}",
            "--format",
            "{{.Manifest.Digest}}",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip().startswith("sha256:"):
        raise SystemExit(
            f"cannot read digest for {image}:{tag}: "
            f"{(out.stderr or out.stdout).strip()[:200]}"
        )
    return out.stdout.strip()


def set_digests(text: str, version: str) -> tuple[str, dict[str, tuple[str, str]]]:
    """Rewrite every _DIGEST to what its tag resolves to now."""
    moved = {}
    for prefix, image in PINS.items():
        digest = digest_of(image, version)
        found = re.search(rf"^{prefix}_DIGEST=(.*)$", text, re.M)
        if not found:
            raise SystemExit(f"{prefix}_DIGEST not found in versions.env")
        moved[prefix] = (found.group(1).strip(), digest)
        text = re.sub(
            rf"^{prefix}_DIGEST=.*$", f"{prefix}_DIGEST={digest}", text, flags=re.M
        )
        # Resolved by the RELEASE tag above, so by construction this build is
        # the one that release published -- record which, since the tag these
        # two are pinned by cannot say it. CI re-reads the label off the digest
        # and fails if this disagrees, so the claim is checked, not trusted.
        if prefix in CARRIES_A_DEPENDENCY_TAG:
            if not re.search(rf"^{prefix}_RELEASE=", text, re.M):
                raise SystemExit(f"{prefix}_RELEASE not found in versions.env")
            text = re.sub(
                rf"^{prefix}_RELEASE=.*$",
                f"{prefix}_RELEASE={version}",
                text,
                flags=re.M,
            )
    return text, moved


SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def carried_versions(release: str) -> dict[str, str]:
    """The dependency version each sidecar carries in `release`.

    Read with image_tags.py's own rule: exactly one `==` pin per package.
    """
    url = FABRIC_PYPROJECT.format(release=release)
    try:
        text = fetch(url)
    except OSError as err:
        raise SystemExit(f"cannot read {url}: {err}") from None
    carried = {}
    for prefix, package in TAGGED_BY.items():
        found = set(re.findall(rf'"{re.escape(package)}==([0-9][^"]*)"', text))
        if len(found) != 1:
            raise SystemExit(
                f"v{release} pins {package} as {sorted(found) or 'nothing'}; "
                f"expected exactly one == version"
            )
        carried[prefix] = found.pop()
    return carried


def set_version(
    text: str, version: str, carried: dict[str, str] | None = None
) -> tuple[str, dict[str, str]]:
    """Return the rewritten file and what each key moved from.

    `carried` maps a sidecar prefix to the dependency version the release
    ships; its _VERSION moves to that, never to `version`.
    """
    carried = carried or {}
    moved = {}
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, old = stripped.partition("=")
        key, old = key.strip(), old.strip()
        if key in TRACKS_THE_RELEASE:
            moved[key] = old
            lines[i] = f"{key}={version}\n"
        elif key.endswith("_VERSION") and key[: -len("_VERSION")] in carried:
            moved[key] = old
            lines[i] = f"{key}={carried[key[: -len('_VERSION')]]}\n"
    return "".join(lines), moved


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: set_release.py <version>   e.g. set_release.py 0.13.1")
    version = sys.argv[1].lstrip("v")

    # A dispatch that arrives with an empty or malformed payload would
    # otherwise write `FABRIC_EMULATOR_VERSION=` and fail four steps later, as
    # an image pull error that names neither this script nor the payload.
    if not SEMVER.match(version):
        sys.exit(f"not a version: {version!r} — expected something like 0.13.1")

    carried = carried_versions(version)
    text = VERSIONS.read_text(encoding="utf-8")
    new, moved = set_version(text, version, carried)

    missing = [k for k in TRACKS_THE_RELEASE if k not in moved]
    missing += [f"{p}_VERSION" for p in carried if f"{p}_VERSION" not in moved]
    if missing:
        sys.exit(f"{VERSIONS.name} has no {', '.join(missing)} to set")

    # Digests BEFORE the write: resolving them can fail (a tag that does not
    # exist yet, a registry that will not answer), and failing after the file
    # has been rewritten would leave versions.env naming a release whose images
    # nobody confirmed are published.
    new, digests = set_digests(new, version)

    VERSIONS.write_text(new, encoding="utf-8")
    for key, old in moved.items():
        now = carried.get(key.removesuffix("_VERSION"), version)
        note = "  (unchanged)" if old == now else ""
        print(f"  {key}: {old} -> {now}{note}")
    for prefix, (before, after) in digests.items():
        note = "  (unchanged)" if before == after else ""
        print(f"  {prefix}_DIGEST: {before[:19]}… -> {after[:19]}…{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
