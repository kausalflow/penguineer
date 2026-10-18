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

## The bench and the calibration

A change to a test, a rule or the ranking changes how `investigate` scores on the [homeostat bench](concepts/bench.md); the Bench workflow posts the smoke bench in every pull request's job summary. To refit the weights and thresholds after such a change:

```bash
uv sync --extra bench
uv run penguineer bench homeostat --scenarios train --per-fault 12 --project .
uv run penguineer bench homeostat --scenarios validation --per-fault 12 --project .
uv run python scripts/fit_calibration.py .penguineer/bench/train-* .penguineer/bench/validation-*
```

Commit the new `src/penguineer/engines/calibration.json` with the change, and quote the validation numbers in the pull request. Never fit on the validation split's answers beyond the thresholds, and never on the hidden split.

A change to `penguineer lag`'s engine (`engines/lagfit.py`) is checked the same way against Homeostat plants with known volumes: run `scripts/validate_lag.py` (see [Finding the actual lag](concepts/lag.md#checked-on-homeostat-plants)) and quote its errors in the pull request.

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

1. Run the release gate: start the *Release gate* workflow from the Actions tab on `main`. It runs the bench on the hidden split (160 plants by default) with a seed taken from the repository secret `BENCH_HIDDEN_SEED` (any text: the workflow hashes it to the whole number `--split-seed` takes), and passes when the scores meet the gate in [The homeostat bench](concepts/bench.md#metrics). It shows only the summary scores and uploads nothing, because this repository is public.
2. Set the new version in `pyproject.toml`, in `packages/plantgraph/pyproject.toml`, and in penguineer's `plantgraph==` pin (`tests/test_versions.py` checks that the three agree), then run `uv lock`. Merge that to `main`.
3. On GitHub, create a release from `main` with the tag `v<version>`, for example `v0.1.0`. The workflow stops if the tag does not match the version.

If publishing fails part way (for example PyPI errors after uploading one of the two packages), run the Release workflow by hand from the Actions tab with the release tag as input. Files already on PyPI are skipped.

PyPI side, done once per project: add a trusted publisher with owner `kausalflow`, repository `penguineer` and workflow `release.yml`, with environment `pypi` for `penguineer` and `pypi-plantgraph` for `plantgraph`. Before a project's first release, add it as a pending publisher under *Your account > Publishing*. The two environments differ on purpose: PyPI cannot create a new project with a token that already matches an existing project's publisher.
