#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
AUTH_HEADERS = {
    "authorization",
    "x-mtls-subject",
    "x-tce-behavior-subject",
    "x-tce-consumer",
    "x-tce-role",
    "x-tce-user",
    "x-tce-workspace",
}
NON_WIRE_KEYS = {"description", "example", "examples", "title"}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read OpenAPI document {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"OpenAPI document {path} must contain an object")
    return value


def _business_parameters(operation: dict[str, Any]) -> list[dict[str, Any]]:
    parameters = operation.get("parameters") or []
    return sorted(
        (
            parameter
            for parameter in parameters
            if not (
                str(parameter.get("in") or "").lower() == "header"
                and str(parameter.get("name") or "").lower() in AUTH_HEADERS
            )
        ),
        key=lambda item: (str(item.get("in") or ""), str(item.get("name") or "")),
    )


def _wire_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _wire_shape(item)
            for key, item in sorted(value.items())
            if key not in NON_WIRE_KEYS
        }
    if isinstance(value, list):
        return [_wire_shape(item) for item in value]
    return value


def _operation_contract(operation: dict[str, Any]) -> dict[str, Any]:
    return _wire_shape({
        "parameters": _business_parameters(operation),
        "requestBody": operation.get("requestBody"),
        "responses": operation.get("responses"),
    })


def _contract_differences(full: dict[str, Any], lite: dict[str, Any]) -> list[str]:
    differences: list[str] = []
    full_paths = full.get("paths") or {}
    lite_paths = lite.get("paths") or {}
    if not isinstance(full_paths, dict) or not isinstance(lite_paths, dict):
        return ["both OpenAPI documents must contain a paths object"]

    full_path_names = set(full_paths)
    lite_path_names = set(lite_paths)
    for path in sorted(full_path_names - lite_path_names):
        differences.append(f"path exists only in Full: {path}")
    for path in sorted(lite_path_names - full_path_names):
        differences.append(f"path exists only in Lite: {path}")

    for path in sorted(full_path_names & lite_path_names):
        full_methods = HTTP_METHODS & set(full_paths[path])
        lite_methods = HTTP_METHODS & set(lite_paths[path])
        for method in sorted(full_methods - lite_methods):
            differences.append(f"operation exists only in Full: {method.upper()} {path}")
        for method in sorted(lite_methods - full_methods):
            differences.append(f"operation exists only in Lite: {method.upper()} {path}")
        for method in sorted(full_methods & lite_methods):
            full_contract = _operation_contract(full_paths[path][method])
            lite_contract = _operation_contract(lite_paths[path][method])
            if full_contract != lite_contract:
                differences.append(f"request/response contract differs: {method.upper()} {path}")

    full_schemas = ((full.get("components") or {}).get("schemas") or {})
    lite_schemas = ((lite.get("components") or {}).get("schemas") or {})
    if not isinstance(full_schemas, dict) or not isinstance(lite_schemas, dict):
        differences.append("both OpenAPI documents must contain components.schemas")
        return differences
    full_schema_names = set(full_schemas)
    lite_schema_names = set(lite_schemas)
    for name in sorted(full_schema_names - lite_schema_names):
        differences.append(f"schema exists only in Full: {name}")
    for name in sorted(lite_schema_names - full_schema_names):
        differences.append(f"schema exists only in Lite: {name}")
    for name in sorted(full_schema_names & lite_schema_names):
        if _wire_shape(full_schemas[name]) != _wire_shape(lite_schemas[name]):
            differences.append(f"schema differs: {name}")
    return differences


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify semantic API parity between Full and Lite OpenAPI documents")
    parser.add_argument("full", type=Path)
    parser.add_argument("lite", type=Path)
    args = parser.parse_args()
    try:
        differences = _contract_differences(_load(args.full), _load(args.lite))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if differences:
        print("Full/Lite OpenAPI parity failed:", file=sys.stderr)
        for difference in differences:
            print(f" - {difference}", file=sys.stderr)
        return 1
    print("Full/Lite OpenAPI request, response, and schema contracts match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
