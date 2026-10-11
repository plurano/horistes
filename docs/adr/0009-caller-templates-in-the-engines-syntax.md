<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0009: Caller templates are written in the engine's syntax and rendered here until the engine does

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

A consumer keeps one thin caller per shared workflow
([ADR-0001](0001-three-layers.md)). Written by hand in each repository, the
callers drift: a trigger is left out, the commit is replaced by a branch, an
input keeps a name the workflow gave up.

The engine already writes each repository's workflow files from templates: Go
`text/template` with the delimiters `<%` and `%>`, so that the forge's own
`${{ }}` stays literal. It renders only the templates built into it, and its
context has no field for a shared repository, a commit or a version
(cordanaLLM/praetor issue 1211). Until that issue is closed a caller template
can be written, but nothing renders or checks it.

## Decision

- One template per shared workflow lives in `templates/callers/<name>.yml.tmpl`,
  in the engine's syntax. Its fields are the engine's `.RunnerTag` and the
  working names `.Shared.Repository`, `.Shared.Commit` and `.Shared.Version`;
  the names become final with issue 1211.
- A template opens with a maintainer note. While `exceptions.yaml` holds the
  entry on the required check's name
  ([ADR-0007](0007-one-own-exception-list.md)), the note names that entry's
  issue and says that the calling job stays out of the required checks.
- `tools/render.py render` renders a template from a context file. It reads
  three things: a field by its dotted name, a note, and the trim markers. It
  refuses every other action (a function, a pipeline, a condition), a field the
  context does not hold, and a value that is no text, whole number or truth
  value. Whatever it renders, it renders byte for byte as the engine's
  template call does; what it is not sure of, it refuses.
- Because functions are refused, a template spells its trigger out instead of
  calling the engine's trigger function.
- `tools/render.py check` is part of the gate (`make check-templates`). It
  renders every template with sample values and fails unless the result is a
  workflow with one job that calls a workflow file of this repository, names
  the repository and the commit through their fields, carries the version as a
  comment behind the commit, passes only inputs the called workflow declares
  and every input it requires, asks for no write scope and passes no secret.
- This repository's own caller, `licence-check.yml`, is not rendered from the
  template: it calls the workflow of the same commit with the `./` form.

## Consequences

- **Positive**: a consumer's caller is one rendered text and not a copy edited
  by hand; an input renamed or removed here fails this repository's gate before
  any consumer runs it; a template the engine could not render is refused here
  first.
- **Negative**: a second renderer exists beside the engine's until issue 1211
  is closed; then the engine renders these files, `render` leaves and `check`
  stays. The field names may change with that issue; nothing is released yet,
  so no consumer depends on them.
- **Neutral**: the comparison with the engine's template call is a measurement
  taken once, with Go 1.27.1, on 45 short texts and on the licence template;
  the expected texts are kept in the tests, so the gate needs no Go.

## References

- Fleet answer ledger, id shared-ci-caller-template-syntax, 2026-10-11
  (paraphrased): the engine's workflow templates are Go text/template with the
  delimiters `<%` and `%>`, trim markers and a note form, and an undeclared
  field fails the render; caller templates use that syntax and the working
  field names until the engine issue on rendering them is closed.
- Fleet answer ledger, id shared-ci-runner-default, 2026-10-11 (paraphrased):
  until the engine issue on the required check's name is fixed, a calling job
  stays out of a consumer's required checks or the switch waits, and the caller
  template's note says so.
- `tests/test_render.py`: the texts rendered as the engine renders them, the
  refused texts, and one planted defect per rule of `check`.
