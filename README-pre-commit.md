# pre-commit tool

This repo uses a fully local pre-commit configuration using custom Python scripts instead of external repositories. All hooks run using `repo: local` with `language: system`.

Some files have been modified to meet our needs. Others can be found here: <https://github.com/pre-commit/pre-commit-hooks/tree/main/pre_commit_hooks>

The goal of the [default pre-commit config](.pre-commit-config.yaml) is to be very fast and only protect from crimes, like pushing secrets or committing to main.

CI will fix formatting and run other checks when a PR is created. [CI Workflow](.github/workflows/auto-fix-formatting.yml)

The config for CI [.pre-commit-config-ci.yaml](.pre-commit-config-ci.yaml) that is run by the workflow.

Run the CI version with:

```sh
pre-commit run --config .pre-commit-config-ci.yaml --all-files
```

## Benefits

- **No external dependencies** - All checks run locally using system Python
- **Faster execution** - No need to download/cache external repos
- **Customizable** - Easy to modify checks for project-specific needs
- **Transparent** - All logic visible in `scripts/` directory

## Usage

```bash
# Install hooks
pre-commit install

# Run manually on all files
pre-commit run --all-files

# Run on staged files
pre-commit run
```

Note, we do not need to use the below flag because we do not have remote dependencies:

```bash
pre-commit install --install-hooks
```
