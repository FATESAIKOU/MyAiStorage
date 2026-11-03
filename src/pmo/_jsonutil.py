"""Shared schema-check helpers for the pmo JSON files (design §3.4).

Every file follows the same rules: `version` is the integer 1 when it is there;
known fields are type-checked when they are present; unknown fields are
preserved as they are and kept on the loaded record.

`version` is required everywhere except the team state: that file is shared
with MyPMO during the transition (Q-9) and MyPMO's team state files carry no
`version`, so a missing one must read as 1 and must not be written back
(`check_version_if_present`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NoReturn

from .errors import SchemaError

_MISSING = object()


def fail(where: str, message: str) -> NoReturn:
    """Raise a SchemaError that names the file's role and what is wrong."""
    raise SchemaError(f"{where}: {message}")


def describe(value: object) -> str:
    """A short, type-led description of a value for error messages."""
    text = repr(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return f"{type(value).__name__} {text}"


def check_object(data: object, where: str) -> Mapping[str, object]:
    """The whole file is a JSON object."""
    if not isinstance(data, dict):
        fail(where, f"not a JSON object ({describe(data)})")
    return data


def need(data: Mapping[str, object], key: str, where: str) -> object:
    """A required field."""
    value = data.get(key, _MISSING)
    if value is _MISSING:
        fail(where, f"the field {key!r} is missing")
    return value


def check_version(data: Mapping[str, object], where: str) -> None:
    """`version` is required and is the integer 1."""
    value = need(data, "version", where)
    if not isinstance(value, int) or isinstance(value, bool):
        fail(where, f"the field 'version' must be the integer 1, got {describe(value)}")
    if value != 1:
        fail(where, f"the field 'version' must be 1, got {value!r}")


def check_version_if_present(data: Mapping[str, object], where: str) -> int | None:
    """An optional `version`; absent is the implicit 1, present must still be 1."""
    if "version" not in data:
        return None
    check_version(data, where)
    return 1


def check_str(data: Mapping[str, object], key: str, where: str) -> str:
    """A required string field."""
    value = need(data, key, where)
    if not isinstance(value, str):
        fail(where, f"the field {key!r} must be a string, got {describe(value)}")
    return value


def check_bool(data: Mapping[str, object], key: str, where: str) -> bool:
    """A required boolean field (a JSON `true`/`false`; `1` is not one)."""
    value = need(data, key, where)
    if not isinstance(value, bool):
        fail(where, f"the field {key!r} must be a boolean, got {describe(value)}")
    return value


def check_dict(data: Mapping[str, object], key: str, where: str) -> Mapping[str, object]:
    """A required object field."""
    value = need(data, key, where)
    if not isinstance(value, dict):
        fail(where, f"the field {key!r} must be an object, got {describe(value)}")
    return value


def optional_str_or_none(data: Mapping[str, object], key: str, where: str) -> str | None:
    """An optional field that is a string or JSON `null` when present."""
    value = data.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        fail(where, f"the field {key!r} must be a string or null, got {describe(value)}")
    return value


def optional_str(
    data: Mapping[str, object], key: str, where: str, default: str | None = None
) -> str | None:
    """An optional string field; absent (or null) falls back to `default`."""
    value = data.get(key, _MISSING)
    if value is _MISSING or value is None:
        return default
    if not isinstance(value, str):
        fail(where, f"the field {key!r} must be a string, got {describe(value)}")
    return value


def optional_dict(data: Mapping[str, object], key: str, where: str) -> Mapping[str, object]:
    """An optional object field; absent falls back to an empty object."""
    value = data.get(key, {})
    if not isinstance(value, dict):
        fail(where, f"the field {key!r} must be an object, got {describe(value)}")
    return value


def optional_dict_or_none(
    data: Mapping[str, object], key: str, where: str
) -> Mapping[str, object] | None:
    """An optional object-or-null field; absent (or null) falls back to None.

    The team state's `exit` is the shape this exists for: the sidecar's exit
    object when the agent has one, `null` when it has not (design §3.4).
    """
    value = data.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, dict):
        fail(where, f"the field {key!r} must be an object or null, got {describe(value)}")
    return value


def dict_of_str(data: Mapping[str, object], key: str, where: str) -> dict[str, str]:
    """A required object whose every value is a string (e.g. `notified.call`)."""
    value = need(data, key, where)
    if not isinstance(value, dict):
        fail(where, f"the field {key!r} must be an object, got {describe(value)}")
    for name, member in value.items():
        if not isinstance(member, str):
            fail(
                where,
                f"the field {key!r} must be a string for every id; {name!r} is {describe(member)}",
            )
    return dict(value)


def pick_extra(data: Mapping[str, object], known: Sequence[str]) -> dict[str, object]:
    """Everything that is not a known field, preserved as it is."""
    keep = set(known)
    return {key: value for key, value in data.items() if key not in keep}
