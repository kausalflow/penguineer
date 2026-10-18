"""penguineer's commands as agent tools.

Each command becomes a tool whose input schema is the command's own Params schema, so a host's
tools follow penguineer's version without changes. Fields the host sets itself (LLM options,
output paths) are left out, and file inputs become strings the host resolves (a query id, a
document name), since a model should not choose paths on the machine.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping

import penguineer

HIDDEN = frozenset({"llm", "llm_model", "llm_dry_run", "out", "in_place"})


def _is_path(schema: dict) -> bool:
    options = schema.get("anyOf", [schema])
    return any(o.get("format") in ("file-path", "directory-path", "path") for o in options)


def input_schema(params: dict, *, hidden: Iterable[str] = HIDDEN, file_input: str = "") -> dict:
    """A command's Params schema without ``hidden`` fields, with file inputs as strings
    described by ``file_input``."""
    schema = copy.deepcopy(params)
    props = schema.get("properties", {})
    for name in set(hidden) & set(props):
        del props[name]
    for name, prop in props.items():
        if _is_path(prop):
            desc = prop.get("description", "")
            text = f"{desc}. {file_input}" if desc and file_input else desc or file_input
            props[name] = {"type": "string", "description": text}
    schema["required"] = [r for r in schema.get("required", []) if r in props]
    schema.pop("title", None)
    if "$defs" in schema and "$ref" not in str(props):
        del schema["$defs"]
    return schema


def command_tools(
    commands: Mapping[str, str],
    *,
    hidden: Iterable[str] = HIDDEN,
    file_input: str = "",
    note: str = "",
) -> dict[str, dict]:
    """Tool definitions keyed by tool name, for ``{tool name: penguineer command}``.

    Each description is the command's help line, with ``note`` (how the host runs it) after it.
    """
    from penguineer.core import default_registry

    schemas = penguineer.schemas()
    out = {}
    for name, command in commands.items():
        help_text = default_registry.get(command).help.split("\n\n")[0].strip()
        desc = f"penguineer {command} (v{penguineer.__version__}): {help_text}"
        out[name] = {
            "name": name,
            "description": f"{desc} {note}".strip(),
            "input_schema": input_schema(
                schemas[command]["params"], hidden=hidden, file_input=file_input
            ),
        }
    return out
