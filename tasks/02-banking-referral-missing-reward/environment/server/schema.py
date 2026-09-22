"""Minimal JSON Schema validator for tool arguments.

Covers the keyword subset the domain registries actually use: type (including
type unions and null), required, additionalProperties: false, enum, pattern,
format date and date-time, minimum, maximum, minItems, items, and properties.

A dependency-free validator is used rather than jsonschema so the image needs no
pip install and so the enforced subset is visible and reviewable. Anything the
registries do not use is not implemented, and a schema keyword this module does
not recognise is ignored rather than silently treated as satisfied by accident:
the registries are checked at container start by validate_registry().
"""
from __future__ import annotations

import re
from datetime import date, datetime

SUPPORTED = {
    "$schema", "type", "properties", "required", "additionalProperties", "enum",
    "pattern", "format", "minimum", "maximum", "minItems", "items", "description",
}

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def _check_format(value: str, fmt: str) -> str | None:
    if fmt == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return f"is not a valid date: {value!r}"
    elif fmt == "date-time":
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return f"is not a valid date-time: {value!r}"
    return None


def validate(instance, schema: dict, path: str = "") -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    errors: list[str] = []
    where = path or "(root)"

    declared = schema.get("type")
    if declared is not None:
        allowed = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS[t](instance) for t in allowed if t in _TYPE_CHECKS):
            got = "null" if instance is None else type(instance).__name__
            errors.append(f"{where}: expected {'/'.join(allowed)}, got {got}")
            return errors

    if instance is None:
        return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} is not one of {schema['enum']}")

    if isinstance(instance, str):
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, instance):
            errors.append(f"{where}: {instance!r} does not match {pattern}")
        fmt = schema.get("format")
        if fmt:
            problem = _check_format(instance, fmt)
            if problem:
                errors.append(f"{where}: {problem}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{where}: {instance} is below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{where}: {instance} is above maximum {schema['maximum']}")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{where}: missing required property {name!r}")
        if schema.get("additionalProperties") is False:
            for name in instance:
                if name not in properties:
                    errors.append(f"{where}: unexpected property {name!r}")
        for name, value in instance.items():
            if name in properties:
                child = f"{path}.{name}" if path else name
                errors.extend(validate(value, properties[name], child))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{where}: needs at least {schema['minItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, value in enumerate(instance):
                errors.extend(validate(value, item_schema, f"{path}[{index}]"))

    return errors


def validate_registry(registry: dict) -> list[str]:
    """Report registry keywords this validator would ignore.

    Called at startup so a schema feature the registry starts using cannot pass
    unenforced without anyone noticing.
    """
    unknown: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("properties",):
                    for child in value.values():
                        walk(child)
                    continue
                if key not in SUPPORTED and not isinstance(value, (dict, list)):
                    unknown.add(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for tool in registry.get("tools", []):
        walk(tool.get("parameters", {}))
    return sorted(unknown)
