# Final consolidated issues report — patch releases from `release/X.Y` branches with label-driven backports (https://github.com/kubecost/mcp-kubecost/pull/111)

Issues are sorted from highest to lowest combined importance/confidence.

---

## 1. A mixed label set backports nothing

**Severity: High** · *(cluster: backport target resolution — see also #5)*

- **User impact:** A merged fix labelled for two maintenance lines where only one branch exists produces no backport PR at all. The PR comment names only the missing branch, so a maintainer reasonably concludes the resolvable line already went through and the fix never ships there.
- **Issue:** In `.github/workflows/backport.yml` the target-resolution step (`steps.targets`, ~lines 98-117) writes valid branches to the `branches` output and then calls `core.setFailed()` for any line whose `release/X.Y` branch is missing. A failed step makes the implicit `success()` condition false on the checkout and `korthout/backport-action` steps, so `if: steps.targets.outputs.branches != ''` can never fire after a partial result. This also contradicts `docs/development/release.md`, which documents one PR per labelled line and a missing branch as an explanatory-comment case.
- **Recommendation:** Keep resolution non-fatal: emit `branches` plus a separate `problems` output, let the backport step run for the lines that resolved, and decide failure in a trailing step keyed on `problems` (or downgrade it to a warning to match the documented comment-instead behavior).

## 2. The release-branch bootstrap recipe cannot produce a release

**Severity: High**

- **User impact:** The first patch release cut by following the documented procedure fails at run creation, before any job starts — the exact scenario (shipping v0.17.2 without 0.18.0) that motivates this PR.
- **Issue:** `docs/development/release.md` ("Starting a maintained line") copies only `.github/workflows/release.yml` and `.github/workflows/ci.yml` from `main` onto `release/X.Y`. `release.yml` also calls `./.github/workflows/sbom.yml` and `./.github/workflows/integrations-ci-cd-kicker.yaml` as reusable workflows, and at `v0.17.1` neither declares `workflow_call` (the kicker has only `on: push: tags`; `sbom.yml` has `push`/`workflow_dispatch`/`pull_request`). Local `uses: ./...` references resolve at the dispatched ref, and GitHub validates called workflows when the run is created.
- **Recommendation:** Extend the `git checkout main --` list to all four files (`release.yml`, `ci.yml`, `sbom.yml`, `integrations-ci-cd-kicker.yaml`). Everything else `main`'s `ci.yml` needs already exists at `v0.17.1`, so the doc change is the whole fix.

## 3. The downstream release announcement is not backport-aware

**Severity: High**

- **User impact:** A 0.17.x patch can walk a downstream consumer back from 0.18.x if that consumer pins or deploys the announced tag — the same regression that `--latest=false`, the suppressed `latest` image tag, and the parent-chart skip exist to prevent.
- **Issue:** In `.github/workflows/release.yml`, `notify-integration-ci-cd` forwards `needs.build.outputs.release_tag` into a `repository_dispatch` (`event_type: mcp-kubecost-release`) on `kubecost/integration-ci-cd` with no `is_backport` gating. It is the one publish path that does not consume the `check-release-ref` outputs that every other path does.
- **Recommendation:** Confirm what the consumer does with `client_payload.tag`. If it treats the tag as current, either gate the job on `is_backport != 'true'` or pass `is_backport` (and `line`) in the payload so the consumer can branch.

## 4. Privileged third-party action is pinned to a mutable tag

**Severity: Medium** · *(cluster: third-party backport action — see also #11)*

- **User impact:** A moved or compromised tag would run with a token that can push branches and create or modify pull requests in this repository.
- **Issue:** `.github/workflows/backport.yml` uses `korthout/backport-action@v4.7.0` and hands it an app installation token scoped `permission-contents: write` / `permission-pull-requests: write`. The rest of the workflow deliberately avoids the dangerous `pull_request_target` combination (checkout of `main` only, `merged == true`, targets derived from a `^v(\d+)\.(\d+)$` label grammar, no PR-controlled string in a shell), which leaves the mutable action ref as the new supply-chain exposure.
- **Recommendation:** Pin the reviewed `v4.7.0` commit SHA (with the version in a trailing comment), or record the accepted risk as a tracked follow-up rather than leaving it implicit in the PR description.

## 5. A `vX.Y` label naming main's own line is silently a no-op

**Severity: Medium** · *(cluster: backport target resolution — see also #1)*

- **User impact:** Immediately after cutting `release/0.18` from `v0.18.0`, labelling a PR `v0.18` to patch that line does nothing. The only trace is a `core.info` line in a workflow run nobody is watching, so the fix is believed shipped when it is not.
- **Issue:** `.github/workflows/backport.yml` resolves a label by comparing it with the version in `main`'s `pyproject.toml` and treats "equal or newer than main" as "ships from main, nothing to do". Because the version bump lands as part of the release, main's in-tree version is the last *released* line, so `v0.18` means both "ships from main" and "backport to release/0.18" at the same time. Unlike the missing-branch case, the skip produces no PR comment.
- **Recommendation:** Prefer resolving a label by whether `release/X.Y` exists — a line with a branch is maintained, independent of main's `pyproject.toml`. If the documented semantics stay, at minimum comment on the PR when a label is skipped as at-or-above main.

## 6. The Dependabot exemption keys on the triggering actor

**Severity: Medium** · *(cluster: version-label gate — see also #7, #8)*

- **User impact:** Any human touch on a Dependabot PR (adding a label, pushing to its branch) makes the label check run on a PR that carries no `vX.Y` label, so it fails — and once the check is required it blocks exactly the PRs the exemption was written to cover.
- **Issue:** `.github/workflows/require-version-label.yml` guards with `github.actor != 'dependabot[bot]'`. `github.actor` is the actor of the triggering event, not the PR author, and flips to a human on `labeled`/`unlabeled` and on human pushes.
- **Recommendation:** Key the exemption on `github.event.pull_request.user.login`, which is stable across all trigger types the workflow declares.

## 7. No concurrency group on the version-label check

**Severity: Medium** · *(cluster: version-label gate — see also #6, #8)*

- **User impact:** Removing and re-adding a label starts overlapping runs with no ordering guarantee, so a stale failing run can land after the passing one and leave a correctly labelled PR blocked.
- **Issue:** `.github/workflows/require-version-label.yml` triggers on `labeled` and `unlabeled` but sets no `concurrency` group, unlike `ci.yml` and `release.yml`.
- **Recommendation:** Add a PR-keyed concurrency group with `cancel-in-progress: true`.

## 8. The label gate is advisory until the ruleset names it

**Severity: Medium** · *(cluster: version-label gate — see also #6, #7)*

- **User impact:** Until the repository ruleset requires it, a PR can merge into `main` with no release-line label, so nothing actually guarantees the backport labels that the new workflow and docs depend on. The `ci-only` label must also exist before the check can be satisfied.
- **Issue:** `require-version-label` is a new check with no enforcement wiring; the rollout is explicitly deferred in the PR description. Worth stating alongside it: a `pull_request`-triggered gate runs from the PR's own merge commit, so a PR can edit or delete `require-version-label.yml` and satisfy its own check — acceptable for a process nudge, not an enforcement boundary.
- **Recommendation:** Complete the documented ruleset rollout (create the `ci-only` label, add the check as required on `main`) before treating labels as release intent, and note in the docs that the gate is advisory rather than tamper-proof.

## 9. The new event paths are unexercised and workflow linting is manual

**Severity: Medium**

- **User impact:** The orchestration defects in this PR are precisely the class that isolated regex and tag-baseline checks miss, and nothing stops the next edit to four workflow files from reintroducing them.
- **Issue:** Verification was manual throughout: `actionlint` run by hand, regex and tag-baseline logic exercised in a scratch repo. There is no `actionlint` step in `ci.yml` or in the pre-commit config. Untested: labelling a merged PR after the fact, resolving multiple targets when one branch is missing, an actual dispatch from a `release/X.Y` branch, `--latest=false` / `--notes-start-tag` against a real release, the `sort -V` downgrade guard, and the label check itself.
- **Recommendation:** Add `actionlint` to `ci.yml` or the pre-commit config, give the resolver regression coverage for the four cases (no targets, valid targets, missing targets, mixed) — extracting its decision logic into a testable script makes that durable — and complete the planned dry-run plus post-merge labelling exercises.

## 10. The app token is minted before any target is resolved

**Severity: Low**

- **User impact:** Every merge into `main` runs the backport job and spends an installation token, labelled or not. If the app's installation or permissions are wrong, every merge goes red instead of only labelled ones.
- **Issue:** In `.github/workflows/backport.yml` the token-generation step carries no `if:` and runs ahead of label resolution.
- **Recommendation:** Resolve targets first and gate token generation (and the steps after it) on a non-empty `branches` output.

## 11. Re-adding a label may collide with an existing backport branch

**Severity: Low** · *(cluster: third-party backport action — see also #4)*

- **User impact:** The documented recovery gesture, applied to a line whose backport PR is already open, could force-push over commits added during review of that PR.
- **Issue:** `docs/development/release.md` step 2 tells maintainers to re-add the label, which fires the `labeled` path and re-runs `korthout/backport-action@v4.7.0` against an existing `backport-<n>-to-release-X.Y` branch. Whether it skips, force-pushes, or fails is not established.
- **Recommendation:** Confirm the action's behavior on an existing backport branch and document it in the recovery step, or guard the re-run when a backport PR for that line is already open.

## 12. Main's tag baseline fails open while the release-line path fails closed

**Severity: Low**

- **User impact:** Under a checkout that fetched no tags, a `main` release would be computed from in-tree state rather than release history — the kind of asymmetry found during an incident rather than in review.
- **Issue:** In `.github/workflows/release.yml`, the release-line branch treats an empty `git tag | grep` result as a hard error (`::error::No v${LINE}.* tag found`, `exit 1`), while the main-branch branch now carries `|| true` and falls through to bumping from `pyproject.toml`. The two paths have opposite postures for the same missing input. `fetch-depth: 0` makes it unlikely today.
- **Recommendation:** Make the two paths agree — error on an empty tag list for both, or document explicitly why the main path is allowed to bootstrap from the in-tree version.
