# Releasing and backporting

Releases are cut by the **release pipeline** workflow (`.github/workflows/release.yml`, manual dispatch).

| Dispatched from | Allowed bump | What it does |
|---|---|---|
| `main` | patch / minor / major | Bumps from the newest tag, publishes the image as `vX.Y.Z` **and** `latest`, marks the GitHub Release as Latest, opens the subchart bump PR on `kubecost/kubecost` `develop`. |
| `release/X.Y` | **patch only** | Bumps from the newest `vX.Y.*` tag, publishes `vX.Y.Z` only (no `latest`), GitHub Release is **not** marked Latest and its notes start at the previous tag of that line. No parent chart PR unless `parent-branch` is set. |
| any branch, `dry-run=true` | n/a | Builds and tests without tagging, pushing or publishing. |

`workflow_dispatch` runs the workflow file **from the branch you dispatch on**, so a `release/X.Y` branch must carry the current `release.yml` and `ci.yml` (see below).

## Labels: which lines does a change ship in?

Every PR into `main` needs one of:

- one or more **`vX.Y`** labels: the release lines the change ships in. The line `main` is on (e.g. `v0.18`) means "ships from main". An older line (e.g. `v0.17`) additionally backports to `release/0.17`.
- **`ci-only`**: nothing shipped changes (workflows, docs, tests).

The `require version label` check enforces this (Dependabot is exempt). It only blocks merging once `require-version-label` is a required status check on `main` in the repository ruleset.

## Backporting a merged change

1. Make sure `release/X.Y` exists (first time only, see below) and a `vX.Y` label exists.
2. Add the `vX.Y` label to the merged PR (or before merging). The **backport** workflow cherry-picks the PR onto `release/X.Y` and opens `[Backport release/X.Y] <title>`. Adding labels for several lines opens one PR per line. A label for a line with no `release/X.Y` branch gets an explanatory comment instead.
3. Review and merge the backport PR as usual (CI runs on it).
4. On a conflict no PR is opened; the bot comments the exact commands on the original PR. Resolve by hand:
   ```bash
   git switch -c backport-<n>-to-release-X.Y origin/release/X.Y
   git cherry-pick -x <sha>        # resolve, then git cherry-pick --continue
   ```
5. Add the version's entry to `CHANGELOG.md` on `release/X.Y`, then dispatch the release pipeline from `release/X.Y` with `bump-level=patch` (run once with `dry-run=true` first).
6. Forward-port the changelog section to `main` in a PR (`ci-only` if that is all it changes).

## Starting a maintained line

Only cut `release/X.Y` when the line will actually receive patches. Once it exists, `vX.Y` labels start producing backports.

```bash
git fetch --tags origin
git switch -c release/X.Y vX.Y.Z               # the last release of that line
git checkout main -- .github/workflows/release.yml .github/workflows/ci.yml \
  .github/workflows/sbom.yml .github/workflows/integrations-ci-cd-kicker.yaml
git commit -m "ci: carry release workflow onto release/X.Y"
git push -u origin release/X.Y
gh label create vX.Y --description "Also ship on the X.Y line (backport to release/X.Y)" --color 0e8a16
```

`release.yml` calls `sbom.yml` and `integrations-ci-cd-kicker.yaml` as reusable workflows, so all four files must exist on the branch; the v0.17.x tags predate their `workflow_call` triggers.

Also confirm the ruleset allows the release bot / `DEPLOY_KEY` to push to `release/*` (the version bump commit is pushed to the dispatched branch).

## Pitfalls

- Never dispatch a minor/major from `release/*`; the pipeline refuses.
- Behaviour that is breaking on `main` (such as `image.registry` precedence) is still breaking when backported: say so in the patch's changelog entry.
- Backports are opt-in per PR. Nothing is backported automatically.
