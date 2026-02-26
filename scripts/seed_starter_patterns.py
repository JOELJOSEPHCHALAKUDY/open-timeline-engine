#!/usr/bin/env python3
from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from tce_api.models import Pattern, WorkflowTemplate

SEED_PATTERNS = [
    {
        "domain": "coding",
        "pattern_type": "workflow",
        "statement": "Start implementation by locking API or interface contracts first.",
    },
    {
        "domain": "coding",
        "pattern_type": "preference",
        "statement": "Prefer incremental changes with clear citations to prior evidence.",
    },
    {
        "domain": "planning",
        "pattern_type": "workflow",
        "statement": "Define success criteria before selecting tooling or implementation path.",
    },
    {
        "domain": "research",
        "pattern_type": "workflow",
        "statement": "Collect evidence first, then summarize decisions with source references.",
    },
]

SEED_WORKFLOWS = [
    {
        "name": "coding-default-loop",
        "domain": "coding",
        "graph": {
            "nodes": [
                {"id": "scope", "label": "Scope task"},
                {"id": "contract", "label": "Define contract"},
                {"id": "implement", "label": "Implement"},
                {"id": "verify", "label": "Verify"},
            ],
            "edges": [
                {"from": "scope", "to": "contract"},
                {"from": "contract", "to": "implement"},
                {"from": "implement", "to": "verify"},
            ],
        },
        "triggers": {"domain": "coding", "cold_start": True},
    }
]


def main() -> None:
    database_url = os.getenv("TCE_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/tce")
    engine = create_engine(database_url, future=True)

    inserted_patterns = 0
    inserted_workflows = 0

    with Session(engine) as db:
        for seed in SEED_PATTERNS:
            existing = db.execute(
                select(Pattern).where(
                    Pattern.domain == seed["domain"],
                    Pattern.pattern_type == seed["pattern_type"],
                    Pattern.statement == seed["statement"],
                )
            ).scalar_one_or_none()
            if existing:
                continue

            db.add(
                Pattern(
                    domain=seed["domain"],
                    pattern_type=seed["pattern_type"],
                    statement=seed["statement"],
                    evidence_event_ids=[],
                    confidence=0.52,
                    updated_at=datetime.now(tz=UTC),
                    version=1,
                    status="needs_review",
                )
            )
            inserted_patterns += 1

        for seed in SEED_WORKFLOWS:
            existing = db.execute(
                select(WorkflowTemplate).where(
                    WorkflowTemplate.domain == seed["domain"],
                    WorkflowTemplate.name == seed["name"],
                )
            ).scalar_one_or_none()
            if existing:
                continue

            db.add(
                WorkflowTemplate(
                    name=seed["name"],
                    domain=seed["domain"],
                    graph=seed["graph"],
                    triggers=seed["triggers"],
                    version=1,
                    updated_at=datetime.now(tz=UTC),
                )
            )
            inserted_workflows += 1

        db.commit()

    print(
        f"seed complete patterns_inserted={inserted_patterns} workflows_inserted={inserted_workflows}"
    )


if __name__ == "__main__":
    main()
