# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
-include .config/praetor/engine.mk

SHELL := /bin/sh
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
# The ref the interface is compared with; CI passes the base of the change.
BASE_REF ?= origin/main
# actionlint is the one tool that does not come from requirements/tools.txt;
# the gate refuses any other version than this one.
ACTIONLINT ?= actionlint
ACTIONLINT_VERSION := 1.7.12
PRAETORCTL ?= $(shell command -v praetorctl 2>/dev/null || command -v standardsctl 2>/dev/null || echo praetorctl)

.PHONY: verify verify-all tools requirements lint-yaml lint-python lint-workflows \
	check-interface check-hosts check-exceptions check-templates check-scorecard test verify-reuse \
	interface-snapshot

# The one gate is `make verify-all`: CI runs exactly that target, so a local
# green run and a green status check mean the same thing. This repository's own
# checks come first, then the engine's context check and standards audit.
verify-all: verify
	@$(PRAETORCTL) compile-context --verify
	@$(PRAETORCTL) caveman check --configured-sources
	@$(PRAETORCTL) audit

# This repository's own checks; they need no engine.
verify: lint-yaml lint-python lint-workflows check-interface check-hosts check-exceptions check-templates \
	check-scorecard test verify-reuse
	@printf '%s\n' 'PASS: YAML, Python and workflow lint, interface lock, host scan, accepted findings, caller templates, scorecard service rules, tests and licence headers.'

# One version of every tool for the local gate and CI, installed with the
# hashes requirements/tools.txt records.
tools: $(VENV)/.stamp
$(VENV)/.stamp: requirements/tools.txt
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --disable-pip-version-check --require-hashes -r requirements/tools.txt
	@touch $@

# Recompile the pins; a release younger than three days is left out.
requirements:
	uv pip compile --quiet --universal --python-version 3.12 --generate-hashes --no-header --no-annotate \
		--exclude-newer "$$(date -u -d '3 days ago' +%Y-%m-%dT00:00:00Z)" \
		requirements/tools.in -o requirements/tools.txt

lint-yaml: tools
	$(BIN)/yamllint --strict .

lint-python: tools
	$(BIN)/ruff check tools tests
	$(BIN)/ruff format --check tools tests

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

# An accepted linter finding and an entry of exceptions.yaml name one file, a
# reason and a day it ends, at most 90 days ahead.
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

# The tests plant one defect per rule; the two outside linters are run on a
# planted workflow as well, so a linter that stopped refusing fails here.
test: tools
	ACTIONLINT="$(ACTIONLINT)" ZIZMOR="$(BIN)/zizmor" REUSE="$(BIN)/reuse" $(BIN)/python -B -m pytest

# Every file carries its licence header or is named in REUSE.toml.
verify-reuse: tools
	$(BIN)/reuse lint
