# ruff: noqa: E501 -- embedded TypeScript is formatted by the generated target.

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from star.main import app

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "frontend" / "src" / "api" / "generated" / "openapiContract.ts"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _typescript(schema: dict[str, Any]) -> str:
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        name = reference.rsplit("/", 1)[-1]
        return f'components["schemas"][{json.dumps(name)}]'
    if "const" in schema:
        return json.dumps(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list):
        return " | ".join(json.dumps(value) for value in enum) or "never"
    for union_key in ("anyOf", "oneOf"):
        options = schema.get(union_key)
        if isinstance(options, list):
            return " | ".join(f"({_typescript(option)})" for option in options)
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        return " & ".join(f"({_typescript(option)})" for option in all_of)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return " | ".join(_typescript({**schema, "type": item}) for item in schema_type)
    if schema_type == "array":
        return f"Array<{_typescript(schema.get('items', {}))}>"
    if schema_type == "object" or "properties" in schema or "additionalProperties" in schema:
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        members = [
            f"{json.dumps(name)}{' ' if name in required else '?'}: {_typescript(value)};"
            for name, value in properties.items()
        ]
        additional = schema.get("additionalProperties")
        if additional is True:
            members.append("[key: string]: unknown;")
        elif isinstance(additional, dict):
            members.append(f"[key: string]: {_typescript(additional)};")
        if not members:
            return "Record<string, unknown>"
        return "{ " + " ".join(members) + " }"
    if schema_type in {"integer", "number"}:
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"
    if schema_type == "string":
        return "string"
    return "unknown"


def _response_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    responses = operation.get("responses", {})
    for status_code in sorted(responses):
        if not str(status_code).startswith("2"):
            continue
        response = responses[status_code]
        if "$ref" in response:
            continue
        content = response.get("content", {})
        media = content.get("application/json")
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"]
    return None


def _operation_schemas(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path, path_item in document.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            schema = _response_schema(operation)
            if schema is not None:
                result[f"{method.upper()} {path}"] = schema
    return result


def _schema_patterns(value: object) -> list[str]:
    patterns: set[str] = set()

    def collect(item: object) -> None:
        if isinstance(item, dict):
            pattern = item.get("pattern")
            if isinstance(pattern, str):
                patterns.add(pattern)
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    return sorted(patterns)


def _runtime_validator() -> str:
    return r"""
type JsonSchema = Record<string, unknown>;

function schemaRef(schema: JsonSchema): JsonSchema {
  const reference = schema.$ref;
  if (typeof reference !== "string") return schema;
  const name = reference.split("/").at(-1);
  if (!name || !(name in API_COMPONENT_SCHEMAS)) throw new Error(`Unknown API schema ${reference}`);
  return API_COMPONENT_SCHEMAS[name as keyof typeof API_COMPONENT_SCHEMAS] as JsonSchema;
}

function matchCombinations(value: unknown, schema: JsonSchema, location: string): string | null {
  for (const key of ["anyOf", "oneOf"] as const) {
    const variants = schema[key];
    if (Array.isArray(variants)) {
      return variants.some((variant) => matchesSchema(value, variant as JsonSchema, location) === null)
        ? null
        : `${location} does not match ${key}`;
    }
  }
  const allOf = schema.allOf;
  if (Array.isArray(allOf)) {
    for (const variant of allOf) {
      const error = matchesSchema(value, variant as JsonSchema, location);
      if (error) return error;
    }
  }
  return null;
}

function matchLiteral(value: unknown, schema: JsonSchema, location: string): string | null {
  const enumValues = schema.enum;
  if (Array.isArray(enumValues) && !enumValues.some((item) => Object.is(item, value))) {
    return `${location} is outside the API enum`;
  }
  if ("const" in schema && !Object.is(schema.const, value)) return `${location} has the wrong constant`;
  return null;
}

function matchString(value: unknown, schema: JsonSchema, location: string): string | null {
  const type = schema.type;
  if (type === "null") return value === null ? null : `${location} must be null`;
  if (type !== "string") return null;
  if (typeof value !== "string") return `${location} must be a string`;
  if (typeof schema.minLength === "number" && value.length < schema.minLength) return `${location} is too short`;
  if (typeof schema.maxLength === "number" && value.length > schema.maxLength) return `${location} is too long`;
  if (typeof schema.pattern === "string" && !API_SCHEMA_PATTERNS[schema.pattern]?.test(value)) return `${location} has an invalid format`;
  return null;
}

function matchNumber(value: unknown, schema: JsonSchema, location: string): string | null {
  const type = schema.type;
  if ((type === "number" || type === "integer") && (typeof value !== "number" || !Number.isFinite(value))) return `${location} must be a finite number`;
  if (type === "integer" && !Number.isInteger(value)) return `${location} must be an integer`;
  if (type !== "number" && type !== "integer") return null;
  if (typeof value !== "number") return null;
  if (typeof schema.minimum === "number" && value < schema.minimum) return `${location} is below the minimum`;
  if (typeof schema.maximum === "number" && value > schema.maximum) return `${location} is above the maximum`;
  return null;
}

function matchPrimitive(value: unknown, schema: JsonSchema, location: string): string | null {
  const stringError = matchString(value, schema, location);
  if (stringError) return stringError;
  const numberError = matchNumber(value, schema, location);
  if (numberError) return numberError;
  if (schema.type === "boolean" && typeof value !== "boolean") return `${location} must be a boolean`;
  return null;
}

function matchArray(value: unknown, schema: JsonSchema, location: string): string | null {
  if (schema.type !== "array") return null;
  if (!Array.isArray(value)) return `${location} must be an array`;
  if (typeof schema.minItems === "number" && value.length < schema.minItems) return `${location} has too few items`;
  if (typeof schema.maxItems === "number" && value.length > schema.maxItems) return `${location} has too many items`;
  if (schema.uniqueItems === true && new Set(value.map((item) => JSON.stringify(item))).size !== value.length) return `${location} must contain unique items`;
  if (!schema.items || typeof schema.items !== "object") return null;
  for (let index = 0; index < value.length; index += 1) {
    const error = matchesSchema(value[index], schema.items as JsonSchema, `${location}[${index}]`);
    if (error) return error;
  }
  return null;
}

function matchObjectProperties(record: Record<string, unknown>, schema: JsonSchema, location: string): string | null {
  const properties = schema.properties;
  const required = Array.isArray(schema.required) ? schema.required : [];
  for (const name of required) if (typeof name === "string" && !(name in record)) return `${location}.${name} is required`;
  if (!properties || typeof properties !== "object") return null;
  for (const [name, propertySchema] of Object.entries(properties)) {
    if (!(name in record)) continue;
    const error = matchesSchema(record[name], propertySchema as JsonSchema, `${location}.${name}`);
    if (error) return error;
  }
  return null;
}

function matchAdditionalProperties(record: Record<string, unknown>, schema: JsonSchema, location: string): string | null {
  const properties = schema.properties;
  const known = properties && typeof properties === "object" ? new Set(Object.keys(properties)) : new Set<string>();
  for (const [name, item] of Object.entries(record)) {
    if (known.has(name)) continue;
    if (schema.additionalProperties === false) return `${location}.${name} is not allowed`;
    if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
      const error = matchesSchema(item, schema.additionalProperties as JsonSchema, `${location}.${name}`);
      if (error) return error;
    }
  }
  return null;
}

function matchObject(value: unknown, schema: JsonSchema, location: string): string | null {
  if (schema.type !== "object" && !schema.properties) return null;
  if (value === null || typeof value !== "object" || Array.isArray(value)) return `${location} must be an object`;
  const record = value as Record<string, unknown>;
  return matchObjectProperties(record, schema, location) ?? matchAdditionalProperties(record, schema, location);
}

function matchesSchema(value: unknown, rawSchema: JsonSchema, location: string): string | null {
  const schema = schemaRef(rawSchema);
  return matchCombinations(value, schema, location)
    ?? matchLiteral(value, schema, location)
    ?? matchPrimitive(value, schema, location)
    ?? matchArray(value, schema, location)
    ?? matchObject(value, schema, location);
}

function pathMatchesTemplate(path: string, template: string): boolean {
  const pathSegments = path.split("/");
  const templateSegments = template.split("/");
  return pathSegments.length === templateSegments.length && templateSegments.every(
    (segment, index) => (segment.startsWith("{") && segment.endsWith("}")) || segment === pathSegments[index],
  );
}

function operationKey(method: string, path: string): keyof typeof API_RESPONSE_SCHEMAS | null {
  const normalizedPath = path.split("?", 1)[0]!.replace(/^\/api\/v1/, "") || "/";
  const fullPath = `/api/v1${normalizedPath}`;
  for (const key of Object.keys(API_RESPONSE_SCHEMAS) as Array<keyof typeof API_RESPONSE_SCHEMAS>) {
    const separator = key.indexOf(" ");
    if (key.slice(0, separator) !== method.toUpperCase()) continue;
    const template = key.slice(separator + 1);
    if (pathMatchesTemplate(fullPath, template)) return key;
  }
  return null;
}

export function validateApiResponse(method: string, path: string, value: unknown): void {
  const key = operationKey(method, path);
  if (key === null) return;
  const error = matchesSchema(value, API_RESPONSE_SCHEMAS[key] as JsonSchema, "response");
  if (error) throw new Error(`API response contract violation for ${key}: ${error}`);
}
"""


def rendered_contract() -> str:
    document = app.openapi()
    schemas: dict[str, dict[str, Any]] = document.get("components", {}).get("schemas", {})
    operations = _operation_schemas(document)
    patterns = _schema_patterns({"schemas": schemas, "operations": operations})
    component_members = "\n".join(
        f"      {json.dumps(name)}: {_typescript(schema)};"
        for name, schema in sorted(schemas.items())
    )
    operation_members = "\n".join(
        f"  {json.dumps(key)}: {_typescript(schema)};" for key, schema in sorted(operations.items())
    )
    return (
        "// Generated by scripts/generate_openapi_contract.py. Do not edit.\n"
        "export interface components {\n  schemas: {\n" + component_members + "\n  };\n}\n\n"
        "export interface ApiOperationResponses {\n" + operation_members + "\n}\n\n"
        "const API_COMPONENT_SCHEMAS = "
        + json.dumps(schemas, sort_keys=True, separators=(",", ":"))
        + " as const;\n\n"
        "const API_RESPONSE_SCHEMAS = "
        + json.dumps(operations, sort_keys=True, separators=(",", ":"))
        + " as const;\n\n"
        + "const API_SCHEMA_PATTERNS: Record<string, RegExp> = {\n"
        + "\n".join(
            f"  {json.dumps(pattern)}: new RegExp({json.dumps(pattern)}),"
            for pattern in patterns
        )
        + "\n};\n"
        + _runtime_validator()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_contract()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            raise SystemExit(
                "generated OpenAPI contract is stale; run "
                "`uv run python scripts/generate_openapi_contract.py`"
            )
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
