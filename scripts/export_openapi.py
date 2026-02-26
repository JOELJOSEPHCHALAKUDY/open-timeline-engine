#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from tce_api.main import app as full_app
from tce_lite_api.main import app as lite_app


def write_spec(target: Path, spec: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out_dir = root / "docs" / "openapi"

    write_spec(out_dir / "tce_api.v1.json", full_app.openapi())
    write_spec(out_dir / "tce_lite_api.v1.json", lite_app.openapi())
    print(f"Exported OpenAPI specs to {out_dir}")


if __name__ == "__main__":
    main()
