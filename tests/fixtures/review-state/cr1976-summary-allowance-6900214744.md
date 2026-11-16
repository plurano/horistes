<!-- coderabbit-ai-generated:v1 -->

<!-- This is an auto-generated comment: summarize by coderabbit.ai -->
<!-- review_stack_entry_start -->

<a href="https://app.coderabbit.ai/change-stack/cordanaLLM/praetor/pull/1976?scope=ghh_OusAJGA4HG8ljoOV0wJvVx8NmnZIjc2DdjmxOu0xZWA&amp;cs_source=review_comment"><picture><source media="(prefers-color-scheme: dark)" srcset="https://storage.googleapis.com/coderabbit_public_assets/review-stack-buttons-v1/explain-change-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://storage.googleapis.com/coderabbit_public_assets/review-stack-buttons-v1/explain-change.svg"><img src="https://storage.googleapis.com/coderabbit_public_assets/review-stack-buttons-v1/explain-change.svg" alt="Explain Change →" height="32"></picture></a>

<!-- review_stack_entry_end -->
<!-- recent_review_status_start -->

No actionable comments were generated in the recent review.

<!-- recent_review_status_end -->
<!-- walkthrough_start -->

<details>
<summary><strong>Walkthrough</strong></summary>
<dl>
<dd>
<br>

Adoption now manages a catch-all CodeRabbit path instruction about `make verify-all` CI evidence and coding-agent turn-end rules. It preserves existing global instructions and validates path-instruction entries before adding the default.

**Changes: CodeRabbit configuration adoption**

|Layer / File(s)|Summary|
|:---|:---|
|**Default instruction and adoption contract** <br> `.coderabbit.yaml`, `internal/adopt/testdata/emitted/.coderabbit.yaml`, `docs/adoption.md`|The configuration and emitted fixture include a catch-all path instruction. The adoption documentation describes when it is added and how malformed entries or overlong instructions are handled.|
|**Merge and validate path instructions** <br> `internal/adopt/coderabbit.go`, `internal/adopt/manifest_text.go`, `internal/adopt/coderabbit_test.go`|The merge logic selects the YAML location, preserves an existing global instruction, and validates entry fields and limits. YAML sequence patching encodes appended nodes while retaining their structure. Tests cover layouts, comments, idempotence, and boundary cases.|

<!-- change_assessment_start -->
**Estimated code review effort:** 3 (Moderate) | ~20 minutes

<!-- change_assessment_commit:"793a759aea1c963feace48e67e6cf3590a37e3dd" -->
<!-- change_assessment_priority:{"version":1,"urgency":"p3","manual":false} -->
**Change:** Feature
<!-- change_assessment_end -->

**Possibly related PRs**

- [cordanaLLM/praetor#1825](https://github.com/cordanaLLM/praetor/pull/1825): Adds managed auto-review settings that this change extends with `path_instructions`.

</dd>
</dl>
</details>

<!-- walkthrough_end -->
<details>
<summary><strong>Merge Risk:</strong> <strong><picture><img src="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/risk-unassessed-16.svg" width="16" height="16" align="absmiddle" alt=""></picture> Minimal</strong> · up to <code>793a7</code></summary>
<dl>
<dd>
<br>

<!-- final_review_risk_start -->
<!-- final_review_risk_label
**Merge Risk:** **⚪ Minimal** · up to `793a7`
-->
<!-- final_review_risk_coverage:{"sourceCommitId":"793a759aea1c963feace48e67e6cf3590a37e3dd","coveredCommitId":"793a759aea1c963feace48e67e6cf3590a37e3dd","kind":"reviewed"} -->

Adoption now adds a catch-all CodeRabbit instruction about `make verify-all` CI evidence, keeps user-defined instructions, and refuses malformed or oversized entries with an entry-specific error. The instruction-length limit is enforced and tested at its boundaries. No remaining merge-blocking risk was found.
<!-- final_review_risk_end -->

</dd>
</dl>
</details>
<!-- pre_merge_checks_walkthrough_start -->

<details>
<summary><strong>Pre-merge checks: <sup><picture><img src="https://storage.googleapis.com/coderabbit_public_assets/pre-merge-checks-counter-passed-14-v2.svg" width="14" height="14" align="middle" alt="Passed" title="Passed"></picture></sup> 4</strong></summary>
<dl>
<dd>
<br>

|         Check name         |                                             Status                                             | Explanation                                                                                                                                                                                               |
| :------------------------ | :--------------------------------------------------------------------------------------------: | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
|         Title check        | ![Passed](https://storage.googleapis.com/coderabbit_public_assets/pre-merge-checks-passed.svg) | The title "feat(adopt): CodeRabbit reads the CI verify-all job as evidence" matches the main change. The PR makes the managed .coderabbit.yaml add a global path instruction that treats a green CI veri… |
|      Description check     | ![Passed](https://storage.googleapis.com/coderabbit_public_assets/pre-merge-checks-passed.svg) | The description follows the repository template: it has a summary, the HISS checklist, the 3D testing section, the context-integrity section, and the receipt section. The summary explains the why, the… |
|     Linked Issues check    | ![Passed](https://storage.googleapis.com/coderabbit_public_assets/pre-merge-checks-passed.svg) | Check skipped because no linked issues were found for this pull request.                                                                                                                                  |
| Out of Scope Changes check | ![Passed](https://storage.googleapis.com/coderabbit_public_assets/pre-merge-checks-passed.svg) | Check skipped because no linked issues were found for this pull request.                                                                                                                                  |

<details>
<summary><strong>Full details: Title check</strong></summary>
<dl>
<dd>
<br>

**Explanation**

The title "feat(adopt): CodeRabbit reads the CI verify-all job as evidence" matches the main change. The PR makes the managed .coderabbit.yaml add a global path instruction that treats a green CI verify-all job as evidence, and adds the adoption logic and tests that support it. The title is specific and uses a conventional-commit scope.

</dd>
</dl>
</details>

<details>
<summary><strong>Full details: Description check</strong></summary>
<dl>
<dd>
<br>

**Explanation**

The description follows the repository template: it has a summary, the HISS checklist, the 3D testing section, the context-integrity section, and the receipt section. The summary explains the why, the what, the adoption behavior, the docs update, and the 20,000-character limit. The receipt block is filled with a JSON receipt under the labelled fence. Some items are still unverified, including whether the receipt certifies the current head commit and whether the HISS-13 debt ratchet was actually run. The author also ticked every checklist box, and the scope of the local race-test run is not clearly stated.

</dd>
</dl>
</details>

</dd>
</dl>
</details>

<!-- pre_merge_checks_walkthrough_end -->
<!-- finishing_touch_checkbox_start -->

<details>
<summary><strong>Finishing Touches</strong></summary>
<dl>
<dd>
<br>

<details open>
<summary><strong>Generate unit tests (beta)</strong></summary>
<dl>
<dd>
<br>

- [ ] <!-- {"checkboxId": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "radioGroupId": "utg-output-choice-group-6914960558"} --> Commit to this branch
- [ ] <!-- {"checkboxId": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "radioGroupId": "utg-output-choice-group-6914960558"} --> Create a new PR

</dd>
</dl>
</details>

</dd>
</dl>
</details>

<!-- finishing_touch_checkbox_end -->

<!-- autopilot:start -->
- [ ] <!-- {"checkboxId":"2708ad07-9f24-4260-9c11-7dc76a49f2e3"} --> <strong title="Keep fixing CodeRabbit findings and required CI, and resolving merge conflicts">Autofix</strong> · Keep fixing CodeRabbit findings and required CI, and resolving merge conflicts
<!-- autopilot:end -->
<!-- tips_start -->



Thanks for using [CodeRabbit](https://coderabbit.ai?utm_source=oss&utm_medium=github&utm_campaign=cordanaLLM/praetor&utm_content=1976)! It's free for OSS, and your support helps us grow. If you like it, consider giving us a shout-out.

<details>
<summary><strong>Share</strong></summary>
<dl>
<dd>
<br>

- [X](https://twitter.com/intent/tweet?text=I%20just%20used%20%40coderabbitai%20for%20my%20code%20review%2C%20and%20it%27s%20fantastic%21%20It%27s%20free%20for%20OSS%20and%20offers%20a%20free%20trial%20for%20the%20proprietary%20code.%20Check%20it%20out%3A&url=https%3A//coderabbit.ai)
- [Mastodon](https://mastodon.social/share?text=I%20just%20used%20%40coderabbitai%20for%20my%20code%20review%2C%20and%20it%27s%20fantastic%21%20It%27s%20free%20for%20OSS%20and%20offers%20a%20free%20trial%20for%20the%20proprietary%20code.%20Check%20it%20out%3A%20https%3A%2F%2Fcoderabbit.ai)
- [Reddit](https://www.reddit.com/submit?title=Great%20tool%20for%20code%20review%20-%20CodeRabbit&text=I%20just%20used%20CodeRabbit%20for%20my%20code%20review%2C%20and%20it%27s%20fantastic%21%20It%27s%20free%20for%20OSS%20and%20offers%20a%20free%20trial%20for%20proprietary%20code.%20Check%20it%20out%3A%20https%3A//coderabbit.ai)
- [LinkedIn](https://www.linkedin.com/sharing/share-offsite/?url=https%3A%2F%2Fcoderabbit.ai&mini=true&title=Great%20tool%20for%20code%20review%20-%20CodeRabbit&summary=I%20just%20used%20CodeRabbit%20for%20my%20code%20review%2C%20and%20it%27s%20fantastic%21%20It%27s%20free%20for%20OSS%20and%20offers%20a%20free%20trial%20for%20proprietary%20code)

</dd>
</dl>
</details>

<!-- tips_end -->
<!-- recent_review_start -->

<h6>
<details>
<summary><strong><picture><img src="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/info.svg" width="12" height="12" align="absmiddle" alt=""></picture> Recent review info</strong></summary>
<dl>
<dd>
<br>

<!-- review_info_content -->
<details>
<summary><strong><picture><source media="(prefers-color-scheme: dark)" srcset="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/cog-dark.svg"><img src="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/cog.svg" width="16" height="16" align="absmiddle" alt=""></picture> Run configuration</strong></summary>
<dl>
<dd>
<br>

- **Source mode**: Auto
- **Repository**: [cordanaLLM/praetor](<https://github.com/cordanaLLM/praetor>)
- **Branch**: [feat/coderabbit-ci-evidence](<https://github.com/cordanaLLM/praetor/tree/feat%2Fcoderabbit-ci-evidence>)
- **File**: [.coderabbit.yaml](<https://github.com/cordanaLLM/praetor/blob/793a759aea1c963feace48e67e6cf3590a37e3dd/.coderabbit.yaml>)
- **Commit**: [793a759](<https://github.com/cordanaLLM/praetor/commit/793a759aea1c963feace48e67e6cf3590a37e3dd>)
- **Layers**: Repository YAML
- **Review profile**: CHILL
- **Plan**: Advanced
- **Run ID**: `f7650267-ba0b-429c-9413-12802e7a3dbd`

</dd>
</dl>
</details>

<details>
<summary><strong><picture><source media="(prefers-color-scheme: dark)" srcset="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/git-commit-horizontal-dark.svg"><img src="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/git-commit-horizontal.svg" width="16" height="16" align="absmiddle" alt=""></picture> Commits</strong></summary>
<dl>
<dd>
<br>

Reviewing files that changed from the base of the PR and between 1c667f35101a466145152ca0cfcab37d42889929 and 793a759aea1c963feace48e67e6cf3590a37e3dd.

</dd>
</dl>
</details>

<details>
<summary><strong><picture><source media="(prefers-color-scheme: dark)" srcset="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/file-plus-corner-dark.svg"><img src="https://storage.googleapis.com/coderabbit_public_assets/review-comment-icons-v1/file-plus-corner.svg" width="16" height="16" align="absmiddle" alt=""></picture> Files selected for processing: 1</strong></summary>
<dl>
<dd>
<br>

* `internal/adopt/coderabbit_test.go`

</dd>
</dl>
</details>

**Included review availability:** This review used your included allowance. Your plan provides up to 1 included review per hour; 0 remain after this review.

</dd>
</dl>
</details>
</h6>

<!-- recent_review_end -->
