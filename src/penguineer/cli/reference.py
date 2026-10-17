"""Render the CLI reference page from the registry, so the docs cannot drift from the code."""

from __future__ import annotations

from penguineer.core.fields import field_shape, format_value, is_path_type, option_name
from penguineer.core.registry import Registry


def _type_name(field) -> str:
    shape = field_shape(field)
    base = shape.base
    if hasattr(base, "__members__"):
        name = " \\| ".join(f"`{m.value}`" for m in base)
    elif is_path_type(base):
        name = {"file": "existing file", "dir": "existing folder"}.get(shape.path_type, "path")
    else:
        name = getattr(base, "__name__", str(base))
    return f"{name}, repeatable" if shape.is_list else name


def render(registry: Registry) -> str:
    lines = [
        "# CLI reference",
        "",
        "<!-- Generated from the command registry by scripts/gen_cli_reference.py. -->",
        "",
        "Every command also takes `--json` (print the result model as JSON on stdout), "
        "`--project PATH` (the folder run records and outputs go under, default `.`) and "
        "`--seed INT` (default `0`).",
        "",
        "Exit codes: 0 success, 1 validation failed, 2 usage error, 3 not enough data, "
        "4 internal error or not implemented yet.",
        "",
    ]
    for cmd in registry:
        lines += [f"## `penguineer {cmd.name}`", "", cmd.help or "", ""]
        lines += ["| Option | Type | Default | Description |", "|---|---|---|---|"]
        for fname, field in cmd.params.model_fields.items():
            if field.is_required():
                default = "required"
            else:
                value = field.get_default(call_default_factory=True)
                default = "" if value in (None, [], ()) else f"`{format_value(value)}`"
            desc = (field.description or "").replace("|", "\\|")
            lines.append(f"| `{option_name(fname)}` | {_type_name(field)} | {default} | {desc} |")
        lines.append("")
    return "\n".join(lines)
