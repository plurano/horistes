<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0048: The Python floor is tested in CI

- **Status**: Accepted
- **Date**: 2026-10-19
- **Authors**: Lusoris

## Context

`README.md:143` and `README.md:355` promise Python 3.12 compatibility: consumers
run `tools/readback.py` and `tools/stranger.py` with their own Python interpreter.

CI ran only the runner image's default Python interpreter, leaving the stated
Python 3.12 floor unproven for consumer-facing tools.

## Decision

- `.github/workflows/ci.yml` adds a second job `floor` ("Python 3.12 floor").
- The job sets up Python 3.12 via `actions/setup-python` and runs the tests of
  the consumer-facing tools (`tests/test_readback.py`, `tests/test_stranger.py`).
- The job runs on every pull request without a path filter, so the engine
  renders it into `.github/rulesets/main.json` as a required check.

### Alternatives considered

- Full `make test` on 3.12: rejected because tools install twice, adding ~5 min
  per pull request.
- Ruff target py312 only: rejected because syntax linting checks syntax, not
  standard library API availability.

## Consequences

- **Positive**:
  - The stated Python 3.12 floor for consumer-facing tools is proven on every
    pull request before merge.
  - A compatibility break against Python 3.12 fails the gate automatically.
- **Negative**:
  - Every pull request runs an additional job with tool installation and test
    execution.
- **Neutral**:
  - Developer checks and gate runs continue on the runner's native Python.
  - `actions/setup-python` is declared in `hosts.toml` under `[actions]`.

## References

- Gap audit C13-01: consumer tools promise Python 3.12, unproven in CI.
- `README.md:143`, `README.md:355`.
- `.github/workflows/ci.yml`: job `floor`.
