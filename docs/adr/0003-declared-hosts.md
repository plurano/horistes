<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0003: Every host a workflow reaches is declared

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

A shared workflow runs in every repository that calls it, with that
repository's token and, for some jobs, its secrets. A download or an upload a
consumer did not expect is a supply-chain and a privacy problem, and it is hard
to see in review once the workflow lives elsewhere.

## Decision

- With default inputs a reusable workflow reaches only the forge itself.
- Every other host is named in `hosts.toml` for the file that reaches it, with
  a reason. For a reusable workflow the entry also names a boolean input that
  defaults to false and switches the host on.
- `hosts.toml` lists the outside actions that may be used, each with the hosts
  it reaches by itself. An action missing from the list fails the gate.
- `tools/hosts.py check` reads URLs, registry paths, container images and
  package commands in every workflow and action, and the Makefile of a file
  that runs `make`. It fails on an undeclared host, an action outside the list,
  a declaration without a reason and a declaration nothing uses any more.
- This repository's own gate is no reusable workflow; its hosts are declared
  with a reason and have no switch.

The scan is a review aid on the text of the files. It is not an egress filter:
a host reached from inside a tool or an action is known only as far as the
declaration of that action says.

## Consequences

- **Positive**: the hosts of every workflow are one reviewed list; a new host
  shows up as a change of that list.
- **Negative**: language workflows need their package registry to do anything
  useful, so "off by default" has to be decided for them before they are
  written; the list of hosts per action is kept by hand.

## References

- Fleet answer ledger, id ci-config-19 (paraphrased): tools come from the
  pinned development image or the forge's registry rather than from a download
  in the job.
