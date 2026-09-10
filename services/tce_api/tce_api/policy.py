from __future__ import annotations

from datetime import datetime
from typing import Any

from tce_shared.events import AgentRole
from tce_shared.policy import ConsumerContext, PolicyDecision
from tce_shared.scope import ResolvedScope

from .config import get_settings


class PolicyEngine:
    def __init__(self) -> None:
        self.settings = get_settings()

    def resolve_consumer(
        self,
        consumer: str,
        role: AgentRole = AgentRole.USER,
        workspace_id: str = "personal",
        owner_id: str = "user",
        scope: ResolvedScope | None = None,
    ) -> ConsumerContext:
        max_sensitivity = max(0, self.settings.block_sensitivity - 1)
        if role == AgentRole.ADVISOR:
            max_sensitivity = min(max_sensitivity, 2)
        return ConsumerContext(
            consumer=consumer,
            allowed_domains=["*"],
            max_sensitivity=max_sensitivity,
            workspace_id=workspace_id,
            owner_id=owner_id,
            scope=scope,
        )

    def evaluate(
        self,
        consumer_ctx: ConsumerContext,
        domain: str,
        sensitivity: int,
        ts: datetime | None = None,
    ) -> PolicyDecision:
        if sensitivity > consumer_ctx.max_sensitivity:
            return PolicyDecision(
                allow=False,
                reason=f"sensitivity {sensitivity} exceeds max allowed {consumer_ctx.max_sensitivity}",
            )
        if consumer_ctx.allowed_domains != ["*"] and domain not in consumer_ctx.allowed_domains:
            return PolicyDecision(allow=False, reason=f"domain {domain} not allowed")
        _ = ts
        return PolicyDecision(allow=True, reason="allowed")

    def summarize(self, blocked_count: int, applied_redactions: list[str], role: AgentRole | None = None) -> dict[str, Any]:
        return {
            "policy_profile": "user-only",
            "blocked_count": blocked_count,
            "applied_redactions": applied_redactions,
            "block_sensitivity": self.settings.block_sensitivity,
            "role": role.value if role else AgentRole.USER.value,
        }
