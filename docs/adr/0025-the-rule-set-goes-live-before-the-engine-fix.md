<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0025: The rule set goes live before the engine names the licence check right

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

The repository is public ([ADR-0024](0024-public-before-the-first-release.md)),
and the free plan now allows a rule set on it.
[ADR-0007](0007-one-own-exception-list.md) said that before the repository
goes public or a rule set goes live, cordanaLLM/praetor issue 1212 is fixed or
no rule set is written. That issue is still open: the engine requires the check
`Licence`, while the forge reports the licence job as `Licence / REUSE lint`.

One maintainer works on this repository, and no review bot can approve a pull
request.

## Decision

- `.standards.yaml` sets `review_mode: single_maintainer`. The rule set the
  engine renders for it requires no approving review and no code-owner review.
  It names the repository admin role as the one bypass actor, in bypass mode
  `pull_request`.
- The rule set in `.github/rulesets/main.json` is applied through the forge
  API as the engine renders it and then read back. `praetorctl sync --remote`
  does not write it.
- Until the engine names the reported check, a pull request whose `Licence`
  check never reports merges through that bypass. The entry for issue 1212 in
  `exceptions.yaml` stays until then.
- Pull requests merge as squash merges through the forge. A push to `main`
  outside a pull request is refused.

This replaces the condition on issue 1212 in the last item of the ADR-0007
decision. The rest of ADR-0007 stands.

## Consequences

- **Positive**:
  - `main` refuses deletion, force pushes, unsigned commits and merge commits
    from now on.
  - The verification gate is a required check.
- **Negative**:
  - Each merge uses the bypass while issue 1212 is open, so the forge does not
    block the licence check by itself; the verification gate runs the REUSE
    lint too.
  - Squash merges are signed by the forge's key, not the maintainer's.
- **Neutral**: once the engine declares `Licence / REUSE lint`, the pin moves,
  the rule set is rendered again, the entry for issue 1212 leaves, and merges
  no longer need the bypass.

## References

- Fleet answer ledger, 2026-10-12 (paraphrased).
  horistes-ruleset-licence-context: apply the engine's rendering now with a
  read-back; until the engine fix lands and the pin moves, a pull request whose
  licence check never reports merges through the pull-request bypass; the
  entry for issue 1212 stays in the repository's own exception list; merges
  are squash merges through the forge.
- Fleet answer ledger, 2026-10-12 (paraphrased). horistes-flip: the repository
  turns public, and the shared CI session applies the rendered rule set with a
  read-back.
