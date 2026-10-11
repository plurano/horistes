<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0014: A release is read back without credentials, by one tool and a parameter file per repository

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

Three repositories of the fleet published a signed release and each read it
back with a script of its own: one for archives with a signed checksums file,
two for images in a registry. Read side by side on 2026-10-11, the scripts
differ in what they leave open, not in what they mean to show.

- **Credentials.** One of the two scripts for images asks the forge's client
  for the attestation, and that client refuses without a login. A read-back
  that needs the publisher's login shows what the publisher can see, not what
  a user can.
- **Identity.** The same script expects the signer and the ref and not the
  commit, so a tag moved to another commit and signed again would still
  pass. The other two expect the commit as well.
- **Controls.** The script for archives repeats each check with one
  expectation changed and requires a refusal, nine times for the signature
  and seven for the provenance. The second script for images has one control
  per check, the first has none. A verification that was never seen to
  refuse proves little: the expected identity may be a pattern that matches
  everything.
- **Written per release.** Two of the three scripts carry the repository,
  the tag, the commit and the names of the artefacts in their text, so the
  next release is an edit of the script.
- **The exit code alone.** `cosign verify` of v2.6.3 answers for a signed
  image with two entries, one of the signature type and one of the provenance
  type. Both scripts for images drop that answer and keep the exit code.
  Whether the tool exits 0 for an image that carries an attestation and no
  signature is not measured here; a read-back should not depend on it.
- **Assets nobody checks.** No script compares the assets of the release page
  with what it verified, and each of the three releases holds at least one
  asset that no signature, checksum or attestation covers.

## Decision

- `tools/readback.py verify --params FILE --tag TAG` reads one release back.
  It is one shape for archives and images; a repository writes a parameter
  file (repository, signer workflow, checksums and signature asset, image
  references) and no code. Unknown keys are refused, and there is no key
  that switches a check off.
- Every request and every command goes through `tools/stranger.py`: a request
  carries no authorisation header, a command gets the search path and a home
  folder made for the run. The forge's client is not used; attestations come
  from the forge's public API and are verified by the signature tool.
- The commit of the tag is asked from the forge with `git ls-remote` at the
  time of the read-back. Every signature and attestation must name the signer
  workflow at the tag as identity, and the repository, the ref and that
  commit as claims of the certificate.
- Each accepted check is repeated with one expectation changed, and every
  such call must be refused: another tag, the branch `main`, the signer's
  path in another repository, another commit, another repository and another
  ref in the certificate, another issuer, another workflow of the repository
  in the signer's place. For archives also an altered file, a file that is no
  subject, and the signature bundle offered as provenance. A control that
  gets no answer, because the tool did not start or ran out of time, fails;
  it never counts as a refusal.
- The assets of the release are exactly the checksums, their signature, the
  listed files and the files the parameter file declares `unverified`. An
  asset more or less fails. A declared file is named in a `NOTE` line of
  every run, with the digest the forge records.
- An image passes only when the answer holds an entry of the signature type
  that names one digest. The provenance is then asked for by that digest, and
  its statement must name it.
- Exit code 1 is a judgement on the release; exit code 3 means the read-back
  could not be made (parameters, tag, rate limit, missing tool) and says
  nothing about the release.

## Consequences

- **Positive**:
  - A repository adds a release read-back by writing a file of a few lines;
    the controls come with the tool and cannot be left out.
  - A tag that was moved after the release fails, because the certificate
    names the commit that was signed.
  - The gap of each release is written down: `snapshot.schema.json` of
    tekmora/epitrop v0.1.0-rc.2, the chart archive and two bills of materials
    on the release page of lusoris/SnatchArr v0.1.5, two bills of materials
    of tekmora/diallak v0.1.2.
- **Negative**:
  - The forge's API answers 60 requests an hour per address without
    credentials, shared by everything behind that address. A release costs
    one request for its assets and one for each attestation its listed files
    need. A refusal for that reason is exit code 3.
  - Of an image with several platforms the index is verified, not each
    platform's image.
  - The tool reads GitHub releases, certificates of GitHub Actions, bundles
    in the new format and SLSA provenance v1. Another forge or format needs a
    change of the tool, not a parameter.
  - `unverified` names a gap and does not close it. A file declared there can
    be replaced on the release page without a failing line; only the digest
    in the `NOTE` line changes.
  - The signature tool fetches Sigstore's trust root into the empty home on
    every run, so a run needs that host besides the forge and the registry.
  - There is no workflow form. This repository is private, so no consumer
    could call one, and its hosts would need a declaration under
    [ADR-0003](0003-declared-hosts.md); the tool runs wherever Python, `git`
    and `cosign` are.
- **Neutral**: the tool was accepted on the three releases named above. Each
  passes with every control refused; the logs are not part of the tree.

## References

- Fleet answer ledger, id shared-ci-readback-three-releases, 2026-10-11
  (paraphrased): the tool is accepted on three releases of three
  repositories, archives, two images with a chart, and one image from a
  signed tag; a signature or provenance missing at one of them is a finding
  the tool states for that repository, not a failure of the tool.
- Fleet controller's brief for this unit, 2026-10-11 (paraphrased): one
  script that verifies a published release without credentials, with
  digests, the signature against the exact workflow identity at the tag,
  provenance per subject and planted negatives; one shape for archives and
  images, parameters per repository only.
- sigstore/cosign v2.6.3, run 2026-10-11: `verify`, `verify-attestation`,
  `verify-blob` and `verify-blob-attestation` with
  `--certificate-github-workflow-repository`, `-ref` and `-sha`; the answer
  of `verify --output json` for `ghcr.io/lusoris/snatcharr-api:0.1.5` holds
  an entry of type `https://sigstore.dev/cosign/sign/v1` and one of type
  `https://slsa.dev/provenance/v1`.
- GitHub REST API, asked without credentials on 2026-10-11:
  `GET /repos/{owner}/{repo}/releases/tags/{tag}` gives each asset with its
  recorded digest; `GET /repos/{owner}/{repo}/attestations/sha256:{digest}`
  gives the bundles for a digest and 404 where there is none;
  `GET /rate_limit` gives a limit of 60.
- `tests/test_readback.py`: one good release, and one planted defect per
  rule, among them a verifier that accepts everything (every control fails)
  and a verifier that gives no answer (nothing counts as refused).
- `tests/test_stranger.py`: a command sees no token and an empty home; a
  request carries no authorisation header.
- `tests/fixtures/readback/`: the parameter files of the three releases.
