# Development and CI

Return to the [guide index](index.md).

## Local checks

Run the full checks from the project root:

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ebs --help
uv run ebs projects list --help
uv run ebs projects use --help
uv run ebs projects show --help
uv run ebs triggers list --help
uv run ebs triggers show --help
uv run ebs triggers enable --help
uv run ebs triggers disable --help
uv run ebs events list --help
uv run ebs s3 credentials --help
uv run ebs s3 upload-url --help
uv run ebs s3 upload --help
uv run ebs s3 ls --help
uv run ebs s3 download --help
uv run ebs s3 rm --help
uv build
```

`uv build` produces an installable wheel and source archive locally. Nothing is published by that command.

Tests use mocked HTTP transports and temporary config files. They do not contact a deployed server or upload live data.

## Continuous integration

GitHub Actions runs the same checks on every push to `main`, every pull request, and on manual dispatch. Each run uses Ubuntu with Python 3.11, 3.12, 3.13, and 3.14 and installs the locked dependencies with `uv sync --locked`, then:

- runs the full pytest suite,
- checks Ruff lint, formatting, and import ordering,
- smoke-tests `ebs --version` and `--help` for every command, and
- builds a wheel and a source distribution and verifies both contain every module in `src/ebs_cli`.

The tests are fully mocked, so CI needs no credentials and never contacts a deployed server or an object store. The workflow requests only read access to the repository and does not publish anything; the build artifacts it produces stay untracked, like `uv build` output locally.
