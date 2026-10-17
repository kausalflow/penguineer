# Contributing

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Adding a command

1. Add a module in `src/penguineer/commands/` with a `Params` model (subclass `CommandParams`), a `Result` model (subclass `CommandResult`) and a function decorated with `@command("group name", params=..., result=...)`. Its docstring is the help text.
2. Import the module in `src/penguineer/commands/__init__.py`.
3. Regenerate the schema snapshot and the CLI reference:

    ```bash
    PENGUINEER_UPDATE_SCHEMAS=1 uv run pytest tests/test_schemas.py
    uv run python scripts/gen_cli_reference.py
    ```

Use `FilePath` for input files, so the CLI checks that they exist and the run record hashes them. Put engines in `engines/`, typed in and out and free of global state.

## Schemas are public API

`tests/test_schemas.py` fails when any command's Params or Result schema changes, because Alchemist builds on them. Regenerate the snapshot only for an intended change, and say so in the pull request.

## Documentation

The site is built with [Zensical](https://zensical.org):

```bash
uvx zensical serve    # preview on http://localhost:8000
uvx zensical build    # static site in ./site
```

`docs/reference/cli.md` is generated from the registry, and a test fails when it is out of date.

## Docs previews and screenshots

Every pull request from a branch of this repository gets a preview of the documentation at `https://kausalflow.com/penguineer/pr-preview/pr-<number>/`, posted as a comment on the pull request and removed when it closes. `main` is published at <https://kausalflow.com/penguineer/>. Both come from the `gh-pages` branch, written by `.github/workflows/docs.yml`.

Each deploy also has screenshots of every page under `_screenshots/` (for a preview, `pr-preview/pr-<number>/_screenshots/`), taken with Playwright by `scripts/screenshot_docs.py`. To take them locally:

```bash
uvx zensical==0.0.70 build --clean
uv run --no-project --with playwright==1.56.0 python scripts/screenshot_docs.py site
```

A pull request description includes the preview link and screenshots of the pages it changes, and of any visible output such as a command's summary. The pull request template has sections for both.

## Releasing

penguineer and plantgraph are published to PyPI together, with one version. `.github/workflows/release.yml` builds both, installs the wheels in a clean environment, and publishes them with PyPI trusted publishing when a GitHub release is published. No token is stored in the repository.

1. Set the new version in `pyproject.toml`, in `packages/plantgraph/pyproject.toml`, and in penguineer's `plantgraph==` pin (`tests/test_versions.py` checks that the three agree), then run `uv lock`. Merge that to `main`.
2. On GitHub, create a release from `main` with the tag `v<version>`, for example `v0.1.0`. The workflow stops if the tag does not match the version.

PyPI side, done once per project (`penguineer` and `plantgraph`): add a trusted publisher with owner `kausalflow`, repository `penguineer`, workflow `release.yml` and environment `pypi`. Before a project's first release, add it as a pending publisher under *Your account > Publishing*.
