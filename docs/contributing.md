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
