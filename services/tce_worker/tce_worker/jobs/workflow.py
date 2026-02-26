from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select

from ..db import SessionLocal
from ..models import Pattern, WorkflowTemplate


def _build_graph(pattern: Pattern) -> dict:
    statement = pattern.statement.strip()
    pattern_node = statement[:140] if statement else f"{pattern.domain} pattern"
    return {
        "nodes": [
            {"id": "start", "label": "Start"},
            {"id": "analyze", "label": f"Analyze: {pattern.domain}"},
            {"id": "pattern", "label": pattern_node},
            {"id": "validate", "label": "Validate with evidence"},
            {"id": "done", "label": "Done"},
        ],
        "edges": [
            {"from": "start", "to": "analyze"},
            {"from": "analyze", "to": "pattern"},
            {"from": "pattern", "to": "validate"},
            {"from": "validate", "to": "done"},
        ],
    }


def run(pattern_id: str) -> dict:
    parsed_id = UUID(pattern_id)
    with SessionLocal() as db:
        pattern = db.execute(select(Pattern).where(Pattern.id == parsed_id)).scalar_one_or_none()
        if pattern is None:
            return {"status": "missing", "pattern_id": pattern_id}

        template_name = f"{pattern.domain}:{pattern.pattern_type}:{pattern.id}"
        existing = db.execute(
            select(WorkflowTemplate)
            .where(WorkflowTemplate.name == template_name)
            .order_by(desc(WorkflowTemplate.updated_at))
            .limit(1)
        ).scalar_one_or_none()

        if existing:
            existing.domain = pattern.domain
            existing.graph = _build_graph(pattern)
            existing.triggers = {
                "domain": pattern.domain,
                "pattern_type": pattern.pattern_type,
                "min_confidence": pattern.confidence,
            }
            existing.version += 1
            existing.updated_at = datetime.now(tz=UTC)
            db.commit()
            return {"status": "updated", "pattern_id": pattern_id, "workflow_id": str(existing.id)}

        template = WorkflowTemplate(
            name=template_name,
            domain=pattern.domain,
            graph=_build_graph(pattern),
            triggers={
                "domain": pattern.domain,
                "pattern_type": pattern.pattern_type,
                "min_confidence": pattern.confidence,
            },
            version=1,
            updated_at=datetime.now(tz=UTC),
        )
        db.add(template)
        db.commit()
        db.refresh(template)
    return {"status": "created", "pattern_id": pattern_id, "workflow_id": str(template.id)}
