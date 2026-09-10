"""The supervisor's narrow HTTP client.

This extends the credential-separation pattern already established in
``services/tce_mcp/tce_mcp/client.py`` rather than inventing a second one: refuse to run when the
credential we hold is one we must not hold, scrub the credentials we must never pass on out of
``os.environ``, and refuse the endpoint prefixes this identity has no business calling.

Three differences from the MCP client, each load-bearing:

* **The retry policy retries GET only.** ``client.py`` installs
  ``allowed_methods=frozenset({"GET","POST","PUT"})``.  Copying that here would make a retried
  ``start`` a *duplicate dispatch* — a second agent process against the same directive — which is
  exactly the failure the effect journal exists to make impossible.  Every mutating call carries an
  idempotency key instead.
* **Manager-only prefixes are refused too**, not just host-only ones.  The supervisor is an
  executor identity; the dashboard, system and runtime-mode routes are the manager's.
* **The collision check is two-sided**: the supervisor token must differ from both the manager's
  ``TCE_API_TOKENS`` and the host-capture ``TCE_HOST_CAPTURE_TOKENS``.

What this client does NOT do, stated so nobody reads more into it: it does not authenticate the
server, it does not pin a certificate, and refusing a path prefix is a client-side courtesy — the
server's own auth is the control.  These refusals catch a misconfiguration, not an attacker who
holds the token.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, cast

import requests
from requests.adapters import HTTPAdapter
from tce_shared.budget import Reconciliation
from tce_shared.charter import ResolvedCharter, resolved_charter_from_json
from tce_shared.verification import (
    AcceptanceCheck,
    AcceptanceCriteria,
    VerificationEvidence,
    acceptance_check_to_json,
    criteria_from_json,
    evidence_to_json,
)
from urllib3.util.retry import Retry

from .config import SupervisorSettings

# The trusted human-input capture channel is a different capability with a different credential.
_HOST_ONLY_PATH_PREFIXES: tuple[str, ...] = ("/v1/inputs",)
# The manager's own surfaces. An executor identity has no reason to reach them.
_MANAGER_ONLY_PATH_PREFIXES: tuple[str, ...] = ("/v1/dashboard/", "/v1/system/", "/v1/runtime/mode")
# Scrubbed from os.environ at construction so a spawned child cannot inherit them, whatever else
# goes wrong. taskdir.child_environment builds a fresh dict anyway; this is the second lock.
_FORBIDDEN_ENV_KEYS: tuple[str, ...] = ("TCE_HOST_CAPTURE_TOKENS", "TCE_HOST_CAPTURE_TOKEN", "TCE_API_TOKENS")

_REQUEST_TIMEOUT_SECONDS: float = 60.0


class CredentialCollision(RuntimeError):
    """The supervisor was configured with a credential that belongs to another identity."""

    key: str

    def __init__(self, key: str) -> None:
        super().__init__(
            f"TCE_SUP_API_TOKEN equals an entry of {key}; the supervisor must hold its own "
            f"credential, distinct from the manager's and from the host capture channel's "
            f"(see docs/supervisor.md)"
        )
        self.key = key


class ApiRefusal(RuntimeError):
    """A non-2xx answer, carried with the status and the parsed detail rather than a bare string."""

    status_code: int
    error: str
    detail: Mapping[str, Any]

    def __init__(self, status_code: int, error: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{status_code} {error}: {detail}")
        self.status_code = status_code
        self.error = error
        self.detail = dict(detail)


def _env_token_set(key: str) -> frozenset[str]:
    return frozenset(item.strip() for item in (os.environ.get(key) or "").split(",") if item.strip())


def assert_supervisor_credential_is_distinct(api_token: str) -> None:
    """Refuse a supervisor token that is the manager's or the host capture channel's.

    The forbidden keys are dropped from ``os.environ`` **whether or not** a collision is found, so
    the check and the scrub are one operation and a caller cannot get the scrub without the check.
    """
    collisions: list[str] = []
    for key in _FORBIDDEN_ENV_KEYS:
        if api_token and api_token in _env_token_set(key):
            collisions.append(key)
    for key in _FORBIDDEN_ENV_KEYS:
        os.environ.pop(key, None)
    if collisions:
        raise CredentialCollision(collisions[0])


def is_forbidden_path(path: str) -> bool:
    """True for a path this identity must never call, host-only or manager-only."""
    text = str(path)
    return text.startswith(_HOST_ONLY_PATH_PREFIXES) or text.startswith(_MANAGER_ONLY_PATH_PREFIXES)


def _detail_of(response: requests.Response) -> tuple[str, dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError:
        return ("http_error", {"body": response.text[:2000]})
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            typed = cast(dict[str, Any], detail)
            return (str(typed.get("error") or "http_error"), typed)
        return ("http_error", {"detail": detail})
    return ("http_error", {"detail": payload})


class ApiClient:
    """The only way the supervisor talks to the manager."""

    def __init__(self, settings: SupervisorSettings) -> None:
        assert_supervisor_credential_is_distinct(str(settings.api_token or ""))
        self.settings = settings
        self.base_url = settings.api_base_url.rstrip("/")
        self.session_id = str(settings.session_id or "")
        self.headers: dict[str, str] = {
            "Authorization": f"Bearer {settings.api_token}",
            "Content-Type": "application/json",
            "X-TCE-Consumer": settings.consumer_id,
            "X-TCE-Role": settings.role,
            "X-TCE-Workspace": settings.workspace_id,
            "X-TCE-User": settings.user_id,
            "X-TCE-Behavior-Subject": settings.behavior_subject_id,
        }
        # GET only. See the module docstring: a retried POST is a duplicate dispatch.
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session = requests.Session()
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # -- transport ---------------------------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: Mapping[str, Any] | None = None, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> requests.Response:
        if is_forbidden_path(path):
            raise PermissionError(f"the supervisor client refuses {path}: it belongs to another identity")
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        return self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=merged,
            json=dict(json_body) if json_body is not None else None,
            params=dict(params) if params is not None else None,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    def _json(self, response: requests.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            error, detail = _detail_of(response)
            raise ApiRefusal(response.status_code, error, detail)
        try:
            payload = response.json()
        except ValueError:
            return {}
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
        return {"data": payload}

    # -- charter -----------------------------------------------------------------------------

    def get_active_charter(self) -> ResolvedCharter | None:
        """The active charter for this session, or None.

        ``{"charter": null}`` and a body with no ``charter_id`` both mean *no active charter*, and
        the caller must refuse rather than proceed. A 404 means the same thing.
        """
        try:
            payload = self._json(self._request("GET", "/v1/charters/active", params={"session_id": self.session_id}))
        except ApiRefusal as refusal:
            if refusal.status_code == 404:
                return None
            raise
        body = payload.get("charter", payload)
        if not isinstance(body, Mapping) or not body.get("charter_id"):
            return None
        return resolved_charter_from_json(body)

    # -- sandbox -----------------------------------------------------------------------------

    def post_self_test(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._json(self._request("POST", "/v1/sandbox/self-test", json_body=payload))

    # -- verification ------------------------------------------------------------------------

    def freeze_acceptance_criteria(self, *, directive_id: str, checks: Sequence[AcceptanceCheck], corpus_manifest: Sequence[tuple[str, str]], charter_id: str | None) -> AcceptanceCriteria:
        body: dict[str, Any] = {
            "session_id": self.session_id,
            "directive_id": directive_id,
            "checks": [acceptance_check_to_json(check) for check in checks],
            "corpus_manifest": [[path, digest] for path, digest in corpus_manifest],
            "charter_id": charter_id,
        }
        payload = self._json(self._request("POST", "/v1/verification/criteria", json_body=body))
        return criteria_from_json(payload.get("criteria", payload))

    def record_verification(self, evidence: VerificationEvidence) -> dict[str, Any]:
        """POST the evidence. The body carries no verdict and no runner principal — by design.

        The verifier presents its OWN credential, and that credential must be SERVER-BOUND: since
        F1 the route derives the runner principal only from a bound identity claim or the host
        capture capability, never from ``X-TCE-Consumer``. An unbound token therefore gets
        ``403 unknown_runner`` rather than an ``inconclusive`` verdict — ``supervise`` catches that
        one refusal and leaves ``verification_state`` at ``unverified``, which P2's R8 keeps out of
        DONE. Provisioning is described in ``docs/supervisor.md`` §2.2.
        """
        headers: dict[str, str] = {}
        if self.settings.verifier_token:
            headers["Authorization"] = f"Bearer {self.settings.verifier_token}"
        if self.settings.effective_verifier_consumer_id:
            headers["X-TCE-Consumer"] = self.settings.effective_verifier_consumer_id
        headers["X-TCE-Role"] = "user"
        body = dict(evidence_to_json(evidence))
        body["session_id"] = self.session_id
        return self._json(self._request("POST", "/v1/verification/results", json_body=body, headers=headers))

    # -- dispatch ----------------------------------------------------------------------------

    def open_dispatch(self, request: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        body = dict(request)
        body.setdefault("session_id", self.session_id)
        return self._json(self._request("POST", "/v1/dispatch", json_body=body, headers={"Idempotency-Key": idempotency_key}))

    def bind_provider_run(self, dispatch_id: str, provider_run_id: str, provider_turn_id: str | None) -> None:
        self._json(
            self._request(
                "POST",
                f"/v1/dispatch/{dispatch_id}/provider",
                json_body={"session_id": self.session_id, "provider_run_id": provider_run_id, "provider_turn_id": provider_turn_id},
            )
        )

    def reconcile_dispatch(self, dispatch_id: str, *, reconciliation: Reconciliation, outcome: str, terminal_reason: str, wall_ms: int, human_intervention_count: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": self.session_id,
            "cost_minor_units": reconciliation.cost_minor_units,
            "cost_source": reconciliation.cost_source,
            "tokens_input": reconciliation.tokens_input,
            "tokens_output": reconciliation.tokens_output,
            "tokens_cached_input": reconciliation.tokens_cached_input,
            "tokens_reasoning": reconciliation.tokens_reasoning,
            "overshoot_minor_units": reconciliation.overshoot_minor_units,
            "outcome": outcome,
            "terminal_reason": terminal_reason,
            "wall_ms": wall_ms,
            "human_intervention_count": human_intervention_count,
        }
        return self._json(self._request("POST", f"/v1/dispatch/{dispatch_id}/reconcile", json_body=body))

    # -- effects -----------------------------------------------------------------------------

    def open_effect(self, request: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(request)
        body.setdefault("session_id", self.session_id)
        return self._json(self._request("POST", "/v1/effects", json_body=body))

    def resolve_effect(self, effect_id: str, *, target_state: str, resolution_source: str, expected_lease: int, evidence: Mapping[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": self.session_id,
            "target_state": target_state,
            "resolution_source": resolution_source,
            "expected_lease": expected_lease,
            "evidence": dict(evidence),
        }
        return self._json(self._request("POST", f"/v1/effects/{effect_id}/resolve", json_body=body))

    def list_open_effects(self, session_id: str) -> list[dict[str, Any]]:
        payload = self._json(self._request("GET", "/v1/effects/open", params={"session_id": session_id or self.session_id}))
        raw = payload.get("effects", payload.get("data", []))
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    # -- the existing directive transport ------------------------------------------------------

    def claim(self, directive_id: str, *, lease_generation: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {"session_id": self.session_id, "directive_id": directive_id}
        if lease_generation is not None:
            body["lease_generation"] = lease_generation
        return self._json(self._request("POST", "/v1/takeover/execution/claim", json_body=body))

    def report(self, directive_id: str, *, state: str, lease_generation: int, idempotency_key: str, milestone: Mapping[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": self.session_id,
            "directive_id": directive_id,
            "state": state,
            "lease_generation": lease_generation,
            "idempotency_key": idempotency_key,
            "milestone": dict(milestone),
        }
        return self._json(self._request("POST", "/v1/takeover/execution/report", json_body=body))

    def cancel(self, directive_id: str, *, reason: str) -> dict[str, Any]:
        return self._json(
            self._request(
                "POST",
                "/v1/takeover/execution/cancel",
                json_body={"session_id": self.session_id, "directive_id": directive_id, "reason": reason},
            )
        )

    def get_execution_status(self, session_id: str) -> dict[str, Any]:
        return self._json(self._request("GET", "/v1/takeover/execution/status", params={"session_id": session_id or self.session_id}))
