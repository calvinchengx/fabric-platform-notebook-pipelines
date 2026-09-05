"""The client and the image must come from the same release.

WHY THIS IS A SCRIPT AND NOT A TEST. `versions.env` pins the emulator IMAGE and
the product pins the client WHEEL. Those live in two repositories, and nothing
in either one alone can see the pair.

`make verify` is where the pair exists: the platform has been pointed at a
specific product, and this runs before any step does. A test would have to
guess where the product is, and would pass by skipping when it guessed wrong --
which is not a pass.

BOTH FILES ARE CHECKED, and the lockfile is the one that decides. Every step
runs `uv run --frozen`, and --frozen resolves from uv.lock without reading
pyproject.toml at all: a bump that moves the declaration but not the lock
leaves the pin pointing one way and the installed client pointing the other.

ALL FOUR WHEELS, not just the client. This product installs fabric_target, the
notebookutils shim and both contoso_fixtures packages from the same release,
and each carries the release version in its FILENAME as well as in its tag.
They move together or the run is comparing a mixture.

STDLIB ONLY, deliberately. A guard that needs an environment cannot run before
that environment is built, and this one has to run first.

WHAT IT COST TO NOT HAVE THIS. When the check was added the platform pinned
v0.35.0 and the product installed v0.29.0, and five consecutive acceptance
nightlies had run GREEN across that gap. The suites do not exercise the
difference, so a green here never established that the pair agreed -- it only
established that nothing had asked.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The two facts that differ per platform. Everything below is shared.
VERSION_KEY = "FABRIC_EMULATOR_VERSION"
EMULATOR = "fabric-emulator"


def pins(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_product_pin.py <product-path>", file=sys.stderr)
        return 2
    product = pathlib.Path(sys.argv[1]).resolve()
    versions = pins((ROOT / "versions.env").read_text(encoding="utf-8"))
    if VERSION_KEY not in versions:
        print(
            f"versions.env has no {VERSION_KEY}. This platform cannot say which "
            f"image it pins, so it cannot check the client against it.",
            file=sys.stderr,
        )
        return 1
    version = versions[VERSION_KEY]
    expected = f"{EMULATOR}/releases/download/v{version}/"

    failures = []
    for name in ("pyproject.toml", "uv.lock"):
        path = product / name
        if not path.is_file():
            failures.append(f"{product.name}/{name} does not exist")
            continue
        text = path.read_text(encoding="utf-8")
        urls = [
            line.strip()
            for line in text.splitlines()
            if f"{EMULATOR}/releases/download/" in line
        ]
        if not urls:
            failures.append(
                f"{product.name}/{name} installs nothing from a {EMULATOR} "
                f"release -- this platform pins the image at v{version} and "
                f"cannot tell which client the product will use"
            )
        elif any(expected not in u for u in urls):
            failures.append(
                f"{product.name}/{name} names a release other than the pinned "
                f"v{version}:\n    " + "\n    ".join(urls)
            )

    if failures:
        print(
            "THE CLIENT AND THE IMAGE DISAGREE.\n\n"
            + "\n\n".join(failures)
            + f"\n\nFix the product's {EMULATOR} URLs to v{version} and run "
            f"`uv lock` there -- the lockfile is what --frozen installs.",
            file=sys.stderr,
        )
        return 1
    print(f"platform: {product.name} installs from {EMULATOR} v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
