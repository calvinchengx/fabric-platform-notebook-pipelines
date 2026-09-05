"""Run docker compose with the pinned release in the environment.

The pin lives in .emulator-version. Reading it from the Makefile would need
$(shell cat ...) — which is not a thing on cmd.exe, where GNU Make on Windows
runs its recipes. So the Makefile stays a one-liner and the logic lives here,
where `pathlib.read_text(encoding="utf-8")` means the same on all three platforms.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request

import release_info as rel

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = [
    "compose/docker-compose.yml",
    "compose/governance.yml",
]
BUILD = ROOT / "compose" / ".generated"

# TERMINAL=1 films the run inside the portal's own terminal pane rather than
# beside a separately launched ttyd. Opt-in because it points the emulator at a
# shell: the overlay sets FABRIC_TERMINAL_URL, and without it the emulator does
# not mount the terminal routes at all.
if os.environ.get("TERMINAL") == "1":
    FILES.append("compose/terminal.yml")


WHEELS = ROOT / ".wheels"


def sources_dir() -> pathlib.Path:
    """The contoso-sources checkout this stack pulls its vendors from.

    A SIBLING PATH, and the one place in this repository where that is right:
    the vendors are not a dependency of this platform, they are the world
    outside it, mounted into containers as bytes rather than imported as code.
    Overridable, because pointing this at real vendors is what production does.
    """
    return pathlib.Path(
        os.environ.get("SOURCES", ROOT.parent / "contoso-sources")
    ).resolve()


def vendor_fragment() -> pathlib.Path:
    """Generate the vendor compose fragment from the sources declaration.

    Generated rather than checked in, so this repository cannot hold a stale
    copy of another repository's vendor list -- which is exactly what it did
    hold until now: eight tracked definition files, byte-identical to
    contoso-sources', agreeing by accident of history rather than by structure.
    """
    src = sources_dir()
    decl = src / "sources.yaml"
    if not decl.exists():
        sys.exit(
            f"no vendor declaration at {decl}.\n\n"
            f"This platform pulls from the vendors contoso-sources declares --\n"
            f"the same ones every other cell pulls from, which is what makes\n"
            f"their gold numbers comparable. Clone it beside this repository,\n"
            f"or set SOURCES=/path/to/contoso-sources."
        )
    # The BYTES, not just the declaration. Without `make sources` over there the
    # vendors still START -- mokapi falls back to generating bodies from the
    # OpenAPI schema -- and every step would land invented data that looks
    # entirely plausible until the numbers are compared.
    data = src / "_data"
    if not data.is_dir() or not any(data.iterdir()):
        sys.exit(
            f"{data} is empty -- the vendors have no bytes to serve.\n\n"
            f"Run `make sources` in {src} first."
        )
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / "sources.json"
    out.write_text(
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "sources.py"), str(decl), str(src)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout,
        encoding="utf-8",
    )
    return out


def stage_product_wheel() -> None:
    """Put the data product where the Spark agent will install it.

    bronze and silver import `contoso_product`, and they run on the agent, not
    in this process. The notebooks declare a Fabric Environment, which is what
    real Fabric acts on; the emulator resolves that binding for a notebook run
    but does not apply it, so the agent needs the wheel by its documented
    `/opt/wheels` fallback instead.

    THE VERSION IS THE PRODUCT'S, ASKED OF THE PRODUCT. This used to read
    `version("contoso-data-product")` from THIS process, meaning the platform's
    own virtualenv -- and the docstring above it claimed that stopped the engine
    and the client diverging. It did the opposite. The client is the product's
    steps, which run in the PRODUCT's virtualenv (`uv run --directory
    $(PRODUCT)`), and this platform pinned its own copy of the product at 0.3.0
    while the leaf had moved to 0.6.0. So the agent installed 0.3.0 and every
    notebook ran three releases behind the process driving it -- exactly "a
    difference no test would catch and every number would hide", written by the
    line that was supposed to prevent it.

    Measured, not reasoned: the agent logged `installed from /opt/wheels:
    contoso_data_product-0.3.0-py3-none-any.whl` while the leaf's venv answered
    0.6.0, and the bronze notebook never reached a terminal state.
    """
    product = os.environ.get("PRODUCT")
    if not product:
        # `make up` without PRODUCT: the empty mount point. Nothing to stage.
        print("PRODUCT is not set; skipping the wheel stage")
        return
    probe = subprocess.run(
        [
            "uv",
            "run",
            "--directory",
            product,
            "--frozen",
            "--no-sync",
            "python",
            "-c",
            "import importlib.metadata as m;print(m.version('contoso-data-product'))",
        ],
        capture_output=True,
        text=True,
    )
    v = probe.stdout.strip()
    if probe.returncode != 0 or not v:
        # The product has no environment yet (`make up` before its `uv sync`).
        # The agent starts without the product and the bronze step fails naming
        # it, which is better than a silent skip -- or than staging whatever
        # version this repository happens to carry.
        print(
            f"could not read contoso-data-product from {product}; "
            "skipping the wheel stage"
        )
        return

    name = f"contoso_data_product-{v}-py3-none-any.whl"
    url = (
        "https://github.com/calvinchengx/contoso-data-product/releases/download/"
        f"v{v}/{name}"
    )
    WHEELS.mkdir(exist_ok=True)
    # Anything else is a wheel for a version we are no longer on. Left behind,
    # the agent would install both and the newer one would not reliably win.
    for stale in WHEELS.glob("contoso_data_product-*.whl"):
        if stale.name != name:
            stale.unlink()
    dest = WHEELS / name
    if dest.is_file():
        return
    print(f"staging {name} for the Spark agent")
    with urllib.request.urlopen(url) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: compose.py <up|down|config|ps> [args...]")
    args = sys.argv[1:]
    if args and args[0] == "up":
        stage_product_wheel()
    env = dict(os.environ)
    # The governance profile is on by default. It is the heaviest part of the
    # stack — OpenSearch alone wants a 1 GB heap — but a catalog that only a
    # separate command exercises is one nobody hears about when it breaks.
    cmd = [
        "docker",
        "compose",
        "--env-file",
        rel.VERSIONS.name,
        "--profile",
        "governance",
    ]
    for f in FILES:
        cmd += ["-f", f]
    # The vendors come last, generated from contoso-sources at every invocation
    # so a vendor added over there is stood up here without an edit.
    cmd += ["-f", str(vendor_fragment().relative_to(ROOT))]
    cmd += args
    print("$", " ".join(cmd), f"   (fabric-emulator {rel.version()})")
    rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    if args and args[0] == "up" and rc != 0:
        dump_failure(cmd[:-len(args)], env)
    return rc


def dump_failure(base: list[str], env: dict) -> None:
    """What the containers said on the way down.

    `compose up` resolves depends_on itself and reports only `dependency
    failed to start` -- WHICH container, and nothing about why it exited. That
    is how G48, a released emulator that did not boot in a sibling stack,
    survived a release and three CI runs without a single line of diagnosis.
    The logs exist at this moment and are gone as soon as anyone runs
    `make down`, which CI does in its cleanup step.

    `ps -a` first because it names which container died and with what code; the
    logs then say what it said on the way out. Both are bounded (`--tail`) so a
    noisy stack cannot bury the failure they exist to explain.

    check=False throughout, and no return value: this runs on a path that is
    already failing, and a diagnostic that can raise would replace the failure
    it was called to explain.
    """
    print("platform: the stack did not come up. what the containers said:",
          flush=True)
    subprocess.run(base + ["ps", "-a"], cwd=ROOT, env=env, check=False)
    subprocess.run(base + ["logs", "--no-color", "--tail=80"],
                   cwd=ROOT, env=env, check=False)


if __name__ == "__main__":
    sys.exit(main())
