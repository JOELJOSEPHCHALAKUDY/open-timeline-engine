"""P2 exit gate G5 — the additive wire contract, pinned once, for both backends.

Every field P2 appends to an existing response model is *optional with a stated default*
(``p2_symbols.md`` §S5.4). Two things follow, and this module asserts both:

1. **Old clients keep working.** Each model still constructs from only its pre-P2 required
   fields; nothing P2 adds is required.
2. **A default cannot drift silently.** Every new field's default is asserted literally here,
   so changing one in ``shared/tce_shared/events.py`` fails a named test rather than quietly
   changing the wire.

The models are defined once, in ``shared/tce_shared/events.py``, and both backends import
*those* classes — ``test_both_backends_share_the_same_wire_classes`` asserts the identity,
which is what makes a single defaults table sufficient for Full and Lite alike.

This file is cross-cutting by construction: it constructs models whose fields land from
Builders A, B and C, so it is expected to be red until all three have landed. Everything P2
adds is therefore imported *inside* the test that needs it, never at module scope: a
module-scope import of a not-yet-existing name is a collection error, and a collection error
interrupts the whole ``pytest tests/unit`` run instead of failing this one file.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from tce_shared.events import (
    ExecutionStatusResponse,
    ResumePacketResponse,
    TakeoverClassification,
    TakeoverGoal,
    TakeoverState,
    TakeoverStepResponse,
)

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
_PROJECTION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_GOAL_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_PACKET_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
_RECORD_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


# --------------------------------------------------------------------------- builders
# Each builder passes ONLY the fields that were required before P2. If P2 ever makes one of
# its additions required, these calls raise ValidationError and the gate fails.


def _takeover_step_response() -> TakeoverStepResponse:
    return TakeoverStepResponse(
        state=TakeoverState(
            session_id="wire-defaults",
            workspace_id="personal",
            user_id="codex-executor",
            updated_at=_NOW,
        ),
        action="observe",
        classification=TakeoverClassification.EMPTY,
    )


def _execution_status_response() -> ExecutionStatusResponse:
    return ExecutionStatusResponse(session_id="wire-defaults", generated_at=_NOW)


def _takeover_goal() -> TakeoverGoal:
    return TakeoverGoal(
        id=_GOAL_ID,
        session_id="wire-defaults",
        workspace_id="personal",
        user_id="codex-executor",
        title="read-only diagnosis",
        description="inspect and report",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _resume_packet_response() -> ResumePacketResponse:
    return ResumePacketResponse(
        packet_id=_PACKET_ID,
        selected_record_id=_RECORD_ID,
        task_summary="resume the objective",
        decision="continue",
        next_step="execute step 1",
        status="ready",
    )


def _task_state_projection_response() -> Any:
    from tce_shared.events import TaskStateProjectionResponse

    return TaskStateProjectionResponse(
        projection_id=_PROJECTION_ID,
        uri="tce://task-state/wire-defaults",
        generated_at=_NOW,
    )


# --------------------------------------------------------------------------- the tables
# NORMATIVE: p2_symbols.md §S5.4 (additive fields) and §1.10 (TaskStateProjectionResponse,
# TaskStateSummary). Every entry is field name -> the default the wire must carry.

_STEP_DEFAULTS: dict[str, Any] = {
    "planning_pending": False,
    "planning_job_id": None,
    "planning_pending_hint_ms": 0,
    "task_state_revision": 0,
}

_GOAL_DEFAULTS: dict[str, Any] = {
    "step_index": None,
    "parent_goal_id": None,
    "depends_on": [],
    "attempts": 0,
    "mutating": False,
}

_RESUME_PACKET_DEFAULTS: dict[str, Any] = {
    "task_id": None,
    "task_state_revision": 0,
    "contract_revision": 0,
    "verification_refs": [],
    "unresolved_effects": [],
    "source_event_id": None,
}

_PROJECTION_DEFAULTS: dict[str, Any] = {
    "task_id": "",
    "view": "state",
    "format": "markdown",
    "mime_type": "text/markdown; charset=utf-8",
    "schema_version": "v1",
    "source_revision": "",
    "content_sha256": "",
    "revision": 0,
    "contract_revision": 0,
    "source_evidence_ids": [],
    "trust_level": "projection",
    "sensitivity": 1,
    "read_only": True,
    "projection_learning_eligible": False,
    "expires_at": None,
    "evidence_count": 0,
    "truncated": False,
    "redaction_applied": False,
    "content": "",
}


def _execution_status_defaults() -> dict[str, Any]:
    from tce_shared.events import TaskNextPermittedAction

    return {
        "task_state_revision": 0,
        "next_permitted_action": TaskNextPermittedAction.NONE,
    }


def _task_state_summary_defaults() -> dict[str, Any]:
    from tce_shared.events import TaskLifecycleStatus, TaskNextPermittedAction

    return {
        "task_id": "",
        "revision": 0,
        "contract_revision": 0,
        "status": TaskLifecycleStatus.AWAITING_OBJECTIVE,
        "next_permitted_action": TaskNextPermittedAction.AWAIT_OWNER_OBJECTIVE,
        "plan_state": "absent",
        "plan_producer": None,
        "open_step_index": None,
        "open_decision_count": 0,
        "unresolved_effect_count": 0,
        "source_revision": "",
    }


# --------------------------------------------------------------------------- helpers


def _assert_defaults(model: Any, expected: dict[str, Any]) -> None:
    fields = type(model).model_fields
    missing = [name for name in expected if name not in fields]
    assert not missing, f"{type(model).__name__} is missing P2 wire fields: {missing}"
    required = [name for name in expected if fields[name].is_required()]
    assert not required, f"{type(model).__name__} made P2 wire fields required: {required}"
    for name, value in expected.items():
        assert getattr(model, name) == value, (
            f"{type(model).__name__}.{name} default is {getattr(model, name)!r}, "
            f"expected {value!r}"
        )


def _assert_round_trip(model: Any, expected: dict[str, Any]) -> None:
    """A default must survive JSON serialization and re-validation unchanged."""
    restored = type(model).model_validate(model.model_dump(mode="json"))
    for name, value in expected.items():
        assert getattr(restored, name) == value, (
            f"{type(model).__name__}.{name} did not round-trip: "
            f"{getattr(restored, name)!r} != {value!r}"
        )


# --------------------------------------------------------------------------- tests


def test_takeover_step_response_constructs_from_pre_p2_required_fields() -> None:
    from tce_shared.events import TaskStateSummary

    response = _takeover_step_response()
    _assert_defaults(response, _STEP_DEFAULTS)
    _assert_round_trip(response, _STEP_DEFAULTS)
    # task_state is a nested model, so it is asserted against its own default table.
    assert isinstance(response.task_state, TaskStateSummary)
    _assert_defaults(response.task_state, _task_state_summary_defaults())


def test_task_state_summary_defaults_are_the_declared_ones() -> None:
    from tce_shared.events import TaskStateSummary

    expected = _task_state_summary_defaults()
    summary = TaskStateSummary()
    _assert_defaults(summary, expected)
    _assert_round_trip(summary, expected)


def test_execution_status_response_constructs_from_pre_p2_required_fields() -> None:
    expected = _execution_status_defaults()
    response = _execution_status_response()
    _assert_defaults(response, expected)
    _assert_round_trip(response, expected)


def test_takeover_goal_constructs_from_pre_p2_required_fields() -> None:
    goal = _takeover_goal()
    _assert_defaults(goal, _GOAL_DEFAULTS)
    _assert_round_trip(goal, _GOAL_DEFAULTS)


def test_resume_packet_response_constructs_from_pre_p2_required_fields() -> None:
    packet = _resume_packet_response()
    _assert_defaults(packet, _RESUME_PACKET_DEFAULTS)
    _assert_round_trip(packet, _RESUME_PACKET_DEFAULTS)


def test_task_state_projection_response_defaults() -> None:
    projection = _task_state_projection_response()
    _assert_defaults(projection, _PROJECTION_DEFAULTS)
    _assert_round_trip(projection, _PROJECTION_DEFAULTS)


@pytest.mark.parametrize(
    ("build", "field_name"),
    [
        (_takeover_goal, "depends_on"),
        (_resume_packet_response, "verification_refs"),
        (_resume_packet_response, "unresolved_effects"),
        (_task_state_projection_response, "source_evidence_ids"),
    ],
)
def test_collection_defaults_are_not_shared_between_instances(
    build: Any, field_name: str
) -> None:
    """A bare ``= []`` default would let one response mutate every other one."""
    first = build()
    second = build()
    getattr(first, field_name).append("mutated")
    assert getattr(second, field_name) == []


def test_task_state_summary_nested_default_is_not_shared() -> None:
    first = _takeover_step_response()
    second = _takeover_step_response()
    assert first.task_state is not second.task_state


def test_both_backends_share_the_same_wire_classes() -> None:
    """Full and Lite must reach the identical model objects.

    If either backend ever shadows one of these with a local definition, a default could
    diverge between the two OpenAPI documents while every table above still passed.
    """
    import tce_api.main as full_main
    import tce_lite_api.main as lite_main
    from tce_shared.events import TaskStateProjectionResponse, TaskStateSummary

    backends = (full_main, lite_main)
    # Both backends import these four by name today, so absence is itself a regression.
    for model in (
        TakeoverStepResponse,
        ExecutionStatusResponse,
        TakeoverGoal,
        ResumePacketResponse,
    ):
        name = model.__name__
        for backend in backends:
            resolved = getattr(backend, name, None)
            assert resolved is not None, f"{backend.__name__} does not expose {name}"
            assert resolved is model, (
                f"{backend.__name__}.{name} is not the shared tce_shared.events class"
            )
    # P2's two new models: a backend may legitimately reference these through the module
    # (``events.TaskStateSummary``) rather than importing the name, so absence is not a
    # failure — a *different* class under the same name is.
    for p2_model in (TaskStateProjectionResponse, TaskStateSummary):
        name = p2_model.__name__
        for backend in backends:
            resolved = getattr(backend, name, None)
            assert resolved is None or resolved is p2_model, (
                f"{backend.__name__}.{name} shadows the shared tce_shared.events class"
            )
