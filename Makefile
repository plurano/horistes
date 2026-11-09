# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
-include .config/praetor/engine.mk

SHELL := /bin/sh
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
# The ref the interface is compared with; CI passes the base of the change.
BASE_REF ?= origin/main
# actionlint and gitleaks do not come from requirements/tools.txt. The gate
# refuses any other actionlint version than this one; the tests refuse any
# other gitleaks release than the one the gate workflow pins.
ACTIONLINT ?= actionlint
ACTIONLINT_VERSION := 1.7.12
GITLEAKS ?= gitleaks
PRAETORCTL ?= $(shell command -v praetorctl 2>/dev/null || command -v standardsctl 2>/dev/null || echo praetorctl)

.PHONY: verify verify-all tools check-tools-stamp requirements requirements-fuzz lint-yaml lint-python lint-workflows \
	check-interface check-hosts check-exceptions check-templates check-scorecard test verify-reuse \
	interface-snapshot list-pins check-pins pin check-runners check-links check-examples

# The one gate is `make verify-all`: CI runs exactly that target, so a local
# green run and a green status check mean the same thing. This repository's own
# checks come first, then the engine's context check and standards audit.
verify-all: verify
	@$(PRAETORCTL) compile-context --verify
	@$(PRAETORCTL) caveman check --configured-sources
	@$(PRAETORCTL) audit

# This repository's own checks; they need no engine.
verify: lint-yaml lint-python lint-workflows check-interface check-hosts check-exceptions check-templates \
	check-scorecard check-runners check-links check-examples test verify-reuse
	@printf '%s\n' 'PASS: YAML, Python and workflow lint, interface lock, host scan, accepted findings, caller templates, scorecard service rules, runner labels, markdown links, fenced examples, tests and licence headers.'

# One version of every tool for the local gate and CI, installed with the
# hashes requirements/tools.txt records.
# Linked worktrees share .venv by symlink: compare content, not mtime.
# Hashed with Python's hashlib: macOS ships no coreutils hash command.
# A failed hash stops `tools` before it installs or writes the stamp.
TOOLS_HASH = $$($(PYTHON) -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' requirements/tools.txt)
TOOLS_STAMP = $$(cat $(VENV)/.stamp 2>/dev/null)

tools:
	@hash=$(TOOLS_HASH) || { printf '%s\n' 'cannot hash requirements/tools.txt' >&2; exit 1; }; \
	if [ "$(TOOLS_STAMP)" != "$$hash" ]; then \
		if [ ! -x $(BIN)/python ]; then $(PYTHON) -m venv $(VENV); fi && \
		$(BIN)/python -m pip install --quiet --disable-pip-version-check --require-hashes -r requirements/tools.txt && \
		printf '%s\n' "$$hash" > $(VENV)/.stamp; \
	fi

# The pre-push gate refuses a run that installs into .venv during
# verification, so pre-push runs this first: it fails when the stamp does
# not match requirements/tools.txt, or when that file cannot be read.
check-tools-stamp:
	@hash=$(TOOLS_HASH); \
	if [ -z "$$hash" ] || [ "$(TOOLS_STAMP)" != "$$hash" ]; then \
		printf '%s\n' '$(VENV)/.stamp does not match requirements/tools.txt; run make tools' >&2; \
		exit 1; \
	fi

# Recompile the pins; a release younger than three days is left out. The
# header of the output records this command and Renovate reruns it from there,
# so every option is one Renovate accepts and each value is joined with `=`
# (tests/test_update_bot.py holds the two in step).
requirements:
	uv pip compile --quiet --universal --python-version=3.12 --generate-hashes --exclude-newer=P3D \
		requirements/tools.in --output-file=requirements/tools.txt

requirements-fuzz:
	uv pip compile --quiet --python-version=3.12 --python-platform=x86_64-manylinux_2_17 --generate-hashes --exclude-newer=P3D \
		requirements/fuzz.in --output-file=requirements/fuzz.txt

lint-yaml: tools
	$(BIN)/yamllint --strict .

lint-python: tools
	$(BIN)/ruff check --config ruff.toml tools tests fuzz
	$(BIN)/ruff format --check --config ruff.toml tools tests fuzz

# actionlint reads .github/workflows by itself; zizmor is given the workflow
# and the action directories and must find at least the workflows.
lint-workflows: tools
	@found="$$($(ACTIONLINT) -version | head -n 1)"; \
	if [ "$$found" != "$(ACTIONLINT_VERSION)" ] && [ "$$found" != "v$(ACTIONLINT_VERSION)" ]; then \
		printf '%s\n' "actionlint $(ACTIONLINT_VERSION) required, found: $$found" >&2; exit 1; fi
	$(ACTIONLINT)
	$(BIN)/zizmor --offline --strict-collection --persona pedantic .github/workflows $(wildcard actions)

check-interface:
	$(PYTHON) -B tools/interface.py check --base-ref $(BASE_REF)

# A host declared with a day it ends takes the same longest time as the
# exceptions below.
check-hosts:
	$(PYTHON) -B tools/hosts.py check

# An accepted linter finding (zizmor, actionlint, ruff.toml) and an entry of
# exceptions.yaml name one file, a reason and a day it ends, at most 90 days ahead.
check-exceptions:
	$(PYTHON) -B tools/exceptions.py check

# Every caller template renders into a caller of a workflow kept here, by
# commit, with inputs that workflow declares.
check-templates:
	$(PYTHON) -B tools/render.py check

# A workflow that runs the Scorecard action keeps to the rules the Scorecard
# service puts on it before it takes a published result.
check-scorecard:
	$(PYTHON) -B tools/scorecard.py check

interface-snapshot:
	$(PYTHON) -B tools/interface.py snapshot

# Action pins (ADR-0016). `list-pins` asks no host. `check-pins` and `pin` ask
# the forge and the repository of every action, so they are no part of the
# gate; they need a token. PINS_ROOT is the checkout that is read.
PINS_ROOT ?= .
list-pins check-pins pin: tools
	ZIZMOR="$(BIN)/zizmor" $(BIN)/python -B tools/pins.py $(patsubst %-pins,%,$@) --root "$(PINS_ROOT)"

# Runner labels (ADR-0018): no workflow names a floating label of a hosted
# machine. The check asks no host and needs no token, so it is part of the
# gate here; another repository is read with PINS_ROOT.
check-runners: tools
	$(BIN)/python -B tools/pins.py runners --root "$(PINS_ROOT)"

check-links: tools
	$(BIN)/python -B tools/links.py

check-examples: tools
	$(BIN)/python -B tools/examples.py

# The tests plant one defect per rule; the outside linters are run on a
# planted workflow or repository as well, so a linter that stopped refusing
# fails here.
# The coverage floor only goes up. The test recipe writes coverage data files
# into its own temporary directory so concurrent runs cannot collide or erase
# each other's data. Each coverage step reads pyproject.toml only, so an
# inherited COVERAGE_RCFILE or a stray .coveragerc cannot set the floor.
# The run clears PYTHONOPTIMIZE, which would strip the asserts of tools/, and
# PYTEST_ADDOPTS, whose options can override the warning filters of
# pyproject.toml.
# The last step compares the unrounded total with the floor.
test: tools
	data=$$(mktemp -d) && \
	trap 'rm -rf "$$data"' EXIT && \
	PYTHONOPTIMIZE= PYTEST_ADDOPTS= ACTIONLINT="$(ACTIONLINT)" ZIZMOR="$(BIN)/zizmor" REUSE="$(BIN)/reuse" GITLEAKS="$(GITLEAKS)" \
		YAMLLINT="$(BIN)/yamllint" RUFF="$(BIN)/ruff" \
		$(BIN)/python -X dev -B -m coverage run --rcfile=pyproject.toml --data-file="$$data/.coverage" -m pytest && \
	$(BIN)/python -B -m coverage combine --rcfile=pyproject.toml --data-file="$$data/.coverage" && \
	$(BIN)/python -B -m coverage report --rcfile=pyproject.toml --data-file="$$data/.coverage" && \
	$(BIN)/python -B -m coverage report --rcfile=pyproject.toml --data-file="$$data/.coverage" --precision=10 --format=total

# Every file carries its licence header or is named in REUSE.toml.
verify-reuse: tools
	$(BIN)/reuse lint
