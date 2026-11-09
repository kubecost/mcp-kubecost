# Patch release lines and label-driven backports

## Summary

PR 111 adds an opt-in maintenance-line workflow: merged changes can be routed to `release/X.Y` by labels, and the existing release pipeline can publish patch releases from those branches without moving any `latest` pointer. It also introduces a required-check candidate for release-line labels and documents the operator workflow for creating branches, resolving conflicts, and releasing patches. Backport releases are isolated across GitHub Releases, ICR, public ECR, Helm, and parent-chart updates. No substantive prior reviewer finding was present to deduplicate.

Watch for: **confirmed** — if one requested release branch is missing, target resolution fails before any valid requested branch can be backported; **confirmed** — the write-capable backport action is pinned to a mutable tag; **confirmed** — the event-driven backport and release paths have not yet been exercised end to end.

**Verdict**: NEEDS_CHANGES

## High-level view

Old-line releases are recognized from an exact `release/X.Y` ref and restricted to patch bumps. Their version baseline is the newest tag from the same line, their GitHub Release is explicitly not Latest, and only versioned ICR and public ECR tags are published. Parent-chart updates are opt-in for backports and refuse a downgrade, so only the normal `main` path advances shared defaults.

Backport selection translates `vX.Y` labels into `release/X.Y` branches only when the label is older than the line currently represented by `main`. The resolver handles labels added before merge through the `closed` event and afterward through the `labeled` event, then delegates cherry-picking and PR creation to `korthout/backport-action`. **Confirmed:** reporting one nonexistent branch marks the resolver step failed, so GitHub skips valid branches resolved alongside it.

The new label check accepts at least one `vX.Y` or `ci-only` label for PRs into `main`, with Dependabot intentionally exempt. **Confirmed:** it does not become merge policy until the repository ruleset names it as required; that rollout dependency is explicitly deferred in the PR description.

The trust boundary is narrower than a typical `pull_request_target` workflow because only merged PRs qualify, target names come from a numeric label grammar, and checkout uses `main`. **Confirmed:** the remaining supply-chain exposure is a tag-pinned third-party action receiving an installation token with repository contents and pull-request write access. **Confirmed:** CI validates the repository and label check, but the new post-merge and old-line release behavior still depends on pending manual exercises.

<details>
<summary>Issues (4)</summary>

1. **Mixed target abort — confirmed, blocking** — A missing `release/X.Y` makes the resolver step fail and suppresses every valid target in the same run. Preserve valid outputs through the backport step, then report missing branches separately.
2. **Mutable privileged action ref — confirmed** — `korthout/backport-action@v4.7.0` receives a contents-write installation token through a mutable tag. Pin the reviewed commit SHA, or explicitly carry the acknowledged risk as a tracked follow-up.
3. **Ruleset activation pending — confirmed** — `require-version-label` is advisory until it becomes a required `main` check. Complete the documented repository-ruleset rollout before relying on labels as release intent.
4. **Event paths unexercised — confirmed** — No completed check covers post-merge labeling, mixed branch resolution, or an actual `release/X.Y` dispatch. Add resolver regression coverage and complete the planned dry-run/live integration checks.

</details>

<details><summary>Details</summary>

## Mixed release-line labels suppress valid backports

**Confirmed:** `.github/workflows/backport.yml:98-117` calls `core.setFailed()` whenever any requested old line lacks its `release/X.Y` branch, even though it still writes valid branches to the `branches` output. A failed action step makes the implicit `success()` condition on subsequent steps false, so `if: steps.targets.outputs.branches != ''` cannot make checkout or backport run after a mixed result.

For example, a merged PR carrying `v0.17` and `v0.16` will resolve `release/0.17` and report that `release/0.16` is missing, but no PR to `release/0.17` will be created. This contradicts `docs/development/release.md`, which says several line labels open one PR per line and describes a missing branch as an explanatory-comment case.

The resolver should emit valid branches without failing, allow those branches to reach the action, and only then decide whether missing branches should make the workflow red. A terminal step keyed by a `problems` output would preserve both behaviors if failure is desired; treating missing branches as a warning would match the documented “comment instead” behavior.

## Old-line publication stays outside moving release channels

The release branch is carried through the pipeline as `is_backport` and `line`, centralizing the branching decision:

```text
release/0.17 dispatch
        |
        +-- patch-only ref gate
        +-- baseline from newest v0.17.* tag
        +-- GitHub Release v0.17.N, latest=false
        +-- ICR v0.17.N (no latest)
        +-- Helm index + public ECR v0.17.N
        +-- parent-chart PR only with parent-branch
```

The same explicit version reaches SBOM generation and the integration CI/CD dispatch. An equal or newer parent-chart dependency exits without opening a downgrade PR. A maintenance release therefore remains distributable without replacing the newer line in shared defaults.

## Backport token scope is narrow, but action provenance remains mutable

The `pull_request_target` trigger can mint a contents-write token. The workflow limits execution to merged PRs, checks out the trusted default branch rather than the pull-request head, and derives target refs only from labels matching `^v(\d+)\.(\d+)$`; no PR-controlled string enters a shell script. The app token lets the generated PR trigger normal CI instead of being suppressed as a `GITHUB_TOKEN`-created event.

**Confirmed:** `korthout/backport-action@v4.7.0` is a mutable tag and receives that installation token with contents and pull-request write permissions. A moved or compromised tag could push branches and create or modify PRs. The PR description acknowledges SHA pinning as a follow-up, but pinning the reviewed `v4.7.0` commit here would remove the new privileged supply-chain exposure.

## Workflow checks do not exercise the new event paths

Current PR checks are green, including the version-label job and repository unit/integration suites. The test plan reports actionlint and scratch validation of regex/tag snippets, which covers syntax and isolated selection rules but not GitHub step-status behavior or delegated-action side effects.

**Confirmed — Not tested:** adding an old-line label to a merged PR, processing multiple targets when one branch is missing, and dispatching from a real `release/X.Y` branch. The mixed-target defect is the kind of orchestration failure those isolated checks miss. The resolver needs cases for no targets, valid targets, missing targets, and mixed valid/missing targets; extracting its decision logic into a testable script would make that coverage durable, while the planned dry-run and post-merge label exercise can validate GitHub/App integration.

</details>

<details><summary>File map</summary>

- `.github/workflows/backport.yml` — resolves maintenance-line labels, mints the bot token, and delegates cherry-pick PR creation.
- `.github/workflows/release.yml` — permits patch-only release branches and isolates release, image, notes, and parent-chart behavior.
- `.github/workflows/require-version-label.yml` — adds the `vX.Y`/`ci-only` pull-request gate with a Dependabot exemption.
- `docs/development/release.md` — defines release-line labels, backport operation, branch setup, and patch-release procedure.
- `docs/development/README.md` — links maintainers to the release guide.
- `AGENTS.md` — adds the release guide to the repository documentation map.

[Full PR diff](https://github.com/kubecost/mcp-kubecost/pull/111/files)

</details>
