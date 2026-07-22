from __future__ import annotations

import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TCE_", extra="ignore")

    api_base_url: str = "http://tce-api:8080"
    api_token: str = "local-dev-token"
    mcp_transport: str = "stdio"
    mcp_consumer_id: str = "mcp-client"
    mcp_role: str = "executor"
    mcp_workspace_id: str = "personal"
    mcp_user_id: str = "local-user"
    mcp_behavior_subject_id: str = ""
    mcp_session_id: str = ""
    mcp_http_retry_total: int = 3
    mcp_http_retry_backoff_seconds: float = 0.4
    mcp_http_retry_statuses: str = "429,500,502,503,504"
    mcp_http_timeout_seconds: float = 90.0

    @property
    def mcp_effective_session_id(self) -> str:
        explicit = str(self.mcp_session_id or "").strip()
        if explicit:
            return explicit

        source = str(self.mcp_user_id or "").strip().lower()
        if not source:
            source = str(self.mcp_role or "").strip().lower()

        for suffix in ("-executor", "_executor", "executor", "-advisor", "_advisor", "advisor"):
            if source.endswith(suffix):
                source = source[: -len(suffix)].rstrip("-_")
                break

        source = re.sub(r"[^a-z0-9_-]+", "-", source).strip("-_")
        return source or "default"

    @property
    def mcp_effective_behavior_subject_id(self) -> str:
        return str(self.mcp_behavior_subject_id or self.mcp_user_id).strip() or "local-user"

    @property
    def mcp_retry_status_codes(self) -> tuple[int, ...]:
        result: list[int] = []
        for token in self.mcp_http_retry_statuses.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                result.append(int(token))
            except ValueError:
                continue
        return tuple(result) or (429, 500, 502, 503, 504)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
