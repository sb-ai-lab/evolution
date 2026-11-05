# Contributing to Evolution

Report reproducible problems and propose changes through the [issue tracker](https://github.com/sb-ai-lab/evolution/issues) and [pull requests](https://github.com/sb-ai-lab/evolution/pulls). Include your package version, operating system, a small reproduction and the behavior you expected. Remove credentials and private task data before sharing logs. Report vulnerabilities through [Security](SECURITY.md).

## Run the checks

Use Python 3.11.3 or newer. From the repository root:

```bash
python -m pip install uv==0.10.11
uv sync --locked --group dev --extra tree --extra llm
uv pip install hatchling==1.31.0 pip
uv run --locked --no-sync ruff check src tests scripts
uv run --locked --no-sync ruff format --check src tests scripts
uv run --locked --no-sync mypy src/evolution
uv run --locked --no-sync pytest -n 8 --dist worksteal -m "not live and not paper and not external_repo" -q
```

The offline suite uses subprocesses and local sockets; package checks can download build dependencies. Tests marked `live`, `paper` or `external_repo` require additional services or data and are not part of the offline command. Documentation checks use Git's tracked-file list, so stage new documentation and register it in `docs/markdown-classification.json` before running them.

Keep changes focused, preserve supported commands and saved-run behavior, and include checks for the behavior you change. Explain the problem, resulting behavior and actual verification in the pull request. Accepted public contributions retain their attribution in later snapshots.

## Build and check a package

```bash
uv run --locked --no-sync python -m build
uv run --locked --no-sync twine check dist/*
uv run --locked --no-sync python scripts/check_release_artifacts.py dist/*
```

The [Quickstart](QUICKSTART.md) provides an offline campaign and the [documentation index](docs/SUMMARY.md) describes the supported workflows. To rehearse an installed wheel outside the checkout, run `uv run --locked --no-sync python scripts/v2_preview_rehearsal.py /tmp/evolution-wheel-check --wheel dist/YOUR_WHEEL.whl`; choose a new output directory and your actual wheel filename. A successful rehearsal prints `ok` and exits zero.
