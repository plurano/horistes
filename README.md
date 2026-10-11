# Plurano shared CI (working name)

Reusable workflows and templates for the repositories of the family: the jobs
every repository runs (build, test, lint, security scans, release, landing) are
kept once here and called from thin callers, instead of each repository keeping
its own copy.

## Name

`shared-ci` is a working name. The real name is chosen before the first
release.

## Status

Private; nothing is released yet. The first unit is a plan: what the
repositories run today, which jobs are the same, and the order in which they
switch over.

## Licence

EUPL-1.2, see [LICENSE](LICENSE).
