"""Introspection of Pydantic fields, shared by the CLI generator, to_argv and run records."""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from pydantic.fields import FieldInfo
from pydantic.types import PathType


@dataclass(frozen=True)
class FieldShape:
    """What a field looks like once Optional, list and Annotated wrappers are peeled off."""

    base: Any  # the innermost type, e.g. int, Path, an Enum subclass
    optional: bool
    is_list: bool
    path_type: str | None  # "file", "dir" or "new" for pydantic FilePath/DirectoryPath/NewPath


def _strip_annotated(tp: Any, metadata: list) -> Any:
    while typing.get_origin(tp) is Annotated:
        args = typing.get_args(tp)
        metadata.extend(args[1:])
        tp = args[0]
    return tp


def field_shape(field: FieldInfo) -> FieldShape:
    metadata: list = list(field.metadata)
    tp = _strip_annotated(field.annotation, metadata)
    optional = False
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        optional = len(args) < len(typing.get_args(tp))
        if len(args) != 1:
            raise TypeError(f"unsupported union type {tp!r}")
        tp = _strip_annotated(args[0], metadata)
        origin = typing.get_origin(tp)
    is_list = origin in (list, tuple, set, frozenset)
    if is_list:
        tp = _strip_annotated(typing.get_args(tp)[0], metadata)
    path_type = next((m.path_type for m in metadata if isinstance(m, PathType)), None)
    return FieldShape(base=tp, optional=optional, is_list=is_list, path_type=path_type)


def format_value(value: Any) -> str:
    """Canonical command-line text for one value, parsed back exactly by the CLI."""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def option_name(field_name: str) -> str:
    return "--" + field_name.replace("_", "-")


def is_path_type(base: Any) -> bool:
    return isinstance(base, type) and issubclass(base, Path)
