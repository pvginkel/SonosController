"""Reading an OpenAPI document in a test."""

from typing import Any

_SCHEMAS = "#/components/schemas/"


def schema_of(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """The schema itself, or the one its ``$ref`` names."""
    if "$ref" not in schema:
        return schema
    assert schema["$ref"].startswith(_SCHEMAS)
    return document["components"]["schemas"][schema["$ref"].removeprefix(_SCHEMAS)]


def json_schema(document: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """The schema of a JSON request body or answer of the document."""
    return schema_of(document, body["content"]["application/json"]["schema"])


def undescribed(document: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """The fields of an object's schema that carry no description, the
    fields of the objects it holds included."""
    schema = schema_of(document, schema)
    missing = []
    for name, field in schema.get("properties", {}).items():
        if not field.get("description"):
            missing.append(name)
        held = [field, field.get("items", {}), *field.get("anyOf", [])]
        for part in held:
            missing += [f"{name}.{inner}" for inner in undescribed(document, part)]
    return missing
