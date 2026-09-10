# pre-commit tool

This repo uses a fully local pre-commit configuration using custom Python scripts instead of external repositories. All hooks run using `repo: local` with `language: system`.

Some files have been modified to meet our needs. Others can be found here: <https://github.com/pre-commit/pre-commit-hooks/tree/main/pre_commit_hooks>

The goal of the [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) is to be very fast and only protect from crimes, like pushing secrets or committing to main.

CI will fix formatting and run other checks when a PR is created. [`auto-fix-formatting.yml`](../../.github/workflows/auto-fix-formatting.yml)

The config for CI [`.github/pre-commit-config-ci.yaml`](../../.github/pre-commit-config-ci.yaml) that is run by the workflow.

Run the CI version from the repository root with:

```sh
just auto-format
# or:
uv run pre-commit run --config .github/pre-commit-config-ci.yaml --all-files
```

`just auto-format` refuses to run when there are unstaged changes, so the formatter's diff stays reviewable. `pre-commit` is a `dev` extra — install it with `uv sync --extra dev` rather than `uvx`.

## Benefits

- **No external dependencies** - All checks run locally using system Python
- **Faster execution** - No need to download/cache external repos
- **Customizable** - Easy to modify checks for project-specific needs
- **Transparent** - All logic visible in `scripts/` directory

## Usage

```bash
# Install hooks
uv run pre-commit install

# Run manually on all files
uv run pre-commit run --all-files

# Run on staged files
uv run pre-commit run
```

Run these commands from the repository root so the config paths above resolve correctly.

Note, we do not need to use the below flag because we do not have remote dependencies:

```bash
pre-commit install --install-hooks
```
