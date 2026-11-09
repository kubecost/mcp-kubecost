# Review: `.github/workflows/pr-agent.yml` (staged changes on `update-dependencies2`)

## Summary

**Ready to merge as-is: NO.** Two blockers, one of them already visible in production runs on `main`.

1. **Blocker (new in the staged diff): automatic reviews can never run.** The new `preflight` step hard-codes
   `['Build and Test PR', 'Validate Routes Against OpenAPI Specs']` (lines 111-119). Neither name exists in this repo
   (`grep` finds them only in `pr-agent.yml`). Preflight therefore always ends with
   "Waiting for Build and Test PR to run on this head." and returns `run=false`. The `workflow_run.workflows` list on
   line 38 is correct, so the workflow fires and then silently no-ops every time.
2. **Blocker (already on `main`, not caused by the diff): `kubecost/kc-pr-agent@v0.47` cannot be resolved from this repo.**
   The only run that reached the agent step failed in "Set up job" with
   `Unable to resolve action 'kubecost/kc-pr-agent', not found` (run 39288157799). `mcp-kubecost` is **public**;
   `kc-pr-agent` is **internal** (`visibility: internal`, actions access `enterprise`). Public repos cannot consume
   actions from internal repos. Even with the names fixed, the agent would not start.

The rest is medium/low. The file parses and `actionlint` is clean, so the problems are semantic, not syntactic.

Also: the diff is a rewrite of the gate (bash job → github-script preflight, `synchronize`/cancel removed), and it is
bundled into a dependency-update branch (`Dockerfile`, `pyproject.toml`, `uv.lock` are also staged). Consider a
separate commit/PR.

## What the staged diff actually changes (vs HEAD, which equals `origin/main` for this file)

- Removes the `gate` job (bash + `gh api`) and its `needs:`; folds the decision into a `preflight` github-script step
  inside `pr_agent_job`.
- Removes `pull_request: synchronize` and the workflow-level cancel-in-progress concurrency; adds a job-level
  `concurrency` group (serialize, never cancel) plus a marker-comment dedupe (`<!-- pr-agent-reviewed:N:SHA -->`).
- Adds a "Recheck head" step and a "Record completed automatic review" step.
- Adds `actions: read` to the agent job; `reason` output; simplifies `report`.
- Flips variable precedence to `KC_PR_AGENT_*` first, legacy `AWS_*` second (the committed version, and the upstream
  template `pr-agent-action-example.yml`, prefer the unprefixed names first).
- The committed (HEAD) version used the correct local names (`MONITORED_WORKFLOWS: '["ci", "Helm chart validation", "Docker build", "Check spelling"]'`);
  the staged rewrite regressed that to names from another repo.

## Findings

### Verified

| # | Severity | Location | Finding | Evidence | Suggested fix |
|---|----------|----------|---------|----------|---------------|
| 1 | **blocker** | L111-L123 (and comment L110-111) | Monitored names match no workflow in this repo, so preflight never passes. | `name:` values here: `ci` (ci.yml:1), `Helm chart validation` (helm.yml:1), `Docker build` (docker-build.yml:18), `Check spelling` (check-spelling.yml:1), `auto-fix formatting`, `require version label`, `backport`, `release pipeline`, `Generate SBOM`, `integration-ci-cd trigger`. `grep "Build and Test PR\|Validate Routes"` hits only pr-agent.yml. L118-120 requires a run named `Build and Test PR`, otherwise `decide(false, 'Waiting for ...')`. Since the agent job then ends `success` (steps skipped), `report` does not run (`result != 'success'` is false) — **no visible failure, only a job summary**. | Replace with local names. Required (always run on every PR): `ci`, `Helm chart validation`, `Check spelling`. Optional (path-filtered, may be absent): `Docker build`. Change L118 check to require all three required names (e.g. a `required` array), and L119 message accordingly. Keep L38 and the monitored list identical. |
| 2 | **blocker** | L171, L229 | Action `kubecost/kc-pr-agent` is internal; this repo is public. | `gh api repos/kubecost/mcp-kubecost` → `visibility: public`; `gh api repos/kubecost/kc-pr-agent` → `visibility: internal`, `actions/permissions/access` → `enterprise`. Run 39288157799: job "Run PR Agent commands" failed at "Set up job"; annotation `Unable to resolve action 'kubecost/kc-pr-agent', not found`. Of the 30 most recent runs, the agent step was never executed (29 skipped, 1 failed this way). | Needs an owner decision, not a workflow tweak: (a) make `kc-pr-agent` public (its image is already on public ECR: `public.ecr.aws/u8u5i0e2/kc-pr-agent:v0.47.11`), or (b) call the public ECR image directly (`uses: docker://public.ecr.aws/u8u5i0e2/kc-pr-agent:<tag-or-digest>`; loses the wrapper's inputs/`agent-version` output), or (c) run this workflow only from private/internal repos. Could not verify that (b) works with the wrapper's env contract. |
| 3 | medium | L171, L229 | `kubecost/kc-pr-agent@v0.47` is a moving tag; every other third-party action in the repo is pinned to a full commit SHA with a version comment (e.g. `actions/github-script@3a2844b7… # v9.0.0` in backport.yml, docker-build.yml; `aws-actions/configure-aws-credentials@e1253824… # v6.3.0` in release.yml:807). The action's own `action.yaml` says "Pin this wrapper with an action commit SHA". | `git/ref/tags/v0.47` → `6bfb088e37a83696f5a760886173b2a81fbd243e`, same SHA as `v0.47.11` (latest 0.47.x). | `uses: kubecost/kc-pr-agent@6bfb088e37a83696f5a760886173b2a81fbd243e # v0.47.11`; update `ACTION_REF` on L229 to match (ideally derive from one place). Dependabot (`github-actions`, weekly, grouped, `/`) will then maintain it **if** it can read the repo; internal-repo access for Dependabot is unverified. |
| 4 | medium | L54-60, L66-71 | `workflow_run` fan-out and `cancel-in-progress: false`: GitHub keeps one running + one *pending* run per group and cancels older pending ones. With four listed workflows completing close together, some `pr_agent_job`s are cancelled, which makes `report` run (`result != 'success'`) and print "Agent cancelled" noise. | Concurrency semantics (GitHub docs); not reproduced in this repo because the agent has never run. | Acceptable by design (last pending wins and rechecks marker), but either treat `cancelled` as quiet in `report` (`needs.pr_agent_job.result != 'cancelled'`), or reduce fan-out by listing only required workflows (`ci`, `Helm chart validation`, `Check spelling`) in `workflow_run`. |
| 5 | medium | L2, L4, L22-L28 header | Header is the upstream template, not repo docs: "Copy to … in the consuming repository", "(see README.md)", "Replace the workflow names with your repository's test workflows". README.md, docs/development/*.md, AGENTS.md mention neither PR-Agent nor any `KC_PR_AGENT_*` variable (`grep -i pr-agent|KC_PR_AGENT|bedrock` over *.md → nothing). Also L26-27 has an odd wrap ("those workflows must run on" / "pull_request"). | — | Rewrite the header for this repo (what it does, which org variables it needs, where the role is defined, how to run `/review`), drop "Copy to…" and "see README.md", or add a short section to `docs/development/README.md`. |
| 6 | medium | L141-146 | Legacy `AWS_*` fallbacks are dead weight here and collide by name with an unrelated var: the repo defines `AWS_ROLE_ARN` (release ECR publish, release.yml:809) while this file falls back to `AWS_ROLE_TO_ASSUME`/`AWS_REGION`/`AWS_MODEL_ARN`… which are not defined at repo level. Org vars `KC_PR_AGENT_AWS_{ROLE_TO_ASSUME,REGION,MODEL_ARN,REASONING_MODEL_ARN,WEAK_MODEL_ARN,FALLBACK_MODEL_ARN}` exist with visibility `all` (also `KC_PR_AGENT_AWS_OPUS_ARN`, unused). If neither role var is set, `role-to-assume` is empty and `configure-aws-credentials` fails with no credentials (job fails; `report` explains). | `gh api orgs/kubecost/actions/variables`; `gh api repos/kubecost/mcp-kubecost/actions/variables` → `AWS_ROLE_ARN`, `RELEASE_APP_ID` only. | Drop the legacy fallbacks (and the "old unprefixed names" header paragraph), keeping only `KC_PR_AGENT_*`. Region default `us-west-2` is fine. |
| 7 | medium | L38, job `if` L54-60 | Dependabot PRs are frequent (`dependabot.yml` has uv + github-actions weekly; see run 39286460574 for a dependabot branch). GitHub gives Dependabot-triggered runs (including `workflow_run` chained from them) a read-only token and no secrets/OIDC, so `configure-aws-credentials` and the marker comment would fail. | GitHub docs for Dependabot-triggered workflows; **could not verify with a live run** because the agent never ran. | Add `github.event.workflow_run.actor.login != 'dependabot[bot]'` and `github.actor != 'dependabot[bot]'` to the automatic branches (same idea as `require-version-label.yml:28`), or accept failures. |
| 8 | low | L52-60 + `report` L215-217 | Every `issue_comment` on any issue/PR and every `workflow_run` from non-PR events (push to main for Helm/Docker) creates a PR-Agent run, skips `pr_agent_job`, and then runs `report` (`result == 'skipped'`). Run history shows ~29 of 30 runs are exactly this. | `gh run list --workflow pr-agent.yml` | Reduce noise: filter at trigger level where possible (`workflow_run` `branches-ignore: [main]` — note it filters on the triggering run's head branch), and make `report` skip the "not eligible" cases (only report `failure`/`cancelled`, or only comment-command skips). |
| 9 | low | L74, L236, L252-253 | `reason` output is set by `decide()` even on `run=true`, so a *failed* agent run reports the reason "The current head has finished its monitored workflows and needs a review." as if it explained the failure. When preflight returns `run=false` the job is `success`, so `report` never shows the reason (it is only in the job summary). | code reading | Only export `reason` for `run=false`, or label it "Preflight said:" in `report`. |
| 10 | low | auto-fix-formatting.yml (checkout default token + `git push`) | After an auto-fix commit the PR head moves to a SHA that has no `ci`/Helm/spelling runs (pushes by `GITHUB_TOKEN` do not trigger workflows). Old runs hit "triggering commit is no longer the PR head"; no `workflow_run` ever fires for the new head, so no automatic review for that head. | auto-fix-formatting.yml lines 16-19, 35-45 | Document; users can run `/review`. No change needed unless you want a `PAT/app` token in auto-fix. |
| 11 | low | L141-L168 ordering | AWS credentials are minted before the "Recheck head" step, so a stale-head skip still assumes the role. | code reading | Move "Recheck head" before "Configure AWS Credentials". |
| 12 | low | L189-L197 | `GITHUB_ACTION_CONFIG.AUTO_*: true` are redundant (runner treats unset as "run", `github_action_runner.py` `auto_x is None or is_true(...)`), and are the only unquoted values in the env block. Dotted keys are right: the runner reads `GITHUB_ACTION_CONFIG.AUTO_REVIEW` etc. via `get_setting_or_env`, and Actions stringifies booleans. | kc-pr-agent @v0.47 `pr_agent/servers/github_action_runner.py` L433-L441, L565-L573 | Keep as documentation, or quote (`"true"`) for consistency. |
| 13 | low | L200-L210 | Marker comment adds an extra bot comment per reviewed head, on top of PR-Agent's own comments (`PUBLISH_DESCRIPTION_AS_COMMENT`). | code reading | Acceptable; could be a hidden/edited single comment or a commit status/check instead. |
| 14 | low | cspell | `.github/workflows/**` is **not** in `check-spelling.yml`'s `files:` list (README, docs, src/…, charts), so the CI spell check will not see it. Local `cspell -c .github/cspell.json` flags `commenters`, `pragent` (×3), `IMDS`. | `cspell` run | Nothing required for CI; add `pragent`, `IMDS` to `.github/cspell.json` if workflows are ever added to the spell-check scope (auto-fix sorts the list). |
| 15 | nit | whole file | Style differences vs. repo workflows: other files use `name: …` lowercase, top-level `permissions: contents: read`, workflow-level `concurrency: ${{ github.workflow }}-${{ github.ref }}`. This file uses `permissions: {}` (stricter, good) and a per-PR job-level group (correct: for `workflow_run`, `github.ref` is the default branch and would serialize all PRs together). `timeout-minutes` is set on every job here, while only `ci.yml`'s `integration` job sets one elsewhere (improvement). `runs-on: ubuntu-latest` matches. | — | None required. |

### Checks that passed (verified)

- **Parses**: `yaml.safe_load` OK; **`actionlint` (installed at /opt/homebrew/bin) reports no issues** on both the staged and HEAD versions.
- **`workflow_run.workflows` (L38)** names are exact, case-sensitive matches: `ci`, `Helm chart validation`, `Docker build`, `Check spelling`.
- **Which of them run on `pull_request`**: `ci` (opened/synchronize/reopened; also `workflow_call` from release.yml, which fires `workflow_run` under the caller's name, not `ci`), `Helm chart validation` (opened/synchronize + push main), `Check spelling` (opened/synchronize), `Docker build` (pull_request **with path filters** + push main; absent on docs/workflow-only PRs). Not `pull_request`: `release pipeline` (dispatch), `Generate SBOM` (tag/dispatch/call + path-filtered PR to main only), `integration-ci-cd trigger` (tag/call), `backport` (pull_request_target on main), `require version label` (PR to main, includes `ready_for_review`), `auto-fix formatting` (PR, pushes commits). Leave out `release`, `sbom`, `kicker`, `backport`, `auto-fix formatting`; `Docker build` should be *optional* (present → wait for it; absent → don't block), mirroring how the code treats route validation.
- **Action SHAs**: `actions/github-script@3a2844b7…` v9.0.0 and `aws-actions/configure-aws-credentials@e1253824…` v6.3.0 are identical to the pins used in backport.yml/docker-build.yml and release.yml. `dependabot.yml` has `github-actions` on `/`, weekly, grouped, so these are maintained.
- **Permissions**: top-level `permissions: {}`; agent job `actions: read` (needed for `listWorkflowRunsForRepo`), `contents: read`, `id-token: write`, `issues: write` (comments), `pull-requests: write`; all used and no more. `report` job `actions: read` is sufficient (`listJobsForWorkflowRun`, `core.summary`).
- **Fork safety**: preflight compares `pr.head.repo.full_name` to this repo and requires open/non-draft/head-SHA match (L102-L108); job `if` requires `workflow_run.event == 'pull_request'` and a non-empty `pull_requests[0].number` (empty for fork PRs). The runner also skips `workflow_run` without `pull_requests` (`github_action_runner.py` L545-L548).
- **issue_comment**: allow-list `OWNER/MEMBER/COLLABORATOR`, not Bot, body contains `/`, must be a PR comment. The job has **no `actions/checkout`**; the agent talks to the PR through the API only, so PR code is never checked out or executed. issue_comment uses the default branch's workflow file.
- **Guards for issue_comment in later steps**: `Recheck head` returns `run=true` early for `issue_comment` (L150-L153); `Record completed automatic review` is guarded by `github.event_name != 'issue_comment'` (L201), so the empty `number/sha/marker` outputs are never used. Correct.
- **ready_for_review**: preflight reads `event.pull_request.number/head.sha` correctly for that event; the concurrency group `automatic-<N>` is shared with `workflow_run` runs of the same PR so they serialize, and the marker dedupes.
- **`ci.yml`** has job names `test` and `integration`, but `workflow_run` and preflight match the **workflow** `name:` (`ci`), which is what the file uses. `integration` hits the public demo and is part of the same `ci` run, so `ci` completing means both are done.
- **`outputs.agent-version`**: the action does expose it (`kubecost/kc-pr-agent` @v0.47 `action.yaml`: `outputs: agent-version`). Fine.
- **CHANGELOG / AGENTS.md**: this is a ci-only change; AGENTS.md says to skip CHANGELOG for ci-only changes, and to commit only when asked. Nothing is committed (changes are staged only). If this lands in a PR to `main`, `require-version-label` needs a `ci-only` label, which conflicts with bundling it with the dependency changes in the same PR (`pyproject.toml`/`uv.lock`/`Dockerfile`), another reason to split.

### Could not verify

- Whether the kubecost enterprise/org policy can be changed so a public repo may consume `kc-pr-agent` (finding 2 evidence is the failed run plus repo visibilities; the exact root cause is inferred from GitHub's rule that public repos can't use actions from internal repos).
- Dependabot behavior for `workflow_run` (finding 7) and whether Dependabot can update an internal action.
- AWS side: role trust policy must accept all three OIDC subjects used here (`workflow_run`/`issue_comment` → `repo:kubecost/mcp-kubecost:ref:refs/heads/main`; `pull_request` → `repo:kubecost/mcp-kubecost:pull_request`), and Bedrock permissions (`InvokeModel`, `GetInferenceProfile`, `ListInferenceProfiles`).
- `CONFIG__FALLBACK_MODELS` is a plain ARN string via a folded `>-` scalar; the folded multi-line `${{ a || b || c || d }}` evaluates fine (actionlint clean), but whether the runtime expects a list is a kc-pr-agent concern. `AWS_USE_IMDS: "true"` and `CONFIG__CUSTOM_MODEL_MAX_TOKENS: "1000000"` are identical to the upstream template; their effect was not tested.
- The agent step has never executed on this repo, so none of the runtime behavior (describe/review/improve, marker write, `report` output) has been observed end to end.

## Recommended order of fixes

1. Resolve the action-access problem (finding 2) — without it nothing else matters.
2. Replace the monitored names (finding 1) with `ci`, `Helm chart validation`, `Check spelling` (required) and `Docker build` (optional), keeping the L38 list identical.
3. Pin `kc-pr-agent` to `6bfb088e37a83696f5a760886173b2a81fbd243e # v0.47.11` (finding 3).
4. Rewrite the header and drop legacy `AWS_*` fallbacks (findings 5, 6).
5. Decide on Dependabot exclusion and `report` noise (findings 7, 8, 9, 4).
6. Land it separately from the dependency bump with a `ci-only` label.

No files other than this report were modified.
