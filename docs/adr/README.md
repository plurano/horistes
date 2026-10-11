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
