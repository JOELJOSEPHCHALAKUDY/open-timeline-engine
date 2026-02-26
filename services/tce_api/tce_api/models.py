from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, REAL, TEXT, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("idx_events_domain_task_type_ts", "domain", "task_type", "ts"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    actor: Mapped[str] = mapped_column(TEXT, nullable=False)
    source: Mapped[str] = mapped_column(TEXT, nullable=False)
    domain: Mapped[str] = mapped_column(TEXT, nullable=False)
    task_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    event_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    title: Mapped[str] = mapped_column(TEXT, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    inputs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    style: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    links: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sensitivity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    redaction_hints: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    redaction_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_id: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    source_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vector_clock: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    authority_level: Mapped[str] = mapped_column(TEXT, nullable=False, default="incidental")
    hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class EventEmbedding(Base):
    __tablename__ = "event_embeddings"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    model: Mapped[str] = mapped_column(TEXT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(TEXT, nullable=False)
    uri: Mapped[str] = mapped_column(TEXT, nullable=False)
    sha256: Mapped[str] = mapped_column(TEXT, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Pattern(Base):
    __tablename__ = "patterns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    pattern_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    statement: Mapped[str] = mapped_column(TEXT, nullable=False)
    evidence_event_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(TEXT, nullable=False, default="needs_review")


class WorkflowTemplate(Base):
    __tablename__ = "workflow_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(TEXT, nullable=False)
    domain: Mapped[str] = mapped_column(TEXT, nullable=False)
    graph: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    triggers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextBundleCache(Base):
    __tablename__ = "context_bundles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_hash: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)
    bundle: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumer: Mapped[str] = mapped_column(TEXT, nullable=False)
    action: Mapped[str] = mapped_column(TEXT, nullable=False)
    query: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_event_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    policy_decisions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)
    rules: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PatternFeedback(Base):
    __tablename__ = "pattern_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pattern_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patterns.id", ondelete="CASCADE"), nullable=False
    )
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(TEXT, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TakeoverSession(Base):
    __tablename__ = "takeover_sessions"

    session_id: Mapped[str] = mapped_column(TEXT, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, primary_key=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(TEXT, nullable=False, default="takeover")
    persona_mode: Mapped[str] = mapped_column(TEXT, nullable=False, default="normal")
    activation_keywords: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    stop_keywords: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    takeover_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    objective_hash: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    working_set_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_deliberation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recent_outcomes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    autonomy_score: Mapped[float] = mapped_column(REAL, nullable=False, default=0.5)
    autonomy_policy_profile: Mapped[str] = mapped_column(TEXT, nullable=False, default="human_consultative")
    active_goal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    goal_queue_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_discovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    continuity_violation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enforcement_mode: Mapped[str] = mapped_column(TEXT, nullable=False, default="strict_takeover")
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_directive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_backlog_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_classification: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    last_safety_decision: Mapped[str] = mapped_column(TEXT, nullable=False, default="allow")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionMemorySnapshot(Base):
    __tablename__ = "session_memory_snapshots"
    __table_args__ = (
        Index(
            "idx_session_memory_snapshots_lookup",
            "workspace_id",
            "user_id",
            "session_id",
            "objective_hash",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    objective_hash: Mapped[str | None] = mapped_column(TEXT, nullable=True, index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class HandoffRecord(Base):
    __tablename__ = "handoff_records"
    __table_args__ = (
        Index("idx_handoff_records_workspace_owner_ts", "workspace_id", "owner_id", "ts"),
        Index("idx_handoff_records_workspace_ts", "workspace_id", "ts"),
        Index("idx_handoff_records_workspace_session_ts", "workspace_id", "session_id", "ts"),
        Index("idx_handoff_records_workspace_owner_objective", "workspace_id", "owner_id", "objective_text"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    directive_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    title: Mapped[str] = mapped_column(TEXT, nullable=False)
    decision: Mapped[str] = mapped_column(TEXT, nullable=False)
    next_step: Mapped[str] = mapped_column(TEXT, nullable=False)
    status: Mapped[str] = mapped_column(TEXT, nullable=False, default="succeeded")
    files_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    anchors_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    git_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    change_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    objective_text: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    source: Mapped[str] = mapped_column(TEXT, nullable=False, default="native")
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    schema_version: Mapped[str] = mapped_column(TEXT, nullable=False, default="v1")
    redaction_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class TakeoverActionLog(Base):
    __tablename__ = "takeover_action_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    turn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    objective_hash: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    action_kind: Mapped[str] = mapped_column(TEXT, nullable=False)
    result: Mapped[str] = mapped_column(TEXT, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class AutonomyGoal(Base):
    __tablename__ = "autonomy_goals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    title: Mapped[str] = mapped_column(TEXT, nullable=False)
    description: Mapped[str] = mapped_column(TEXT, nullable=False)
    source: Mapped[str] = mapped_column(TEXT, nullable=False, default="open_discovery")
    priority_score: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    risk_tier: Mapped[str] = mapped_column(TEXT, nullable=False, default="medium")
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    reasoning: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    evidence_event_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    goal_kind: Mapped[str] = mapped_column(TEXT, nullable=False, default="normal")
    affective_scores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    selection_score: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    goal_embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    goal_signature: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    parent_goal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_source: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    status: Mapped[str] = mapped_column(TEXT, nullable=False, default="candidate")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class AutonomyGoalCache(Base):
    __tablename__ = "autonomy_goal_cache"

    cache_key: Mapped[str] = mapped_column(TEXT, primary_key=True)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    cache_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AutonomyNotice(Base):
    __tablename__ = "autonomy_notices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(TEXT, nullable=False)
    reason: Mapped[str] = mapped_column(TEXT, nullable=False)
    priority: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DirectiveExecution(Base):
    __tablename__ = "directive_executions"
    __table_args__ = (
        Index("idx_directive_executions_session_state_started", "session_id", "state", "started_at"),
    )

    directive_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    objective_hash: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    action_kind: Mapped[str] = mapped_column(TEXT, nullable=False, default="execute")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(TEXT, nullable=False, default="pending")
    requires_permit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    retry_strategy: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ExecutionPermit(Base):
    __tablename__ = "execution_permits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    action_kind: Mapped[str] = mapped_column(TEXT, nullable=False)
    target_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    command_preview: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    estimated_change_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision: Mapped[str] = mapped_column(TEXT, nullable=False, default="allow")
    reason: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    confirmed_by: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventIdentity(Base):
    __tablename__ = "event_identity"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingest_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, default="default", index=True)
    goal: Mapped[str] = mapped_column(TEXT, nullable=False)
    context: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(TEXT, nullable=False, default="open")
    authority_score: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    stability_score: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class EpisodeDecision(Base):
    __tablename__ = "episode_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(TEXT, nullable=False)
    why: Mapped[str] = mapped_column(TEXT, nullable=False, default="")
    alternatives_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EpisodeLesson(Base):
    __tablename__ = "episode_lessons"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    do_more_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    do_less_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    avoid_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EpisodeEventLink(Base):
    __tablename__ = "episode_event_links"
    __table_args__ = (
        Index("idx_episode_event_links_scope", "episode_id", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryRule(Base):
    __tablename__ = "memory_rules"
    __table_args__ = (
        Index("idx_memory_rules_scope_priority", "workspace_id", "user_id", "active", "priority"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    scope: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rule_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    statement: Mapped[str] = mapped_column(TEXT, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    evergreen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_episode_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryTombstone(Base):
    __tablename__ = "memory_tombstones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    target_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(TEXT, nullable=False, default="user_requested")
    requested_by: Mapped[str] = mapped_column(TEXT, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (
        Index("idx_entity_aliases_scope_key", "workspace_id", "owner_id", "alias_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    canonical_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    alias_key: Mapped[str] = mapped_column(TEXT, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, default=0.5)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventFingerprint(Base):
    __tablename__ = "event_fingerprints"
    __table_args__ = (
        Index("idx_event_fingerprints_scope_hash", "workspace_id", "owner_id", "fingerprint_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    fingerprint_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentInteraction(Base):
    __tablename__ = "agent_interactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interaction_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    source_consumer: Mapped[str] = mapped_column(TEXT, nullable=False)
    source_role: Mapped[str] = mapped_column(TEXT, nullable=False)
    target_role: Mapped[str] = mapped_column(TEXT, nullable=False)
    action: Mapped[str] = mapped_column(TEXT, nullable=False)
    citations: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str] = mapped_column(TEXT, nullable=False, default="ok")


class DecisionObservation(Base):
    __tablename__ = "decision_observations"
    __table_args__ = (
        Index("idx_decision_obs_consumer", "consumer_id", "workspace_id", "situation_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consumer_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, default="default")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    situation_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    situation_summary: Mapped[str] = mapped_column(TEXT, nullable=False)
    context_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    user_response: Mapped[str] = mapped_column(TEXT, nullable=False)
    response_reasoning: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    outcome: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    outcome_sentiment: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    source_event_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, default=1.0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)


class BehavioralFingerprint(Base):
    __tablename__ = "behavioral_fingerprints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consumer_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, default="default")
    fingerprint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CloneFeedback(Base):
    __tablename__ = "clone_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_observations.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    feedback_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    correction_text: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DashboardHumanScoreSnapshot(Base):
    __tablename__ = "dashboard_human_score_snapshots"
    __table_args__ = (
        Index(
            "idx_dashboard_human_score_scope_created",
            "workspace_id",
            "user_id",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(TEXT, nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    band: Mapped[str] = mapped_column(TEXT, nullable=False)
    subscores_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    inputs_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
