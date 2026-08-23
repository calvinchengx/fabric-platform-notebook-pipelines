"""Point this repository at a specific fabric-emulator release.

WHY THIS EXISTS. The acceptance run is triggered by the emulator's release
workflow, and the whole claim being made is "the release that just shipped
carries a working platform". A run that fired on 0.13.1 but verified the 0.13.0
in `versions.env` would be worse than no run at all: it reports success for a
release nobody tested, and reports it in the emulator's own release history.

THREE VERSIONS MOVE, not one. `sail` and `spark-agent` are built by the same
release workflow with `type=semver,pattern={{version}}`, so they carry the
emulator's tag. Sail is the Spark engine, which decides how bronze and silver
behave; spark-agent is what the emulator drives to run a notebook. Leaving
either pinned while moving the emulator would verify a new emulator against an
old engine and call that a release test.

Rewrites in place rather than exporting environment variables, because compose
reads `versions.env` via `--env-file` and `release_info` reads the same file.
One file changes, and every reader — Python, compose, the summary — agrees on
what was tested without any of them being told separately.
"""

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.env"

# The keys the emulator's release tags in lockstep. Anything not listed here
# ships on its own cadence and must NOT be moved by a fabric-emulator release.
TRACKS_THE_RELEASE = ("FABRIC_EMULATOR_VERSION", "SAIL_VERSION", "SPARK_AGENT_VERSION")

# THE DIGEST MOVES WITH THE TAG, or the pin is worse than no pin at all. Docker
# ignores the tag in `repo:tag@sha256:...` and fetches the digest, so a run that
# bumped the versions and left the digests behind would pull the PREVIOUS images
# while the summary named the new release -- the same "verified a release nobody
# tested" failure this script was written to prevent, one level down.
#
# var prefix -> the image whose tag that prefix's _VERSION supplies.
PINS = {
    "FABRIC_EMULATOR": "ghcr.io/calvinchengx/fabric-emulator",
    "SAIL": "ghcr.io/calvinchengx/emulator-sail",
    "SPARK_AGENT": "ghcr.io/calvinchengx/emulator-spark-agent",
}


def digest_of(image: str, tag: str) -> str:
    """Ask the registry what this tag points at RIGHT NOW.

    The INDEX digest, which is what `imagetools inspect` reports for a
    multi-arch tag. Pinning one platform's manifest instead would produce a
    stack that runs on the CI runner and fails to pull on a developer's laptop.
    """
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", f"{image}:{tag}",
         "--format", "{{.Manifest.Digest}}"],
        capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip().startswith("sha256:"):
        raise SystemExit(f"cannot read digest for {image}:{tag}: "
                         f"{(out.stderr or out.stdout).strip()[:200]}")
    return out.stdout.strip()


def set_digests(text: str, version: str) -> dict[str, tuple[str, str]]:
    """Rewrite every _DIGEST to what its tag resolves to now."""
    moved = {}
    for prefix, image in PINS.items():
        digest = digest_of(image, version)
        found = re.search(rf"^{prefix}_DIGEST=(.*)$", text, re.M)
        if not found:
            raise SystemExit(f"{prefix}_DIGEST not found in versions.env")
        moved[prefix] = (found.group(1).strip(), digest)
        text = re.sub(rf"^{prefix}_DIGEST=.*$", f"{prefix}_DIGEST={digest}",
                      text, flags=re.M)
    return text, moved

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


def set_version(text: str, version: str) -> tuple[str, dict[str, str]]:
    """Return the rewritten file and what each key moved from."""
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

    text = VERSIONS.read_text(encoding="utf-8")
    new, moved = set_version(text, version)

    missing = [k for k in TRACKS_THE_RELEASE if k not in moved]
    if missing:
        sys.exit(f"{VERSIONS.name} has no {', '.join(missing)} to set")

    # Digests BEFORE the write: resolving them can fail (a tag that does not
    # exist yet, a registry that will not answer), and failing after the file
    # has been rewritten would leave versions.env naming a release whose images
    # nobody confirmed are published.
    new, digests = set_digests(new, version)

    VERSIONS.write_text(new, encoding="utf-8")
    for key, old in moved.items():
        note = "  (unchanged)" if old == version else ""
        print(f"  {key}: {old} -> {version}{note}")
    for prefix, (before, after) in digests.items():
        note = "  (unchanged)" if before == after else ""
        print(f"  {prefix}_DIGEST: {before[:19]}… -> {after[:19]}…{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
