#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os

from sqlalchemy import create_engine, text


def expected_hash(row: dict) -> str:
    digest_input = {
        "schema_version": row["schema_version"],
        "ts": row["ts"].isoformat(),
        "actor": row["actor"],
        "source": row["source"],
        "domain": row["domain"],
        "task_type": row["task_type"],
        "event_type": row["event_type"],
        "title": row["title"],
        "payload": row["payload"],
    }
    return hashlib.sha256(json.dumps(digest_input, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def main() -> None:
    url = os.getenv("TCE_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/tce")
    engine = create_engine(url)
    mismatches = 0
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, ts, actor, source, domain, task_type, event_type, title, payload, schema_version, hash
                FROM events
                ORDER BY ts DESC
                LIMIT 5000
                """
            )
        ).mappings()
        for row in rows:
            actual = expected_hash(dict(row))
            if actual != row["hash"]:
                mismatches += 1
                print(f"hash mismatch event_id={row['id']}")
    print(f"integrity check complete mismatches={mismatches}")


if __name__ == "__main__":
    main()
