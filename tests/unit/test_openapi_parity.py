from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_parity_module() -> ModuleType:
    path = Path(__file__).parents[2] / ".github" / "ci" / "openapi_parity.py"
    spec = importlib.util.spec_from_file_location("openapi_parity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parity = _load_parity_module()


def _document(*, value_type: str = "string", include_auth: bool = False) -> dict:
    parameters = []
    if include_auth:
        parameters.append(
            {
                "in": "header",
                "name": "Authorization",
                "schema": {"type": "string", "title": "Authorization"},
            }
        )
    return {
        "paths": {
            "/v1/value": {
                "get": {
                    "parameters": parameters,
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Value"}
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Value": {
                    "title": "Value",
                    "type": "object",
                    "properties": {"value": {"type": value_type}},
                    "required": ["value"],
                }
            }
        },
    }


def test_parity_ignores_auth_headers_and_documentation_annotations() -> None:
    full = _document(include_auth=True)
    lite = _document()
    full["components"]["schemas"]["Value"]["description"] = "Full documentation"
    lite["components"]["schemas"]["Value"]["description"] = "Lite documentation"

    assert parity._contract_differences(full, lite) == []


def test_parity_detects_wire_schema_difference() -> None:
    differences = parity._contract_differences(
        _document(value_type="string"),
        _document(value_type="integer"),
    )

    assert differences == ["schema differs: Value"]


def test_parity_detects_missing_operation() -> None:
    full = _document()
    lite = _document()
    lite["paths"]["/v1/value"].pop("get")

    assert parity._contract_differences(full, lite) == [
        "operation exists only in Full: GET /v1/value"
    ]
