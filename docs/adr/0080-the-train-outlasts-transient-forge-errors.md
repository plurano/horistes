<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0080: The train outlasts transient forge errors

- **Status**: Accepted
- **Date**: 2026-11-12
- **Authors**: Lusoris

## Context

On 2026-11-12 the fleet merge train in the k8s repository went red four times in
six hours. None of the failures came from a pull request the train serves:

- Run 43203110717: the forge closed the connection while the train revoked its
  installation token (`http.client.RemoteDisconnected`). `call` caught only
  `URLError` and `TimeoutError`, and urllib raises a dropped connection from
  `getresponse` unwrapped, so the exception escaped `revoke`. `revoke` runs from
  a `finally`, so it ended the whole sweep and hid the repository's result.
- Run 43251161246: the REST read of a red run's jobs answered HTTP 502. Only the
  GraphQL page read (`read_page`) was tried again; REST reads ran once.
- Run 43245308767: the merge was refused with "Pull Request is not mergeable".
  Another pull request had landed between the train's read and its merge, which
  left this one BEHIND. The refusal failed the repository.
- Run 43262049834: release-please answered "Server Error" while it opened the
  release pull request, and the release job failed.

## Decision

- **Reads are tried again; writes are not.** `call` sends a GET at most
  `READ_ATTEMPTS` (3) times, `READ_BACKOFF_SECONDS` apart, when the forge answers
  502, 503 or 504 (`RETRY_CODES`) or the connection drops or times out
  (`transient`). `read_graphql` uses the same test for every GraphQL read: the
  page read, commits, merge state, review bodies and merged pull requests. Every
  other method runs once: a write that reached the forge before the answer was
  lost must not run twice. A 500 is not tried again; it stays a refusal.
- **Transport errors are refusals.** `request_once` turns every `OSError` and
  `http.client.HTTPException` into `RefusedError`, so a dropped connection
  refuses one call and is judged like any other refusal. A body shorter than its
  `Content-Length` is refused with `http.client.IncompleteRead` as the cause, so
  a cut read counts as transient and a cut write still runs once.
- **A failed revocation warns.** `revoke` writes a `::warning` and the sweep goes
  on. The installation token expires within the hour on its own.
- **A moved pull request is a note.** When the forge refuses a merge or an
  enqueue as not mergeable (compared in lower case), `moved_state` reads the live merge state
  once more. If the state is no longer one the caller accepts, the run writes
  "not landed: the merge state moved to STATE" and the next run serves the pull
  request from its new state. If the state is still accepted, the refusal
  stands.
- **The release pull request gets a second attempt.** The first
  release-please step carries `continue-on-error: true`. When it fails, the job
  waits 30 seconds and runs the same step once more. Only the second attempt can
  fail the job.

## Consequences

- A forge outage longer than about ten seconds still fails the run; the retry
  covers short gateway errors only.
- A revocation that fails leaves a live token for up to an hour. It is scoped to
  one repository and the train's rights, and the warning names the failure.
- A merge that the forge refused while the state stayed accepted is still red,
  so a real refusal is not hidden.
- The release-please step runs twice on a real error, such as a wrong config
  file; the second failure is the one reported.

## References

- Planted cases in `tests/test_merge_train.py` (gateway errors and a dropped
  connection on the jobs read, exhausted retries, a 500 that is not tried again,
  a failed revocation, a merge refused after the state moved) and
  `tests/test_release_labels.py` (the second release attempt).
