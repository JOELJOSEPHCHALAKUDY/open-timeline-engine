from __future__ import annotations

import time
from typing import Any, cast
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import get_settings


class TCEApiClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.api_base_url.rstrip("/")
        self.request_timeout_seconds = max(5.0, float(getattr(settings, "mcp_http_timeout_seconds", 90.0)))
        self.default_session_id = settings.mcp_effective_session_id
        self.headers = {
            "Authorization": f"Bearer {settings.api_token}",
            "Content-Type": "application/json",
            "X-TCE-Consumer": settings.mcp_consumer_id,
            "X-TCE-Role": settings.mcp_role,
            "X-TCE-Workspace": settings.mcp_workspace_id,
            "X-TCE-User": settings.mcp_user_id,
            "X-TCE-Behavior-Subject": settings.mcp_effective_behavior_subject_id,
        }
        retries = Retry(
            total=max(0, settings.mcp_http_retry_total),
            connect=max(0, settings.mcp_http_retry_total),
            read=max(0, settings.mcp_http_retry_total),
            status=max(0, settings.mcp_http_retry_total),
            backoff_factor=max(0.0, settings.mcp_http_retry_backoff_seconds),
            status_forcelist=settings.mcp_retry_status_codes,
            allowed_methods=frozenset({"GET", "POST", "PUT"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session = requests.Session()
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _normalize_session_id(self, value: Any) -> str:
        current = str(value or "").strip()
        if current and current.lower() != "default":
            return current
        return self.default_session_id

    def _normalize_params(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        params = kwargs.get("params")
        if not isinstance(params, dict) or "session_id" not in params:
            return kwargs
        normalized = dict(kwargs)
        normalized_params = dict(params)
        normalized_params["session_id"] = self._normalize_session_id(normalized_params.get("session_id"))
        normalized["params"] = normalized_params
        return normalized

    def _normalize_body(self, body: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(body)
        if "session_id" in normalized:
            normalized["session_id"] = self._normalize_session_id(normalized.get("session_id"))
        return normalized

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            timeout=self.request_timeout_seconds,
            **kwargs,
        )
        if response.status_code != 429:
            return response
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                sleep_seconds = min(5.0, max(0.0, float(retry_after)))
            except ValueError:
                sleep_seconds = 0.0
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        return self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            timeout=self.request_timeout_seconds,
            **kwargs,
        )

    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", path, **self._normalize_params(kwargs))

    def _post(self, path: str, body: dict[str, Any]) -> requests.Response:
        return self._request("POST", path, json=self._normalize_body(body))

    def _put(self, path: str, body: dict[str, Any]) -> requests.Response:
        return self._request("PUT", path, json=self._normalize_body(body))

    @staticmethod
    def _json_dict(response: requests.Response) -> dict[str, Any]:
        payload = response.json()
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
        return {"data": payload}

    @staticmethod
    def _json_list_dict(response: requests.Response) -> list[dict[str, Any]]:
        payload = response.json()
        if not isinstance(payload, list):
            return []
        output: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                output.append(cast(dict[str, Any], item))
        return output

    def search_events(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/search", body)
        response.raise_for_status()
        return self._json_dict(response)

    def context_bundle(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/context_bundle", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_resume_packet(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/handoff/resume", body)
        response.raise_for_status()
        return self._json_dict(response)

    def capture_completion(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/completions", body)
        response.raise_for_status()
        return self._json_dict(response)

    def report_resume_feedback(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/continuity/pilot/feedback", body)
        response.raise_for_status()
        return self._json_dict(response)

    def continuity_pilot_status(self, days: int = 30) -> dict[str, Any]:
        response = self._get("/v1/continuity/pilot/status", params={"days": days})
        response.raise_for_status()
        return self._json_dict(response)

    def governance_status(self) -> dict[str, Any]:
        response = self._get("/v1/governance/status")
        response.raise_for_status()
        return self._json_dict(response)

    def context_brief(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/context/brief", body)
        response.raise_for_status()
        return self._json_dict(response)

    def annotate_event(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/events/annotate", body)
        response.raise_for_status()
        return self._json_dict(response)

    def list_episodes(self, session_id: str, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"session_id": session_id, "limit": limit}
        if status:
            params["status"] = status
        response = self._get("/v1/episodes", params=params)
        response.raise_for_status()
        return self._json_dict(response)

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        response = self._get(f"/v1/episodes/{episode_id}")
        response.raise_for_status()
        return self._json_dict(response)

    def list_memory_rules(self, include_inactive: bool = False) -> dict[str, Any]:
        response = self._get("/v1/memory/rules", params={"include_inactive": include_inactive})
        response.raise_for_status()
        return self._json_dict(response)

    def upsert_memory_rule(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/memory/rules", body)
        response.raise_for_status()
        return self._json_dict(response)

    def deprecate_memory_rule(self, rule_id: str) -> dict[str, Any]:
        response = self._post(f"/v1/memory/rules/{rule_id}/deprecate", {})
        response.raise_for_status()
        return self._json_dict(response)

    def forget_memory(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/memory/forget", body)
        response.raise_for_status()
        return self._json_dict(response)

    def retrieval_eval_status(self, session_id: str) -> dict[str, Any]:
        response = self._get("/v1/retrieval/eval/status", params={"session_id": session_id})
        response.raise_for_status()
        return self._json_dict(response)

    def retrieval_eval_run(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/retrieval/eval/run", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_patterns(self, domain: str | None, min_confidence: float) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"min_confidence": min_confidence}
        if domain:
            params["domain"] = domain
        response = self._get("/v1/patterns", params=params)
        response.raise_for_status()
        return self._json_list_dict(response)

    def record_event(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/events", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_mode(self) -> dict[str, Any]:
        response = self._get("/v1/runtime/mode")
        response.raise_for_status()
        return self._json_dict(response)

    def set_mode(self, mode: str) -> dict[str, Any]:
        response = self._put("/v1/runtime/mode", {"mode": mode})
        response.raise_for_status()
        return self._json_dict(response)

    def clone_advice(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/clone/advice", body)
        response.raise_for_status()
        return self._json_dict(response)

    def clone_arbitrate(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/clone/arbitrate", body)
        response.raise_for_status()
        return self._json_dict(response)

    def ingest_observations(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/clone/ingest-observations", body)
        response.raise_for_status()
        return self._json_dict(response)

    def record_behavior_evidence(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/evidence", body)
        response.raise_for_status()
        return self._json_dict(response)

    def predict_behavior(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/predict", body)
        response.raise_for_status()
        return self._json_dict(response)

    def run_behavior_evaluation(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/evaluate", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_evaluations(self, limit: int = 20) -> dict[str, Any]:
        response = self._get("/v1/behavior/evaluations", params={"limit": limit})
        response.raise_for_status()
        return self._json_dict(response)

    def get_current_behavior_projection(self, format_name: str = "markdown") -> dict[str, Any]:
        response = self._get(
            "/v1/behavior/projections/current",
            params={"format": format_name},
        )
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_decisions_projection(
        self,
        topic: str,
        format_name: str = "markdown",
    ) -> dict[str, Any]:
        response = self._get(
            f"/v1/behavior/projections/decisions/{quote(topic, safe='')}",
            params={"format": format_name},
        )
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_evidence_projection(
        self,
        observation_id: str,
        format_name: str = "json",
    ) -> dict[str, Any]:
        response = self._get(
            f"/v1/behavior/projections/evidence/{quote(observation_id, safe='')}",
            params={"format": format_name},
        )
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_review_projection(self) -> dict[str, Any]:
        response = self._get("/v1/behavior/projections/review")
        response.raise_for_status()
        return self._json_dict(response)

    def assign_behavior_projection_pilot(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/projections/pilot/assign", body)
        response.raise_for_status()
        return self._json_dict(response)

    def report_behavior_projection_pilot_outcome(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/projections/pilot/outcome", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_projection_pilot_status(self) -> dict[str, Any]:
        response = self._get("/v1/behavior/projections/pilot/status")
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_calibration_scenarios(self) -> dict[str, Any]:
        response = self._get("/v1/behavior/calibration/scenarios")
        response.raise_for_status()
        return self._json_dict(response)

    def answer_behavior_calibration(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/calibration/answer", body)
        response.raise_for_status()
        return self._json_dict(response)

    def create_capability_grant(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/capabilities/grants", body)
        response.raise_for_status()
        return self._json_dict(response)

    def consume_capability_grant(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/capabilities/consume", body)
        response.raise_for_status()
        return self._json_dict(response)

    def mine_behavior_processes(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/processes/mine", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_processes(self, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        response = self._get("/v1/behavior/processes", params=params)
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_shadow_status(self, limit: int = 200) -> dict[str, Any]:
        response = self._get("/v1/behavior/shadow/status", params={"limit": limit})
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_memory_reviews(self, status: str | None = "pending", limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        response = self._get("/v1/behavior/reviews", params=params)
        response.raise_for_status()
        return self._json_dict(response)

    def resolve_behavior_memory_review(self, review_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post(f"/v1/behavior/reviews/{review_id}/resolve", body)
        response.raise_for_status()
        return self._json_dict(response)

    def create_behavior_counterfactual(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/behavior/counterfactuals", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_behavior_counterfactuals(self, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        response = self._get("/v1/behavior/counterfactuals", params=params)
        response.raise_for_status()
        return self._json_dict(response)

    def resolve_behavior_counterfactual(self, counterfactual_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post(f"/v1/behavior/counterfactuals/{counterfactual_id}/resolve", body)
        response.raise_for_status()
        return self._json_dict(response)

    def takeover_step(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/step", body)
        response.raise_for_status()
        return self._json_dict(response)

    def takeover_preload(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/preload", body)
        response.raise_for_status()
        return self._json_dict(response)

    def takeover_feedback(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/feedback", body)
        response.raise_for_status()
        return self._json_dict(response)

    def takeover_discover_goals(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/goals/discover", body)
        response.raise_for_status()
        return self._json_dict(response)

    def takeover_precompute_goals(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/goals/precompute", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_takeover_goals(self, session_id: str, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"session_id": session_id}
        if status:
            params["status"] = status
        response = self._get("/v1/takeover/goals", params=params)
        response.raise_for_status()
        return self._json_dict(response)

    def get_takeover_goal_cache_status(self, session_id: str) -> dict[str, Any]:
        response = self._get("/v1/takeover/goals/cache/status", params={"session_id": session_id})
        response.raise_for_status()
        return self._json_dict(response)

    def invalidate_takeover_goal_cache(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/goals/cache/invalidate", body)
        response.raise_for_status()
        return self._json_dict(response)

    def select_takeover_goal(self, goal_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post(f"/v1/takeover/goals/{goal_id}/select", body)
        response.raise_for_status()
        return self._json_dict(response)

    def request_execution_permit(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/permit", body)
        response.raise_for_status()
        return self._json_dict(response)

    def resolve_execution_permit(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/permit/resolve", body)
        response.raise_for_status()
        return self._json_dict(response)

    def takeover_autonomy_status(self, session_id: str) -> dict[str, Any]:
        response = self._get("/v1/takeover/autonomy/status", params={"session_id": session_id})
        response.raise_for_status()
        return self._json_dict(response)

    def takeover_autonomy_tick(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/autonomy/tick", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_takeover_notices(self, session_id: str) -> dict[str, Any]:
        response = self._get("/v1/takeover/notices", params={"session_id": session_id})
        response.raise_for_status()
        return self._json_dict(response)

    def ack_takeover_notice(self, notice_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post(f"/v1/takeover/notices/{notice_id}/ack", body)
        response.raise_for_status()
        return self._json_dict(response)

    def claim_execution(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/execution/claim", body)
        response.raise_for_status()
        return self._json_dict(response)

    def report_execution(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/takeover/execution/report", body)
        response.raise_for_status()
        return self._json_dict(response)

    def get_execution_status(self, session_id: str) -> dict[str, Any]:
        response = self._get("/v1/takeover/execution/status", params={"session_id": session_id})
        response.raise_for_status()
        return self._json_dict(response)

    def get_workflow_templates(self, session_id: str = "default", limit: int = 10) -> dict[str, Any]:
        response = self._get(
            "/v1/workflow/templates",
            params={"session_id": session_id, "limit": max(1, int(limit or 10))},
        )
        response.raise_for_status()
        return self._json_dict(response)

    def get_takeover_state(
        self,
        session_id: str,
        persona_mode: str = "normal",
        activation_keywords: str | None = None,
        stop_keywords: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "session_id": session_id,
            "persona_mode": persona_mode,
        }
        if activation_keywords is not None:
            params["activation_keywords"] = activation_keywords
        if stop_keywords is not None:
            params["stop_keywords"] = stop_keywords
        response = self._get("/v1/takeover/state", params=params)
        response.raise_for_status()
        return self._json_dict(response)

    def reset_takeover_state(
        self,
        session_id: str,
        persona_mode: str = "normal",
        activation_keywords: str | None = None,
        stop_keywords: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "session_id": self._normalize_session_id(session_id),
            "persona_mode": persona_mode,
        }
        if activation_keywords is not None:
            params["activation_keywords"] = activation_keywords
        if stop_keywords is not None:
            params["stop_keywords"] = stop_keywords
        response = self.session.post(
            f"{self.base_url}/v1/takeover/reset",
            params=params,
            headers=self.headers,
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        return self._json_dict(response)

    def check_context(
        self,
        file_path: str,
        intended_action: str = "edit",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"file_path": file_path, "intended_action": intended_action}
        if session_id:
            body["session_id"] = session_id
        response = self._post("/v1/clone/check-context", body)
        response.raise_for_status()
        return self._json_dict(response)

    def search_entities(self, query: str, k: int = 20) -> dict[str, Any]:
        response = self._get("/v1/graph/entities", params={"query": query, "k": k})
        response.raise_for_status()
        return self._json_dict(response)

    def event_graph(self, event_id: str) -> dict[str, Any]:
        response = self._get(f"/v1/graph/event/{event_id}")
        response.raise_for_status()
        return self._json_dict(response)

    def list_team_memberships(self) -> list[dict[str, Any]]:
        response = self._get("/v1/team/memberships")
        response.raise_for_status()
        return self._json_list_dict(response)

    def activity_summary(self, period: str = "today", domain: str | None = None, max_events: int = 400) -> dict[str, Any]:
        params: dict[str, Any] = {"period": period, "max_events": max_events}
        if domain:
            params["domain"] = domain
        response = self._get("/v1/summary/activity", params=params)
        response.raise_for_status()
        return self._json_dict(response)

    def lifecycle_status(self) -> dict[str, Any]:
        response = self._get("/v1/admin/lifecycle/status")
        response.raise_for_status()
        return self._json_dict(response)

    def lifecycle_run(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/admin/lifecycle/run", body)
        response.raise_for_status()
        return self._json_dict(response)
