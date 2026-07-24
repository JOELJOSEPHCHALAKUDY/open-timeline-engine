from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client import multiprocess as prometheus_multiprocess
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse, Response
from tce_shared.dashboard import timeline_dashboard_html
from tce_shared.events import AgentRole, RuntimeModeConfig

from ..auth import AuthContext
from ..schemas import HealthResponse, RuntimeModeSetRequest


def build_system_router(
    *,
    request_counter: Any,
    get_auth_context_dep: Callable[..., Any],
    get_db_dep: Callable[..., Any],
    get_runtime_mode_fn: Callable[[Session], RuntimeModeConfig],
    set_runtime_mode_fn: Callable[[Session, Any, str], RuntimeModeConfig],
    write_audit_log_fn: Callable[..., None],
    enforce_workspace_access_fn: Callable[[AuthContext, Session], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        request_counter.labels(endpoint="health", method="GET").inc()
        return HealthResponse(status="ok")

    @router.get("/v1/metrics")
    def metrics() -> Response:
        # Under multi-worker uvicorn each process has its own registry; the
        # scrape must aggregate via the shared mmap dir or counters appear
        # to jump/reset depending on which worker answers.
        multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        if multiproc_dir:
            registry = CollectorRegistry()
            prometheus_multiprocess.MultiProcessCollector(registry)
            payload = generate_latest(registry)
        else:
            payload = generate_latest()
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    @router.get("/", include_in_schema=False)
    def dashboard_root() -> Response:
        return RedirectResponse(url="/dashboard", status_code=302)

    @router.get("/ui", include_in_schema=False)
    def dashboard() -> Response:
        return HTMLResponse(content=timeline_dashboard_html(api_base=""))

    @router.get("/v1/runtime/mode", response_model=RuntimeModeConfig)
    def runtime_mode(
        auth: AuthContext = Depends(get_auth_context_dep),
        db: Session = Depends(get_db_dep),
    ) -> RuntimeModeConfig:
        request_counter.labels(endpoint="runtime_mode_get", method="GET").inc()
        _ = auth
        return get_runtime_mode_fn(db)

    @router.put("/v1/runtime/mode", response_model=RuntimeModeConfig)
    def set_mode(
        body: RuntimeModeSetRequest,
        auth: AuthContext = Depends(get_auth_context_dep),
        db: Session = Depends(get_db_dep),
    ) -> RuntimeModeConfig:
        request_counter.labels(endpoint="runtime_mode_set", method="PUT").inc()
        if auth.role == AgentRole.ADVISOR:
            raise HTTPException(status_code=403, detail="advisor cannot change runtime mode")
        enforce_workspace_access_fn(auth, db)
        mode = set_runtime_mode_fn(db, body.mode, auth.consumer)
        write_audit_log_fn(
            db,
            consumer=auth.consumer,
            action="set_runtime_mode",
            query={"mode": body.mode.value},
            result_event_ids=[],
            policy_decisions={"role": auth.role.value},
            latency_ms=0,
        )
        return mode

    return router
