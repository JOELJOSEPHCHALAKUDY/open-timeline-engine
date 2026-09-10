from __future__ import annotations

from dataclasses import dataclass, field

from .scope import ResolvedScope


@dataclass(slots=True)
class PolicyDecision:
    allow: bool
    reason: str
    applied_redactions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConsumerContext:
    consumer: str
    allowed_domains: list[str]
    max_sensitivity: int
    workspace_id: str = "personal"
    owner_id: str = "user"
    scope: ResolvedScope | None = None
