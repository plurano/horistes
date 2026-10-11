# Architectural Decision Records (ADRs)

This directory documents key architectural decisions. Records are append-only
like public APIs under HISS-14 (Append-Only ABI & Migration Footers).
An accepted record keeps its number and text; a changed decision is a new
record that supersedes it.

| Number | Date | Title | Status |
| :--- | :--- | :--- | :--- |
| [0000](0000-template.md) | 2026-09-11 | ADR template | Accepted |
| [0001](0001-three-layers.md) | 2026-10-11 | One source, a copy per owner, thin callers | Accepted |
| [0002](0002-versioning-and-pinning.md) | 2026-10-11 | Versions, pins and the recorded interface | Accepted |
| [0003](0003-declared-hosts.md) | 2026-10-11 | Every host a workflow reaches is declared | Accepted |
| [0004](0004-same-verdict-before-a-switch.md) | 2026-10-11 | A consumer switches only after the same verdict is shown | Accepted |
| [0005](0005-host-exception-with-an-end.md) | 2026-10-11 | A host without a switch is an exception with a day it ends | Accepted |
| [0006](0006-planted-case-as-a-fixture.md) | 2026-10-11 | A planted case may be a fixture in the tree | Accepted |
| [0007](0007-one-own-exception-list.md) | 2026-10-11 | Exceptions the engine's list cannot carry live in one own list | Accepted |
| [0008](0008-licence-tool-image-by-digest.md) | 2026-10-11 | The licence tool's image is named by digest | Accepted |
| [0009](0009-caller-templates-in-the-engines-syntax.md) | 2026-10-11 | Caller templates are written in the engine's syntax and rendered here until the engine does | Accepted |
| [0010](0010-one-longest-time-for-every-exception.md) | 2026-10-11 | A dated host runs no longer than any other exception | Accepted |
| [0011](0011-uploaded-result-of-a-job-that-always-passes.md) | 2026-10-11 | A job that passes whatever it finds is compared by the result it uploads | Accepted |
| [0012](0012-row-outside-the-trial.md) | 2026-10-11 | A push whose commit does not hold a job's workflow file lies outside the trial | Accepted |
