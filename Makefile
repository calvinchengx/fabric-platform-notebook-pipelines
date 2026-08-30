# The only interface. Windows, macOS and Linux run the SAME targets.
#
# Windows users:  winget install ezwinports.make
# Everyone needs: Docker, and uv (https://docs.astral.sh/uv/)
#
# EVERY RECIPE IS A ONE-LINER over `docker`, `uv` or `python`. Nothing here may
# use a shell builtin, a pipe, `rm -rf`, backticks or an `if`: GNU Make on
# Windows runs recipes through cmd.exe, where none of that means what it means
# on a POSIX shell. Logic belongs in scripts/, which is Python, which is the
# only thing all three platforms genuinely agree on.
#
# The test for whether a recipe belongs here: could it run unchanged in cmd.exe?
# If not, it goes in a script.
#
# UV, STRICTLY. pyproject.toml is the manifest and uv.lock is committed, so a
# clone resolves to the same versions on all three platforms. No bare python,
# no pip, no `--with` (which resolves fresh every run and would silently change
# the test suite between two runs of the same commit).
#
#   --frozen           use the committed lock; never resolve or update it
#   --no-sync          do not touch the environment
#   --no-project       stdlib-only diagnostics, so `make doctor` still works
#                      when the environment is broken or absent
#
# `make fixtures` installs the generator wheels with `uv pip install`, outside
# the lock — that is deliberate, because WHICH release they came from is the
# thing under test and pinning them in the lock would defeat it.
#
# MEASURED, after getting this wrong twice:
#
#   uv run --frozen   (lock unchanged)  -> fixtures SURVIVE
#   uv sync           (explicit)        -> fixtures EVICTED
#
# So `uv sync` prunes anything not in the lock, and re-running `make fixtures`
# after one is required rather than optional. `--no-sync` on this repository's
# own run targets keeps a step from reconciling the environment out from under
# them.
#
# THE PRODUCT'S STEPS DO NOT PASS IT, and that is the same measurement read the
# other way. They run in the PRODUCT's virtualenv, which a fresh checkout of
# that repository does not have: `--no-sync` made uv create an EMPTY one and
# every step died importing its first dependency. Since `uv run --frozen`
# leaves out-of-lock installs alone -- the top line of the table above --
# omitting the flag provisions the environment from the committed lock and
# prunes nothing, which is what was wanted at both ends.

.DEFAULT_GOAL := help
# THE VENDORS LIVE IN THEIR OWN REPOSITORY. This platform used to carry a copy
# of them; it now mounts contoso-sources' and generates its compose fragment
# from that repo's declaration, so every cell in the family pulls the same
# bytes -- which is the only reason their gold numbers are comparable.
SOURCES ?= ../contoso-sources
# ABSOLUTE, and exported. The steps now run with the PRODUCT as their working
# directory, so a path relative to this Makefile would resolve against the
# wrong repository -- and exporting it is what makes this platform's choice of
# vendors govern, rather than the product guessing at a sibling checkout.
export SOURCES := $(abspath $(SOURCES))
# WHICH PLATFORM IS RUNNING THE PRODUCT. gold runs dbt inside a container this
# platform defines, so the product has to be able to ask this platform to start
# it -- it knows it needs a dbt container, not which compose files declare one.
# WHERE THE STEPS REACH THE STACK, derived from the ports it publishes.
#
# The product's client (`fabric_target`) defaults to https://localhost:9443 and
# https://localhost:8443 when these are unset. Those are the compose DEFAULTS,
# so everything agreed until someone moved a port -- and then the steps kept
# talking to 9443, which is whatever else is listening there. Measured, and it
# is much worse than a bind failure: a run with FABRIC_PORT=19443 provisioned
# its workspace, landed four vendors and submitted a notebook job into ANOTHER
# STACK'S emulator, then failed with "the RunNotebook job never reached a
# terminal state" because that emulator's agent was never asked to run it. The
# stack this platform started sat empty and correct throughout.
#
# Deriving them here means the override cannot half-apply: one variable moves
# the container and the client together.
# ALL FOUR, not two. The first version of this covered Fabric and Entra and
# stopped, which fixed the symptom I had in front of me and left the same split
# open for the other two: the next run got one step further and died on
# `https://localhost:8445`, ARM's default, answered by another stack's ARM
# emulator. Every published port a client can be pointed at belongs here, or
# the override half-applies again somewhere new.
export FABRIC_EMULATOR_URL := https://localhost:$(or $(FABRIC_PORT),9443)
export ENTRA_EMULATOR_URL  := https://localhost:$(or $(ENTRA_PORT),8443)
export VAULT_EMULATOR_URL  := https://localhost:$(or $(KEYVAULT_PORT),8444)
export FABRIC_ARM_URL      := https://localhost:$(or $(ARM_PORT),8445)
# THE CATALOG, on the port THIS platform publishes it on.
#
# Every URL above is exported for the same reason: the product's steps address
# a service by env var, and the platform is the only thing that knows which
# port it bound. OM_URL was the one missing, and the omission became a failure
# the day the catalog moved off 8585 to avoid colliding with the two sibling
# platforms -- `govern.py` kept its own default of localhost:8585, so the
# medallion ran to step 14 of 16 and died on "Connection refused" against a
# port nothing was listening on. The default here is compose's own.
export OM_URL := http://localhost:$(or $(OM_PORT),18587)/api/v1

export PLATFORM := $(CURDIR)
# WHERE THE dbt CONTAINER FINDS THE PROJECT. The product stages its gold models
# into its own gold/, so the mount has to follow PRODUCT rather than pointing at
# this repository -- which silently built stale models when the two were split.
export PRODUCT_GOLD := $(abspath $(PRODUCT))/gold

# PRODUCT IS A PATH, NOT A NAME. This platform instantiates the emulator and
# runs whatever product it is pointed at; naming one here would make "a second
# product can use this unchanged" untestable, because there would be nothing
# else to point it at. `./product` is an empty, gitignored mount point.
PRODUCT ?= ./product
STEP := uv run --directory $(PRODUCT) --frozen

.PHONY: help doctor fixtures sources up down config lint fmt capture demo govern verify reconcile snapshot test test-fixtures clean witness logs

help:  ## Show the targets
	@uv run --no-project python scripts/help.py

doctor:  ## Check prerequisites and report what is and is not ready
	@uv run --no-project python scripts/doctor.py

fixtures:  ## Install the seeded generators published by the pinned release
	@uv run --no-project python scripts/fixtures.py

sources:  ## Materialise the vendor exports, in the repo that owns them
	@$(MAKE) -C $(SOURCES) sources

up:  ## Start the emulator family and the source systems
	@uv run --no-project python scripts/compose.py up -d

down:  ## Stop everything and remove volumes
	@uv run --no-project python scripts/compose.py down -v

logs:  ## Follow the stack's logs (SVC=<service> to narrow)
	@uv run --no-project python scripts/compose.py logs -f --tail 100 $(SVC)

config:  ## Show the resolved compose config (proves the pin)
	@uv run --no-project python scripts/compose.py config

capture:  ## Verify and photograph the catalog (the flow video comes from `make verify`)
	@uv run --frozen --no-sync python platform/capture.py

govern:  ## Catalog the platform in OpenMetadata (also runs inside `make verify`)
	@$(STEP) python steps/govern.py

verify:  ## Run the platform end to end against the pinned release
	@$(STEP) python steps/pipeline.py

witness: verify ## The family's one word for `verify`: run the cell, fail if it fails

reconcile:  ## Prove the model's numbers ARE the warehouse's (needs `make verify` first)
	@$(STEP) python steps/reconcile.py

snapshot:  ## Publish gold's numbers for compare_products (needs `make verify` first)
	@$(STEP) python steps/snapshot.py

demo:  ## Record the run: terminal and flow graph side by side (needs ttyd)
	@uv run --frozen --no-sync python scripts/demo.py

lint:  ## ruff (lint + format check) and ty (types)
	@uv run --frozen ruff check .
	@uv run --frozen ruff format --check .
	@uv run --frozen ty check .

fmt:  ## Apply ruff's formatting and safe fixes
	@uv run --frozen ruff check --fix .
	@uv run --frozen ruff format .

test:  ## The repo's own tests — version lockstep, boundaries, config
	@uv run --frozen pytest -q tests -m "not fixtures"

test-fixtures:  ## The tests that need the published wheels — after `make fixtures`
	@uv run --no-project python scripts/test_fixtures.py

clean:  ## Remove build and run artifacts
	@uv run --no-project python scripts/clean.py
