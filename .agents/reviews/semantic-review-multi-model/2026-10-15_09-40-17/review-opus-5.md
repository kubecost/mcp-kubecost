# Patch releases from `release/X.Y` and label-driven backports

The release pipeline grows a second legal dispatch source: a `release/X.Y` branch, restricted to patch bumps, with a tag baseline scoped to that line so a 0.17 patch is never computed from a 0.18 tag. Everything that would let an old line masquerade as current is gated on a new `is_backport` output — the `latest` image tag, GitHub's "Latest release" badge, the release-notes start tag, and the parent chart PR (now opt-in via a `parent-branch` input, with a `sort -V` guard that refuses to move `kubecost/kubecost` backwards). Alongside it, a `backport` workflow turns a `vX.Y` label on a merged PR into a cherry-pick onto `release/X.Y`, and a `require-version-label` check makes every PR into `main` declare which lines it ships in or opt out with `ci-only`. The motivating case is shipping #104 as v0.17.2 without releasing 0.18.0.

Watch for: the documented recipe for cutting `release/X.Y` copies only `release.yml` and `ci.yml` from main, but `release.yml` also calls `sbom.yml` and `integrations-ci-cd-kicker.yaml` as reusable workflows, and neither declares `workflow_call` at `v0.17.1` (confirmed) — the first backport release would fail at startup. A label set containing one resolvable and one unresolvable line creates no backport at all (confirmed). `notify-integration-ci-cd` announces a backport tag downstream with no backport gating, unlike every other publish path (likely). And a `vX.Y` label that names main's own line is silently dropped, which is exactly the label you would reach for right after cutting `release/X.Y` from main's current version (likely).

**Verdict**: NEEDS_CHANGES

## High-level view

`check-release-ref` computes `is_backport` and `line` once and four downstream decisions consume them as `needs` outputs: the tag baseline, the `latest` image tag, the GitHub Release badge and notes range, and the parent chart PR. One publish path was missed. `notify-integration-ci-cd` fires a `mcp-kubecost-release` repository dispatch carrying the backport tag unconditionally, so whatever `integration-ci-cd` does with `client_payload.tag` happens for 0.17.2 the same way it happened for 0.18.0.

On a release line the tag baseline is anchored to `^v0\.17\.[0-9]+$` and a missing tag is a hard error. The same step's main-branch path gained a `|| true` that makes an empty tag list fall through to bumping from the in-tree version instead of failing, so the two paths now have opposite failure postures for the same missing input.

The label vocabulary carries more weight than it can bear. A `vX.Y` label means "ships on the X.Y line", and the backport workflow resolves it by comparing against the version in `main`'s `pyproject.toml`: strictly older than main means backport, equal or newer means "ships from main, nothing to do". Because main's in-tree version is the last *released* version, not the next one, the moment you cut `release/0.18` from `v0.18.0` the label `v0.18` is simultaneously "ships from main" and "backport to release/0.18", and the code picks the former with nothing but a log line. The same `core.info`-only treatment applies to every at-or-above-main label, so the no-op is invisible on the PR.

Partial failure in target resolution is the sharpest concrete bug. The script collects resolvable branches and unresolvable ones, comments on the PR about the latter, then calls `core.setFailed` — which fails the step and skips the checkout and backport steps that would have handled the branches that *did* resolve. Labeling a PR `v0.17` and `v0.16` when only `release/0.17` exists backports nothing.

`backport.yml` avoids the one combination that makes `pull_request_target` dangerous: write-scoped credentials never meet PR-head code, because the job checks out only main and requires `merged == true`. The residual exposure is that label permission alone now triggers a branch push and PR creation, and that the app token is minted before anything has been resolved, so every merge into main runs the job and spends a token whether or not there is a label.

The enforcement gate has two properties worth knowing before it becomes a required check: the Dependabot exemption keys on `github.actor`, which is the person who added a label rather than the PR author, so any human touch on a Dependabot PR makes the check run and fail; and there is no concurrency group, so label toggling produces overlapping runs whose completion order decides the check's final state.

Verification is manual throughout — `actionlint` was run by hand and the regex and tag-baseline logic was exercised in a scratch repo, but there is no `actionlint` step in `ci.yml` or in the pre-commit config, so none of it regresses automatically.

<details>
<summary>Issues (10)</summary>

1. **Release-branch bootstrap misses two called workflows** — `docs/development/release.md` copies only `release.yml` and `ci.yml` from main onto `release/X.Y`, but `release.yml` also calls `./.github/workflows/sbom.yml` and `./.github/workflows/integrations-ci-cd-kicker.yaml`, and neither declares `workflow_call` at `v0.17.1`. Add both to the `git checkout main --` list.
2. **Partial label set drops valid backports** — `core.setFailed` in the target-resolution step skips the checkout and backport steps, so a mix of resolvable and unresolvable lines backports nothing. Comment and warn for the unresolvable lines, and fail in a trailing step after the backport runs.
3. **Downstream release announcement is not backport-aware** — `notify-integration-ci-cd` dispatches the backport tag to `kubecost/integration-ci-cd` unconditionally. Confirm what the consumer does with `client_payload.tag` and gate or pass `is_backport` if it treats the tag as current.
4. **`vX.Y` naming main's own line is silently a no-op** — right after cutting `release/0.18` from `v0.18.0`, labelling a PR `v0.18` to backport it does nothing but log. Either resolve a label by whether `release/X.Y` exists rather than by comparing to main's version, or comment on the PR when a label is skipped as at-or-above main.
5. **Dependabot exemption keys on the wrong field** — `github.actor != 'dependabot[bot]'` is the actor of the triggering event, so any human label action on a Dependabot PR runs the check and fails it. Use `github.event.pull_request.user.login`.
6. **No concurrency group on `require-version-label`** — rapid label changes produce overlapping runs with no completion ordering, so a stale failing run can land after a passing one. Add a PR-keyed group with `cancel-in-progress: true`.
7. **App token minted before targets are resolved** — the token step has no `if`, so every merged PR into main runs the backport job and mints a token; a missing or under-permissioned app turns every merge red. Move resolution ahead of token generation.
8. **Main's tag baseline now fails open** — the `|| true` added to the main-branch `git tag | grep` bumps from the in-tree version when no tag is found, while the release-line path errors out. Make the two paths agree.
9. **Re-adding a label may collide with an existing backport branch** — the docs' recovery path is "re-add the label", and the same gesture on a line that already has an open backport PR re-runs the action against an existing `backport-<n>-to-release-X.Y` branch. Confirm `korthout/backport-action@v4.7.0`'s behaviour there (skip, force-push, or fail).
10. **No automated workflow linting** — three new workflows and a substantially reworked `release.yml`, with `actionlint` run only by hand. Add it to `ci.yml` or the pre-commit config.

</details>

<details>
<summary>Details</summary>

### The release-branch bootstrap cannot work as documented

`release.md`'s "Starting a maintained line" recipe is the one piece of this PR that gets exercised first, and it is incomplete:

```bash
git switch -c release/X.Y vX.Y.Z               # the last release of that line
git checkout main -- .github/workflows/release.yml .github/workflows/ci.yml
```

`workflow_dispatch` runs the workflow file from the dispatched branch — the doc says so — and local reusable-workflow references (`uses: ./.github/workflows/...`) resolve at that same ref. `release.yml` has three:

```
release.yml (from main)
  ├─ test                     → ./.github/workflows/ci.yml                     ← copied
  ├─ sbom                     → ./.github/workflows/sbom.yml                   ← branch's own
  └─ notify-integration-ci-cd → ./.github/workflows/integrations-ci-cd-kicker.yaml ← branch's own
```

Both files exist at `v0.17.1`, but neither declares `workflow_call` there — the kicker has only `on: push: tags`, and `sbom.yml` has `push`/`workflow_dispatch`/`pull_request`. The `workflow_call` interfaces and the `tag` / `release_version` inputs that `release.yml` passes were added later on main. GitHub validates called workflows when the run is created, so the expected outcome is that the whole dispatch fails before any job executes (likely — the file-level mismatch is confirmed, the blast radius is from how Actions resolves reusable workflows at run creation). Either way the first v0.17.2 attempt does not produce a release.

Everything else main's `ci.yml` depends on does exist at `v0.17.1` (`scripts/check_consent_branding.py`, `scripts/pre_commit_hooks/check_repo_safety.py`, `tests/mcp-demo.json`), so extending the copy list to four files is the whole fix.

### Target resolution fails the step it still produces output for

```js
if (problems.length > 0) {
  await github.rest.issues.createComment({ /* ... */ });
  core.setFailed(problems.join(' '));
}
core.setOutput('branches', branches.join(' '));
```

The output is set, and the two steps that consume it are guarded only by `if: steps.targets.outputs.branches != ''`. A failed step skips them. So the asymmetry is: label a merged PR `v0.17` alone and it backports; label it `v0.17` and `v0.16` when `release/0.16` was never cut and neither happens, with the PR comment naming only the missing branch. The comment text ("Create the branch ... then re-add the label") reads as though the resolvable half already went through.

### What a `vX.Y` label resolves to depends on when you look

The line comparison reads main's in-tree version:

```js
const ver = /^version\s*=\s*"(\d+)\.(\d+)\./m.exec(text);   // pyproject.toml on main
const olderThanMain = (l) => l.major < main.major || (l.major === main.major && l.minor < main.minor);
```

The version bump commit is pushed as part of the release, so main's in-tree version is the *last released* line, not the next one. Immediately after releasing 0.18.0 and cutting `release/0.18`, both meanings of `v0.18` are live at once — `release.md` defines it as "ships from main", while an engineer who just created the branch reads it as "and patch the 0.18 line". The code takes the first reading and logs `v0.18 is main's line or newer; no backport needed.` into a workflow run nobody is watching. The failure is silent, which is the part worth fixing even if the semantics stay as documented: the at-or-above-main skip deserves the same PR comment the missing-branch case gets.

Resolving a label by whether `release/X.Y` exists sidesteps the ambiguity entirely — a line with no branch is not maintained, and a line with a branch is, independent of what main's `pyproject.toml` says.

### Backport-aware publishing, and the one path that isn't

Four decisions hang off `is_backport`, and they cover the ways a stale line could present itself as current:

```
check-release-ref ──► is_backport, line
   ├─ build:            tag baseline grep anchored to the line; --latest=false; --notes-start-tag <prev of line>
   ├─ publish-image:    latest tag enable=... && is_backport != 'true'
   ├─ update-parent-chart: skipped unless parent-branch names a target; sort -V downgrade guard
   └─ notify-integration-ci-cd:  (ungated)
```

The parent-chart path is defended twice: the job is skipped unless `parent-branch` names a target, and if it runs anyway `sort -V | tail -1` refuses to rewrite a newer pin.

`notify-integration-ci-cd` is the gap. It forwards `needs.build.outputs.release_tag` into a `repository_dispatch` on `kubecost/integration-ci-cd` with `event_type: mcp-kubecost-release`. If that consumer pins or deploys the announced tag, a 0.17.2 backport walks it back from 0.18.x — the same failure mode `--latest=false` and the parent-chart skip exist to prevent. The downstream repo isn't visible from here, so this is a "check then gate", not a confirmed regression.

### Baseline derivation disagrees with itself on missing tags

```bash
if [ -n "$LINE" ]; then
  LATEST_TAG=$(git tag --sort=-v:refname | grep -E "$TAG_PATTERN" | head -1 || true)
  if [ -z "$LATEST_TAG" ]; then
    echo "::error::No v${LINE}.* tag found; ..."
    exit 1
  fi
else
  LATEST_TAG=$(git tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)
fi
```

The release-line branch treats "no tag" as unreleasable and stops. The main branch, with the newly added `|| true`, treats it as "bump from whatever `pyproject.toml` says" — which under a bad checkout (no tags fetched) produces a release computed from in-tree state rather than from release history. `fetch-depth: 0` makes that unlikely today; the asymmetry is still the kind that gets discovered during an incident.

### `backport.yml` as a trust boundary

The dangerous combination in a `pull_request_target` workflow is write-scoped credentials plus PR-head code, and this one avoids it deliberately: `actions/checkout` is given no `ref:` so it takes the base branch, the comment above `permissions` states the rule, and `merged == true` means the commits being cherry-picked are already on main. The app token narrows itself to `permission-contents: write` / `permission-pull-requests: write`.

Two residual notes. Adding a label is a triage-level permission, and it is now sufficient to push a branch and open a PR via the app token — acceptable given the content is already reviewed, but it is a wider trigger surface than merge permission. And the token step runs unconditionally ahead of resolution, so every merged PR into main produces a backport run; if the app's installation or permissions are wrong (still an open follow-up in the PR description), that is a red check on every merge rather than only on labelled ones.

Idempotency is the untested corner. `release.md` step 2's recovery instruction is to re-add the label, which fires the `labeled` path with that single label — correct for the "branch was missing" case. The same gesture on a line whose backport PR is already open re-runs `korthout/backport-action@v4.7.0` against an existing `backport-<n>-to-release-X.Y` branch, and whether that skips, force-pushes over review commits, or fails is not established here.

### The label gate's enforcement properties

Label values reach the shell through an env var and `jq` rather than `${{ }}` interpolation, and the absence of a `paths` filter is deliberate and commented — a required check that doesn't run on some PRs blocks them permanently.

The Dependabot exemption is the defect. `github.actor` is the actor of the triggering event: on `labeled` that is whoever added the label, and on a human push to a Dependabot branch it is that human. Either flips the condition to true, the job runs, and a Dependabot PR has no `vX.Y` label — so the required check fails on a PR the exemption was written to cover. `github.event.pull_request.user.login` is stable across all five trigger types.

Missing concurrency is the second. `labeled` and `unlabeled` both trigger, and the workflow sets no group, so removing and re-adding a label starts two runs with no guarantee about which finishes last. The surviving check-run conclusion is then a coin flip. `ci.yml` and `release.yml` both set a group; this one should too.

Worth stating plainly before this becomes a required check: a `pull_request`-triggered gate runs from the PR's own merge commit, so a PR can edit or delete `require-version-label.yml` and satisfy its own check. That's inherent to the trigger, and fine for a process nudge, but it is not an enforcement boundary against a motivated author.

### Verification

The PR's checks — `actionlint` by hand, regex and tag-baseline logic in a scratch repo — are not repeatable: there is no `actionlint` step in `ci.yml` and none in the pre-commit config, so the three new workflows and the reworked `release.yml` have no syntax or expression guard going forward. The `grep`/`sort -V`/`jq` logic added here is the kind that `bats` or a small script could pin, though that would be new testing infrastructure for this repo.

Not tested: the end-to-end backport (label → cherry-pick → PR), a dry-run dispatch from a `release/X.Y` branch, the `--latest=false` and `--notes-start-tag` behaviour against a real release, the downgrade guard, and the `require-version-label` check itself (blocked on the `ci-only` label existing).

</details>

<details>
<summary>File map</summary>

- `.github/workflows/backport.yml` (new, 129 lines) — `pull_request_target` on `closed`/`labeled`; `github-script` resolves `vX.Y` labels to `release/X.Y` branches against main's `pyproject.toml` version, then `korthout/backport-action` cherry-picks.
- `.github/workflows/release.yml` — ref gate extended to `release/X.Y` with a patch-only rule and `is_backport`/`line` outputs; line-scoped tag baseline; `--latest=false` and `--notes-start-tag` for backports; `latest` image tag suppressed; new `parent-branch` input plus a `sort -V` downgrade guard on the parent chart.
- `.github/workflows/require-version-label.yml` (new, 35 lines) — requires a `vX.Y` or `ci-only` label on PRs into main, Dependabot exempt.
- `docs/development/release.md` (new, 54 lines) — dispatch matrix, label semantics, backport procedure, starting a maintained line, pitfalls.
- `docs/development/README.md` — one-line pointer to `release.md`.
- `AGENTS.md` — `release.md` added to Related Docs.

Full diff: `gh pr diff 111 --repo kubecost/mcp-kubecost`

</details>
