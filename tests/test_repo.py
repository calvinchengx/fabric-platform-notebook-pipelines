"""Invariants this repository must hold on Windows, macOS and Linux.

None of these need the emulator, Docker, or the fixture wheels — they are about
the repository itself, so they are the part of CI that is green from day one and
runs identically on all three platforms.
"""

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")


def _pins():
    out = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_every_image_is_pinned_to_a_version():
    """All THREE emulator images.

    The family ships on independent cadences, so fabric-emulator, entra and
    keyvault sit on different version lines and one pin cannot describe the
    stack. Assuming it could is how this repo first failed to start: `manifest
    unknown`, because 0.13.0 existed for one image and not the others.

    MOKAPI IS NO LONGER HERE, and that is the point rather than an omission.
    The simulator is part of what "the vendor" means, so it is pinned by
    `contoso-sources` alongside the specs it serves — two consumers on
    different mokapis are not pulling from the same vendor even if the specs
    match. `scripts/sources.py` reads that repo's versions.env and refuses to
    guess a version it does not find there.
    """
    pins = _pins()
    expected = {
        "FABRIC_EMULATOR_VERSION",
        "ENTRA_EMULATOR_VERSION",
        "KEYVAULT_EMULATOR_VERSION",
    }
    assert expected <= set(pins), expected - set(pins)
    # The invariant is IMMUTABLE, not a particular shape. Upstream projects
    # version how they like — postgres `16.4`, redpanda `v24.2.7`, debezium
    # `2.7.3.Final` — and demanding X.Y.Z of all of them would say nothing
    # about reproducibility while rejecting perfectly good pins.
    mutable = {"latest", "stable", "main", "edge", "nightly", "dev", "alpha"}
    for k, v in pins.items():
        assert v, f"{k} is empty"
        assert v.lower() not in mutable, f"{k}={v} is a moving tag"
        assert any(c.isdigit() for c in v), f"{k}={v} names no version"


def test_compose_reads_every_pin():
    """A pin nothing substitutes is a comment. Each variable must appear in a
    compose file, or the image silently falls back to whatever is there."""
    composed = "".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "compose").glob("*.yml")
    )
    for k in _pins():
        assert "${" + k in composed, f"{k} is pinned but never used"


def test_compose_never_uses_latest():
    # `latest` would make a green run unattributable: something worked, but you
    # could not say which release.
    text = (ROOT / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    assert ":latest" not in text
    assert "${FABRIC_EMULATOR_VERSION" in text


def test_every_make_recipe_survives_cmd_exe():
    """The cross-platform claim, enforced.

    GNU Make on Windows runs recipes through cmd.exe. A recipe using a pipe, a
    shell builtin, `rm`, backticks or `&&` works on two platforms and fails on
    the third — and it fails for the user, not for us, which is the wrong place
    to find out. Logic belongs in scripts/, which is Python.
    """
    banned = re.compile(
        r"(\|\||&&|\|(?!\|)|`|\brm\b|\bcp\b|\bmv\b|\bcat\b|"
        r"\bsed\b|\btest\b\s+-|\bif\b\s|\bfor\b\s|\$\(shell)"
    )
    offenders = []
    for line in MAKEFILE.splitlines():
        if not line.startswith("\t"):
            continue
        recipe = line.lstrip("\t").lstrip("@")
        if banned.search(recipe):
            offenders.append(recipe)
    assert not offenders, f"these recipes would not run on cmd.exe: {offenders}"


def test_make_targets_are_documented():
    # `make help` is generated from these, so an undocumented target is an
    # invisible one.
    declared = set(re.findall(r"^\.PHONY:\s*(.+)$", MAKEFILE, re.M)[0].split())
    documented = set(re.findall(r"^([a-z][a-z0-9-]*):.*?##", MAKEFILE, re.M))
    assert declared == documented, declared ^ documented


def test_the_emulator_client_plumbing_is_never_imported():
    """This repo must build against the emulator like any consumer would.

    `common.py` ships inside contoso-fixtures — it is the in-tree examples'
    client plumbing (endpoints, token minting, tds_connect). Importing it here
    would hand this repository the answer key and quietly void the one claim it
    exists to make: that the published emulator is usable by someone who does
    not have its source.
    """
    offenders = []
    for p in ROOT.rglob("*.py"):
        if ".venv" in p.parts or p.name == "test_repo.py":
            continue
        src = p.read_text(encoding="utf-8")
        if re.search(r"^\s*(from common import|import common\b)", src, re.M):
            offenders.append(p.relative_to(ROOT).as_posix())
    assert not offenders, f"must not import the emulator's own plumbing: {offenders}"


def test_python_is_only_ever_invoked_through_uv():
    """uv, strictly.

    A bare `python` or `pip` in a recipe or workflow resolves to whatever the
    machine happens to have — a different interpreter on Windows than on the
    Linux runner, and a different one again on a contributor's Mac. The whole
    point of committing uv.lock is that those are the same.
    """
    bad = []
    files = [ROOT / "Makefile", *sorted((ROOT / ".github/workflows").glob("*.yml"))]
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip().lstrip("@- ")
            if stripped.startswith("#") or "python-version" in stripped:
                continue
            if re.match(r"^(run:\s*)?(python3?|pip3?)\s", stripped):
                bad.append(f"{f.name}:{i}: {stripped}")
    assert not bad, f"invoke through uv instead: {bad}"


def test_the_lockfile_is_committed():
    """Without it, `--frozen` has nothing to be frozen to and three platforms
    resolve three different dependency sets."""
    assert (ROOT / "uv.lock").exists()
    assert "pytest" in (ROOT / "uv.lock").read_text(encoding="utf-8"), (
        "dev group not locked"
    )


def test_every_rule_names_a_test_that_exists():
    """RULES.md is the codebase's rules. A rule citing a test that does not
    exist is prose asserting a guarantee nothing enforces — the failure this
    whole platform is built to catch, turned on our own documentation.

    `judgement` is an honest answer and is allowed. A wrong test name is not.
    """
    rules = (ROOT / "RULES.md").read_text(encoding="utf-8")
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", rules))
    assert cited, "RULES.md cites no tests at all"

    defined = set()
    for p in (ROOT / "tests").glob("test_*.py"):
        defined |= set(
            re.findall(r"^def (test_[a-z0-9_]+)", p.read_text(encoding="utf-8"), re.M)
        )

    missing = sorted(cited - defined)
    assert not missing, f"RULES.md cites tests that do not exist: {missing}"


def test_set_release_moves_every_version_the_emulator_tags():
    """The emulator's release tags fabric-emulator, sail AND spark-agent.

    Sail is the Spark engine — bronze and silver run inside it. spark-agent is
    what the emulator drives to execute a notebook. Moving the emulator while
    leaving either pinned would verify a new release against an old engine and
    call the result a release test.

    NOTE this test cannot catch an allowlist that has fallen BEHIND; it derives
    its expectation from the allowlist. That is
    `test_every_emulator_family_image_tracks_the_release`'s job.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from set_release import TRACKS_THE_RELEASE, set_version

    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    new, moved = set_version(text, "9.9.9")
    assert set(moved) == set(TRACKS_THE_RELEASE), moved
    for key in TRACKS_THE_RELEASE:
        assert re.search(rf"^{key}=9\.9\.9$", new, re.M), key
    # Versions on independent cadences must NOT be dragged along.
    independent = (
        "ENTRA_EMULATOR_VERSION",
        "KEYVAULT_EMULATOR_VERSION",
    )
    for key in independent:
        b = re.search(rf"^{key}=(.+)$", text, re.M)
        a = re.search(rf"^{key}=(.+)$", new, re.M)
        assert b and a, f"{key} is missing from versions.env"
        assert b.group(1) == a.group(1), f"{key} moved: {b.group(1)} -> {a.group(1)}"


def test_every_emulator_family_image_tracks_the_release():
    """Check the allowlist against something OTHER than itself.

    `test_set_release_moves_every_version_the_emulator_tags` asserts
    `set(moved) == set(TRACKS_THE_RELEASE)` — it derives its expectation FROM
    the allowlist, so it passes whatever that tuple happens to say, including a
    tuple that has silently fallen behind. Add a fourth image published by the
    emulator's release workflow, forget the allowlist, and it stays pinned at
    the previous version while everything above stays green. That already
    nearly happened: spark-agent arrived and the allowlist named two things.

    Compose is the independent witness. Every `ghcr.io/calvinchengx/
    fabric-emulator*` image is tagged `{{version}}` by that one release
    workflow, so its version variable MUST move with the release. entra and
    keyvault are separate repositories on their own cadences, and the prefix
    is what tells them apart.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from set_release import TRACKS_THE_RELEASE

    composed = "".join(
        p.read_text(encoding="utf-8") for p in sorted((ROOT / "compose").glob("*.yml"))
    )
    # A SET of pairs, not a dict keyed by image. A dict lets the LAST file win,
    # so an overlay naming the same image erases the base pin from what this
    # guard witnesses — it would then be checking the overlay and reporting on
    # the release. Found when compose/terminal.yml pinned the family to
    # unreleased builds and the base FABRIC_EMULATOR_VERSION vanished from the
    # evidence entirely.
    pins = set(
        re.findall(
            r"image:\s*ghcr\.io/calvinchengx/(fabric-emulator[\w-]*)"
            r":\$\{([A-Z_]+)",
            composed,
        )
    )
    assert pins, (
        "no ghcr.io/calvinchengx/fabric-emulator* images found in compose — "
        "this guard is reading the wrong thing and would pass on anything"
    )
    # An overlay may deliberately pin an image to something the release does not
    # move — that is what running an unreleased build MEANS, and refusing it
    # would make the guard forbid the one job overlays exist for.
    #
    # It is exempt only while the image ALSO carries a tracked pin somewhere, so
    # the override can add a way to escape the release but never become the only
    # pin. Drop the base and this fails, which is the case worth catching: a
    # stack that has quietly stopped following releases at all.
    tracked = {img for img, var in pins if var in TRACKS_THE_RELEASE}
    missing = {
        img: var
        for img, var in pins
        if var not in TRACKS_THE_RELEASE
        and not (var.endswith("_OVERRIDE") and img in tracked)
    }
    assert not missing, (
        f"published by the emulator's release but NOT in TRACKS_THE_RELEASE: "
        f"{missing}. A release would move the emulator and leave these behind, "
        f"silently — add them to scripts/set_release.py. (A deliberate "
        f"unreleased pin must be named <VAR>_OVERRIDE and the image must keep "
        f"its tracked pin in the base compose.)"
    )


def test_set_release_refuses_a_payload_that_is_not_a_version():
    """An empty client_payload would otherwise write `VERSION=` and surface
    four steps later as an image-pull error naming neither the payload nor this
    script."""
    for bad in ("", "latest", "v", "0.13", "; rm -rf /"):
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "set_release.py"), bad],
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, f"accepted {bad!r}"


def test_the_acceptance_run_uses_the_dispatched_version():
    """A dispatch that triggers a run against the OLD pin is worse than no
    dispatch: it reports success for a release nobody tested."""
    wf = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    assert "repository_dispatch" in wf
    assert "client_payload.version" in wf, (
        "acceptance is triggered by a release but never reads which one"
    )
    assert "set_release.py" in wf


def test_the_pin_moves_only_after_a_green_verify():
    """Adoption is automatic, so the GATE is the whole safety argument.

    The acceptance run commits the verified version back to versions.env,
    which means a released emulator becomes the one this platform claims to
    support without a human in the loop. That is only sound while the commit
    is unreachable from a failed run: an `if: always()` here, or the step
    drifting above `make verify`, would adopt a version precisely when the
    evidence says not to — and it would do it silently, in the emulator's own
    release history.
    """
    import yaml

    wf = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    )
    job = wf["jobs"]["verify"]
    steps = job["steps"]

    def index_of(pred) -> int:
        hits = [i for i, s in enumerate(steps) if pred(s)]
        assert len(hits) == 1, f"expected exactly one matching step, got {hits}"
        return hits[0]

    # `startswith`, not equality: the step carries `PRODUCT=...` now that the
    # product is a separate repository, and an exact match would silently find
    # nothing -- which reads as "there is no verify step" rather than "the
    # matcher is stale". The `make verify` prefix is what this test is about.
    verify = index_of(lambda s: s.get("run", "").strip().startswith("make verify"))
    adopt = index_of(lambda s: "push origin" in s.get("run", ""))

    assert adopt > verify, "the pin is adopted before the run that verifies it"

    cond = str(steps[adopt].get("if", ""))
    assert "always()" not in cond, (
        "the adopt step runs even when verification failed; "
        "a red run must leave the pin where it is"
    )
    # THE SECOND HALF OF THE GATE: the pin must have actually moved.
    #
    # This used to assert `repository_dispatch` in the condition, on the reasoning
    # that the schedule re-verified the EXISTING pin and so had nothing to adopt.
    # That reasoning stopped being true when the schedule started resolving the
    # NEWEST release, so that a missing or under-scoped dispatch token costs a day
    # of latency rather than silence. A scheduled run that verified a newer
    # release end to end has proved exactly what a dispatched one proves.
    #
    # What was load-bearing in that assertion was never the event: it was that a
    # run with nothing new must not commit. That is now explicit, in the step, and
    # asserted here directly rather than through a proxy for it.
    run = steps[adopt].get("run", "")
    assert "git diff --quiet versions.env" in run, (
        "the adopt step does not check whether the pin actually moved, so a run "
        "with nothing new would commit anyway"
    )

    # Writing to the repository is not the default and must be asked for
    # explicitly, or the push fails at the end of an eight-minute run.
    assert job.get("permissions", {}).get("contents") == "write"


def _steps(workflow: str, job: str):
    import yaml

    wf = yaml.safe_load(
        (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    )
    return wf["jobs"][job]["steps"]


def _first_running(steps, prefix: str) -> int:
    hits = [
        i for i, s in enumerate(steps) if s.get("run", "").strip().startswith(prefix)
    ]
    assert hits, f"no step runs {prefix!r}"
    return hits[0]


def test_the_vendors_are_materialised_before_anything_reads_them():
    """`make sources` first, or `make doctor` fails on an empty `_data/`.

    While this platform carried its own copy of the vendors, a checkout was
    already populated and the order of these two steps did not matter. Since
    it stopped, `_data/` is gitignored inside contoso-sources and a fresh
    checkout of that repo is empty until `make sources` delegates over there.
    doctor checks the vendors are materialised and exits non-zero when they
    are not, so it standing above the step that materialises them fails every
    run before the emulator is so much as pulled.
    """
    steps = _steps("acceptance.yml", "verify")
    sources = _first_running(steps, "make sources")
    assert sources < _first_running(steps, "make doctor")
    assert sources < _first_running(steps, "make up")


def test_attribute_stands_up_the_same_platform_acceptance_does():
    """Attribution is only worth anything if the two suites are identical.

    This workflow runs ONLY when Acceptance is already red, so it is invisible
    until the moment it is load-bearing -- and that is exactly where it went
    stale. This repo became a platform that mounts contoso-sources' vendors
    and runs a product out of a third repository; a lone checkout of this one
    cannot start anything, and `make verify` without PRODUCT points at the
    empty ./product mount and attributes nothing. A verdict of "it fails on
    N-1 too, so the fault is ours" is worse than no verdict when the real
    reason is that the runner never had the vendors.
    """
    steps = _steps("attribute.yml", "bisect")
    checked_out = {
        s.get("with", {}).get("repository")
        for s in steps
        if str(s.get("uses", "")).startswith("actions/checkout")
    }
    assert "calvinchengx/contoso-sources" in checked_out
    assert "calvinchengx/contoso-data-product-fabric-notebook-pipelines" in checked_out

    sources = _first_running(steps, "make sources")
    assert sources < _first_running(steps, "make up")
    verify = steps[_first_running(steps, "make verify")]["run"]
    assert "PRODUCT=" in verify, "verify would run against the empty ./product mount"


def _load_script(name: str):
    """Import a scripts/ module by path.

    tests/ sets no pythonpath and the scripts are not a package, so importing
    one by name would depend on the working directory pytest happened to start
    in.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader, f"scripts/{name}.py is not importable"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_an_empty_fixtures_selection_is_not_a_failure(monkeypatch):
    """The `fixtures` marker is an escape hatch, so having none is the goal.

    RULES.md forbids any test under tests/ from reaching a fixture wheel and
    makes the marker the exception. Zero marked tests is therefore the state
    that rule drives toward, and it is where this repo landed once the
    wheel-dependent tests moved to the product repository. pytest exits 5 for
    "no tests collected", so `make test-fixtures` could only fail -- masked for
    weeks behind an earlier step that failed first.
    """
    import subprocess as sp

    mod = _load_script("test_fixtures")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return sp.CompletedProcess(cmd, mod.NO_TESTS_COLLECTED)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.main() == 0
    assert "fixtures" in seen["cmd"], "the marker is what selects the tests"


def test_a_marked_test_that_fails_still_fails(monkeypatch):
    """Tolerating an EMPTY selection must not tolerate a failing one.

    Mapping every non-zero exit to success would turn the escape hatch into a
    step that cannot report anything, which is worse than the bug it replaced.
    """
    import subprocess as sp

    mod = _load_script("test_fixtures")
    monkeypatch.setattr(
        mod.subprocess, "run", lambda cmd, **kw: sp.CompletedProcess(cmd, 1)
    )
    assert mod.main() == 1


def test_the_fixtures_target_goes_through_the_script():
    """Bare `pytest -m fixtures` in the recipe is the bug itself.

    The Makefile may not branch (cmd.exe runs these recipes on Windows), so the
    exit-code handling has nowhere to live except a script.
    """
    recipe = [ln for ln in MAKEFILE.splitlines() if "test_fixtures.py" in ln]
    assert recipe, "test-fixtures no longer delegates to scripts/test_fixtures.py"


def test_the_product_steps_provision_their_own_environment():
    """`--no-sync` here means an EMPTY venv, not a protected one.

    The steps run with `--directory $(PRODUCT)`, so the environment they need
    belongs to the product's repository and a fresh checkout of it has none.
    `--no-sync` made uv create one and install nothing into it, and every step
    died importing its first dependency -- masked for weeks behind two earlier
    steps that failed first.

    The flag protects this repository's venv, where `make fixtures` installs
    wheels outside the lock. The product's venv has no such installs, and
    `uv run --frozen` would not prune them anyway, so there is nothing here for
    it to defend.
    """
    step = [ln for ln in MAKEFILE.splitlines() if ln.startswith("STEP :=")]
    assert len(step) == 1, "STEP is defined once"
    assert "--no-sync" not in step[0], "STEP must let uv provision the product venv"

    recipes = [
        ln for ln in MAKEFILE.splitlines() if "$(STEP)" in ln and "--no-sync" in ln
    ]
    assert not recipes, f"these would run against an empty product venv: {recipes}"


def test_the_acceptance_run_asserts_the_numbers_and_not_only_the_run():
    """A nightly that proves the platform RAN proves nothing about the answer.

    G50: across all seven platforms with an acceptance workflow, none compared a
    snapshot against an expected value. This one was worse than the others --
    `snapshot` is not in the product's STEPS list, so no unattended run had ever
    produced a snapshot at all, let alone read one back. Gold could have
    returned different money indefinitely behind a green tick.

    BOTH HALVES ARE ASSERTED. `make snapshot` must run, and it must run after
    the pipeline that gives it something to read and before the check.
    """
    acceptance = ROOT / ".github" / "workflows" / "acceptance.yml"
    raw = acceptance.read_text(encoding="utf-8")
    wf = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    for needed in ("make snapshot", "scripts/assert_snapshot.py"):
        assert needed in wf, f"the acceptance run never runs `{needed}`"
    through_uv = (
        "uv run --no-project python \\\n"
        "            ../contoso-data-product/scripts/assert_snapshot.py"
    )
    assert through_uv in wf, (
        "the assert step must go through uv like every other interpreter here"
    )
    core = wf[wf.index("repository: calvinchengx/contoso-data-product\n") :]
    assert re.search(r"ref: [0-9a-f]{40}", core[: core.index("path:")]), (
        "the contoso-data-product checkout is not pinned to a commit"
    )
    assert (
        wf.index("make verify")
        < wf.index("make snapshot")
        < wf.index("scripts/assert_snapshot.py")
    ), "verify, then snapshot, then assert -- in that order or the check is empty"


def test_the_snapshot_target_writes_where_the_acceptance_run_looks():
    """The path is the part that can rot, and here it crosses two repositories.

    `steps/snapshot.py` lives in the product and resolves its output against its
    own ROOT, so the workflow's path is the product checkout plus that filename.
    Restating it is how the two drift.
    """
    acceptance = ROOT / ".github" / "workflows" / "acceptance.yml"
    wf = acceptance.read_text(encoding="utf-8")
    product = "contoso-data-product-fabric-notebook-pipelines"
    assert f"../{product}/product_snapshot.json" in wf, (
        "the assert step does not read the product's snapshot"
    )
    assert f"make snapshot PRODUCT=../{product}" in wf, (
        "`make snapshot` runs against a different checkout than the one read"
    )


def test_no_image_comes_from_a_registry_the_family_does_not_trust():
    """G44: OpenMetadata shipped from docker.getcollate.io and took this
    nightly down twice in one morning.

    That registry is backed by neither Docker Hub nor GHCR, and a pull failure
    there reads as a broken governance step rather than as somebody else's
    outage. The images are mirrored into ghcr.io/calvinchengx by
    `calvinchengx/emulators` (`mirrors.json`, `scripts/mirror_images.py`), which
    copies the manifest index and records the digest the registry serves.

    AN ALLOWLIST, NOT A BAN ON ONE NAME. Asserting `getcollate` is absent would
    pass the day somebody adds a different vendor registry, which is the same
    defect one name later. This asks the opposite question: every image must
    come from somewhere the family already depends on being up.

    A VALUE THAT IS ENTIRELY A VARIABLE IS RESOLVED, not skipped. `${X}` hides
    the host completely, so a check that ignored those would be a check with a
    hole exactly where an unreviewed image would sit.
    """
    trusted = {
        # The family's own, and the mirrors it keeps there.
        "ghcr.io",
        # Docker Hub, which is what a bare `name/image` resolves to.
        "docker.io",
        "mcr.microsoft.com",
    }

    env = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    def host_of(ref: str) -> str | None:
        head = ref.split("/")[0]
        return head if ("." in head or ":" in head) else "docker.io"

    bad = []
    for path in sorted((ROOT / "compose").rglob("*.yml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            ref = stripped.split(":", 1)[1].strip()
            whole = re.fullmatch(r"\$\{(\w+)(?::[?-][^}]*)?\}", ref)
            if whole:
                name = whole.group(1)
                if name not in env:
                    bad.append(
                        f"{path.name}:{n}: ${{{name}}} is not in versions.env, "
                        f"so nothing here can tell which registry it names"
                    )
                    continue
                ref = env[name]
            host = host_of(ref)
            if host not in trusted:
                bad.append(
                    f"{path.name}:{n}: {host} is not a registry the family "
                    f"trusts to be up ({ref})"
                )
    assert not bad, "untrusted registries:\n  " + "\n  ".join(bad)


def test_openmetadata_comes_from_the_mirror():
    """The allowlist above would also pass if OpenMetadata simply vanished.

    So this names the thing G44 is about: the catalog's two images, from the
    family's registry, by the tag versions.env pins.
    """
    gov = (ROOT / "compose" / "governance.yml").read_text(encoding="utf-8")
    images = [ln.strip() for ln in gov.splitlines() if ln.strip().startswith("image:")]
    for name in ("openmetadata-server", "openmetadata-postgresql"):
        assert any(f"ghcr.io/calvinchengx/{name}:" in i for i in images), (
            f"the governance stack does not pull {name} from the family's registry"
        )
    assert not any("getcollate" in i for i in images), (
        "an image still comes straight from the vendor registry"
    )


def test_the_steps_reach_the_stack_this_platform_publishes():
    """FABRIC_PORT must move the client as well as the container.

    The product's `fabric_target` defaults to https://localhost:9443 and
    https://localhost:8443 when `FABRIC_EMULATOR_URL` / `ENTRA_EMULATOR_URL`
    are unset. Those are this compose's defaults too, so everything agreed
    until a port moved — and then the steps kept talking to 9443, which is
    whatever else happens to be listening.

    That is not a bind failure, it is a SUCCESSFUL run against the wrong
    system: measured, a run with FABRIC_PORT=19443 provisioned a workspace,
    landed four vendors and submitted a notebook job into another stack's
    emulator, while the stack this platform had just started sat empty. The
    only symptom was a job that never reached a terminal state.

    So the Makefile derives both URLs from the port variables, and this asserts
    the derivation rather than the comment.
    """
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[1]

    def resolved(name, env):
        out = subprocess.run(
            ["make", "-C", str(root), "-p", "-n"],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        ).stdout
        for line in out.splitlines():
            if line.startswith(f"{name} :="):
                return line.split(":=", 1)[1].strip()
        return None

    assert resolved("FABRIC_EMULATOR_URL", {}) == "https://localhost:9443"
    assert resolved("ENTRA_EMULATOR_URL", {}) == "https://localhost:8443"
    assert resolved("FABRIC_EMULATOR_URL", {"FABRIC_PORT": "19443"}) == (
        "https://localhost:19443"
    ), (
        "FABRIC_PORT moved the container but not the client — "
        "the exact split this prevents"
    )
    assert resolved("ENTRA_EMULATOR_URL", {"ENTRA_PORT": "18443"}) == (
        "https://localhost:18443"
    )

    # EVERY published port a client can be pointed at, not the two that
    # happened to fail first. Covering Fabric and Entra alone left ARM and the
    # vault still defaulting, and the next run died one step later on another
    # stack's ARM emulator.
    for var, port_var, default, moved in (
        ("VAULT_EMULATOR_URL", "KEYVAULT_PORT", "8444", "18444"),
        ("FABRIC_ARM_URL", "ARM_PORT", "8445", "18445"),
    ):
        assert resolved(var, {}) == f"https://localhost:{default}"
        assert resolved(var, {port_var: moved}) == f"https://localhost:{moved}", (
            f"{port_var} moves the container but not {var}"
        )


def test_every_digest_pinned_image_moves_with_its_version():
    """A digest left behind is worse than no digest at all.

    Docker ignores the tag in `repo:tag@sha256:...` and fetches the DIGEST. So a
    release run that bumped the versions and left the digests would pull the
    previous images while the summary named the new release — the very
    "verified a release nobody tested" failure `set_release.py` exists to
    prevent, reintroduced one level down where it is harder to see.

    `set_digests` is exercised with a stub resolver: the rule under test is that
    every pinned prefix is rewritten, not that the registry answers.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import set_release

    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    fake = "sha256:" + "b" * 64
    original = set_release.digest_of
    try:
        set_release.digest_of = lambda image, tag: fake
        new, moved = set_release.set_digests(text, "9.9.9")
    finally:
        set_release.digest_of = original

    assert set(moved) == set(set_release.PINS), moved
    for prefix in set_release.PINS:
        assert re.search(rf"^{prefix}_DIGEST={fake}$", new, re.M), prefix


def test_every_release_tracked_image_is_digest_pinned():
    """The allowlist and the pin list must not drift apart.

    `TRACKS_THE_RELEASE` says which versions a release moves; `PINS` says which
    images are fetched by digest. An image in the first and not the second is
    pinned by a tag that its own publisher documents as NOT an identity
    (fabric-emulator's scripts/check_image_digests.py). The reverse would leave
    a digest nobody updates.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from set_release import PINS, TRACKS_THE_RELEASE

    tracked = {k[: -len("_VERSION")] for k in TRACKS_THE_RELEASE}
    assert tracked == set(PINS), (
        f"release-tracked but not digest-pinned: {sorted(tracked - set(PINS))}; "
        f"digest-pinned but not release-tracked: {sorted(set(PINS) - tracked)}")


def test_the_compose_file_fetches_those_images_by_digest():
    """The pin has to reach the thing that pulls, not just versions.env."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from set_release import PINS

    compose = (ROOT / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    for prefix, image in PINS.items():
        for line in compose.splitlines():
            if f"image: {image}:" in line:
                assert f"@${{{prefix}_DIGEST" in line, (
                    f"{image} is pulled by tag alone:\n  {line.strip()}")
                break
        else:
            raise AssertionError(f"{image} is not referenced in the compose file")


def test_a_release_run_writes_the_versions_AND_the_digests(tmp_path):
    """End to end through `main()`, because that is where the wiring can be cut.

    CAUGHT BY MUTATION: deleting the `set_digests` call from `main()` left every
    other test in this file passing, because they exercise `set_digests`
    directly. A release would then have written fresh versions beside stale
    digests, and docker would have pulled the stale ones — the failure silently
    restored by removing one line.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import set_release

    versions = tmp_path / "versions.env"
    versions.write_text((ROOT / "versions.env").read_text(encoding="utf-8"),
                        encoding="utf-8")
    fake = "sha256:" + "c" * 64
    saved = (set_release.VERSIONS, set_release.digest_of, sys.argv)
    try:
        set_release.VERSIONS = versions
        set_release.digest_of = lambda image, tag: fake
        sys.argv = ["set_release.py", "9.9.9"]
        assert set_release.main() == 0
    finally:
        set_release.VERSIONS, set_release.digest_of, sys.argv = saved

    written = versions.read_text(encoding="utf-8")
    for key in set_release.TRACKS_THE_RELEASE:
        assert re.search(rf"^{key}=9\.9\.9$", written, re.M), key
    for prefix in set_release.PINS:
        assert re.search(rf"^{prefix}_DIGEST={fake}$", written, re.M), (
            f"{prefix} kept a stale digest beside a fresh tag")


def test_every_pullable_image_in_every_compose_file_is_digest_pinned():
    """All three compose files, every `image:` line, with exemptions in place.

    Broader than `test_the_compose_file_fetches_those_images_by_digest`, which
    only checks the images a release retags. This one catches a service added
    later, and it caught the governance overlay — openmetadata and opensearch
    were pulled by tag while the main file was fully pinned.

    `compose/terminal.yml` is the exemption: it exists to point a filmed run at
    an unreleased `:dev` build, so a digest there would defeat the overlay.
    """
    for path in sorted((ROOT / "compose").glob("*.yml")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            if "digest-exempt:" in "\n".join(lines[max(0, i - 2):i]):
                continue
            assert "@${" in stripped or "@sha256:" in stripped, (
                f"{path.name}: pulled by tag alone: {stripped}")
