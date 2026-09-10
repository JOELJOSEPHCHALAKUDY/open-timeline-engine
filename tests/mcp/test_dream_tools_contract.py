"""P5 §4.2 / §7.5 — the MCP contract for aspiration proposals.

Three things are asserted here and they are different in kind.

The first is a **wire contract**: ``_slim_takeover_result`` forwards the integer
``dream_proposals_pending`` and nothing else from P5.  In particular P5 contributes no
constraint rule and no proposal text, so the constraint list on an active turn is
byte-identical to what it was before this file existed (``p45_shared.md`` §3.2 clause 5).

The second is a **behavioural contract**: listing proposals records that they were shown.
That record is the only thing standing between *nonresponse* and *rejection* — without it
the passage of time silently reads as "no", which is the defect P5 exists to remove.  So
``test_list_marks_surfaced`` asserts the second call happens and names the ids from the
first, and asserts it does **not** name anything else.

The third is a **refusal**: the tool does not offer accept / reject / snooze / unsnooze.
This is not politeness.  ``tce_mcp/config.py`` pins ``mcp_role = "executor"`` and
``client.assert_executor_credential_is_not_host_capture`` raises if this process is ever
handed the host-capture credential, so a verdict posted from here would be 403'd every
time.  The test asserts the four verbs are absent from the tool's parameter schema and
that passing one returns a refusal that names the client which can do it.

This file owns its own fixtures: it must not edit or depend on the fixtures in the
pre-existing MCP contract test files.
"""

from __future__ import annotations

from typing import Any

import pytest
from tce_mcp import server, tools
from tce_mcp.config import Settings
from tce_mcp.tools import (
    DREAM_TOOL_ACTIONS,
    DREAM_VERDICT_ACTION_NAMES,
    HARD_CONSTRAINTS,
    _slim_takeover_result,
)

# --- fixtures ------------------------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    """A minimal active-takeover takeover_step result with no directive and no lock."""
    payload: dict[str, Any] = {
        "state": {
            "session_id": "s-dreams",
            "active": True,
            "mode": "takeover",
            "persona_mode": "normal",
            "takeover_context": {"objective": "rework the retry ladder", "turn_count": 2},
        },
        "action": "advisor_takeover",
        "classification": "decisive",
        "enforced": True,
        "final_response": None,
        "safety_decision": "allow",
        "needs_human": False,
        "execution_permit_required": False,
        "execution_permit_id": None,
        "directive_state": None,
        "pending_execution": None,
    }
    payload.update(overrides)
    return payload


def _proposal(proposal_id: str, title: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": proposal_id,
        "workspace_id": "personal",
        "project_id": "proj_0123456789abcdef01234567",
        "scope_kind": "project",
        "status": "proposed",
        "nonresponse": "never_surfaced",
        "surfaced_count": 0,
        "surfaced_attested": False,
        "title": title,
        "connection": "you have come back to this three times",
        "benefit": "you stop re-deciding it every week",
        "first_step": "write down the retry ladder you actually want",
        "citations": [
            {
                "event_id": "11111111-1111-4111-8111-111111111111",
                "receipt_id": "22222222-2222-4222-8222-222222222222",
                "origin_kind": "human_input",
                "observed_at": "2026-09-01T10:00:00Z",
                "quote": "the retry ladder is wrong and i keep patching it",
            }
        ],
        "citation_count": 1,
        "citations_verified": "cheap",
        "evidence_basis": "trusted_current",
        "attribution": "you said",
        "revision": 1,
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-01T10:00:00Z",
    }
    body.update(overrides)
    return body


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status on {self.status_code}")


class _FakeClient:
    """Records every call so the ORDER of list-then-surface is assertable, not assumed."""

    def __init__(self, list_body: Any, *, post_status: int = 201) -> None:
        self.list_body = list_body
        self.post_status = post_status
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def _get(self, path: str, **kwargs: Any) -> _Response:
        self.gets.append((path, dict(kwargs.get("params") or {})))
        return _Response(self.list_body)

    def _post(self, path: str, body: dict[str, Any]) -> _Response:
        self.posts.append((path, dict(body)))
        return _Response({"ok": True}, status_code=self.post_status)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient({"proposals": [], "total": 0, "citations_verified": "cheap"})
    monkeypatch.setattr(tools, "client", fake)
    return fake


# --- the slim-result key -------------------------------------------------------------


def test_dream_proposals_pending_is_forwarded() -> None:
    slim = _slim_takeover_result(_payload(dream_proposals_pending=3))

    assert slim["dream_proposals_pending"] == 3
    assert isinstance(slim["dream_proposals_pending"], int)


def test_dream_proposals_pending_defaults_to_zero() -> None:
    for payload in (_payload(), _payload(dream_proposals_pending=None), _payload(dream_proposals_pending="")):
        assert _slim_takeover_result(payload)["dream_proposals_pending"] == 0


def test_p5_adds_no_constraint_rule() -> None:
    """p45_shared.md §3.2 clause 5: P5's projection contribution is one scalar and no rule."""
    before = [dict(rule) for rule in HARD_CONSTRAINTS]

    with_dreams = _slim_takeover_result(_payload(dream_proposals_pending=7))
    without = _slim_takeover_result(_payload())

    assert [rule["rule_id"] for rule in with_dreams["constraints"]] == [
        rule["rule_id"] for rule in without["constraints"]
    ]
    assert not any("dream" in str(rule["rule_id"]) for rule in with_dreams["constraints"])
    assert not any("dream" in str(rule.get("reason", "")).lower() for rule in with_dreams["constraints"])
    # And the process-global list is untouched by the turn.
    assert [dict(rule) for rule in HARD_CONSTRAINTS] == before


def test_no_proposal_text_crosses_the_slim_boundary() -> None:
    """The count crosses; the owner's words do not.  They are read with tce.dreams."""
    slim = _slim_takeover_result(
        _payload(
            dream_proposals_pending=2,
            dream_proposals=[_proposal("a", "the retry ladder")],
        )
    )

    assert "dream_proposals" not in slim
    assert slim["dream_proposals_pending"] == 2


# --- listing records the display -----------------------------------------------------


def test_list_marks_surfaced(fake_client: _FakeClient) -> None:
    fake_client.list_body = {
        "proposals": [
            _proposal("aaaaaaaa-0000-4000-8000-000000000001", "the retry ladder"),
            _proposal("aaaaaaaa-0000-4000-8000-000000000002", "the scheduler rewrite"),
        ],
        "total": 2,
        "citations_verified": "cheap",
    }

    result = tools.dreams(session_id="s-dreams", action="list")

    listed = [item["id"] for item in result["proposals"]]
    assert listed == [
        "aaaaaaaa-0000-4000-8000-000000000001",
        "aaaaaaaa-0000-4000-8000-000000000002",
    ]
    # The second call happened, names the ids from the first, and names nothing else.
    assert [path for path, _ in fake_client.posts] == [
        f"/v1/dreams/{proposal_id}/transition" for proposal_id in listed
    ]
    assert all(body["action"] == "surfaced" for _, body in fake_client.posts)
    assert all(body["session_id"] == "s-dreams" for _, body in fake_client.posts)
    assert result["surfaced_recorded"] == listed


def test_an_empty_list_marks_nothing(fake_client: _FakeClient) -> None:
    result = tools.dreams(action="list")

    assert result["proposals"] == []
    assert result["proposal_count"] == 0
    assert result["surfaced_recorded"] == []
    assert fake_client.posts == []


def test_a_refused_display_is_reported_not_raised(fake_client: _FakeClient) -> None:
    """Re-surfacing is rate-limited server-side; a refusal must not fail the read."""
    fake_client.post_status = 409
    fake_client.list_body = {"proposals": [_proposal("aaaaaaaa-0000-4000-8000-000000000003", "the ladder")]}

    result = tools.dreams(action="list")

    assert len(result["proposals"]) == 1
    assert result["surfaced_recorded"] == []
    assert len(fake_client.posts) == 1


def test_surfaced_action_records_one_proposal(fake_client: _FakeClient) -> None:
    result = tools.dreams(session_id="s-dreams", action="surfaced", proposal_id="aaaaaaaa-0000-4000-8000-000000000004")

    assert result["kind"] == "dream_surfaced"
    assert result["surfaced_recorded"] == ["aaaaaaaa-0000-4000-8000-000000000004"]
    assert fake_client.posts == [
        (
            "/v1/dreams/aaaaaaaa-0000-4000-8000-000000000004/transition",
            {"session_id": "s-dreams", "action": "surfaced"},
        )
    ]


def test_surfaced_without_a_proposal_id_posts_nothing(fake_client: _FakeClient) -> None:
    result = tools.dreams(action="surfaced", proposal_id="")

    assert result["surfaced_recorded"] == []
    assert fake_client.posts == []


def test_attribution_and_nonresponse_reach_the_reader(fake_client: _FakeClient) -> None:
    """T5/G19: imported history must never render as something said today."""
    fake_client.list_body = {
        "proposals": [
            _proposal(
                "aaaaaaaa-0000-4000-8000-000000000005",
                "the scheduler rewrite",
                evidence_basis="backfill_only",
                attribution="from your imported history",
                nonresponse="awaiting_response",
                surfaced_count=2,
            )
        ]
    }

    item = tools.dreams(action="list")["proposals"][0]

    assert item["attribution"] == "from your imported history"
    assert item["evidence_basis"] == "backfill_only"
    assert item["nonresponse"] == "awaiting_response"
    assert item["surfaced_count"] == 2


def test_quotes_are_forwarded_verbatim(fake_client: _FakeClient) -> None:
    """A quote is evidence.  Trimming it here would make the shown text unverified text."""
    quote = "the retry ladder is wrong and i keep patching it"
    fake_client.list_body = {"proposals": [_proposal("aaaaaaaa-0000-4000-8000-000000000006", "the ladder")]}

    citations = tools.dreams(action="list")["proposals"][0]["citations"]

    assert [item["quote"] for item in citations] == [quote]
    assert [item["origin_kind"] for item in citations] == ["human_input"]


def test_cited_event_ids_are_capped_at_twelve(fake_client: _FakeClient) -> None:
    proposals = []
    for index in range(20):
        proposal = _proposal(f"aaaaaaaa-0000-4000-8000-{index:012d}", f"theme {index}")
        proposal["citations"][0]["event_id"] = f"1111111{index:04d}-1111-4111-8111-111111111111"
        proposals.append(proposal)
    fake_client.list_body = {"proposals": proposals}

    result = tools.dreams(action="list")

    assert len(result["citations"]) == 12
    assert len(result["proposals"]) == 20


# --- the refusal ---------------------------------------------------------------------


def test_tool_refuses_verdict_actions(fake_client: _FakeClient) -> None:
    for action in sorted(DREAM_VERDICT_ACTION_NAMES):
        result = tools.dreams(action=action, proposal_id="aaaaaaaa-0000-4000-8000-000000000007")

        assert result["refused"] is True, action
        assert result["reason"] == "verdict_requires_verified_human", action
        assert "tce dreams accept" in result["next_step"], action
        assert "verified human" in result["next_step"], action

    # Nothing was posted for any of them.
    assert fake_client.posts == []
    assert fake_client.gets == []


def test_verdict_verbs_are_absent_from_the_tool_schema() -> None:
    """The tool does not OFFER accept/reject/snooze — it cannot authenticate for them."""
    tools_by_name = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    assert "tce.dreams" in tools_by_name

    parameters = tools_by_name["tce.dreams"].parameters or {}
    properties = (parameters.get("properties") or {}).keys()
    assert set(properties) == {"session_id", "action", "proposal_id"}

    action_schema = (parameters.get("properties") or {}).get("action") or {}
    enum = {str(item) for item in (action_schema.get("enum") or [])}
    assert enum & DREAM_VERDICT_ACTION_NAMES == set()

    description = str(tools_by_name["tce.dreams"].description or "")
    assert "CANNOT accept, reject, snooze or unsnooze" in description
    assert "tce dreams accept" in description
    assert "action='list'" in description
    assert "surfaced" in description


def test_unknown_actions_are_refused_not_guessed(fake_client: _FakeClient) -> None:
    result = tools.dreams(action="refresh")

    assert result["refused"] is True
    assert result["reason"] == "unknown_action"
    assert fake_client.posts == []
    assert fake_client.gets == []


def test_declared_actions_are_exactly_list_and_surfaced() -> None:
    assert DREAM_TOOL_ACTIONS == ("list", "surfaced")
    assert DREAM_TOOL_ACTIONS[0] not in DREAM_VERDICT_ACTION_NAMES


# --- the surface does not grow sideways ----------------------------------------------


def test_dreams_tool_exposes_no_credential_shaped_parameter() -> None:
    tools_by_name = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    properties = (tools_by_name["tce.dreams"].parameters or {}).get("properties") or {}
    for parameter in properties:
        lowered = str(parameter).lower()
        assert "token" not in lowered and "secret" not in lowered and "key" not in lowered


def test_p5_adds_exactly_one_mcp_tool() -> None:
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert {name for name in names if "dream" in name} == {"tce.dreams"}


def test_takeover_step_description_mentions_dream_proposals_pending() -> None:
    """The slim result is a whitelist; a key nobody is told about is a key nobody reads."""
    tools_by_name = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    description = str(tools_by_name["tce.takeover_step"].description)
    assert "dream_proposals_pending" in description
    assert "tce.dreams" in description


def test_settings_gain_no_dream_field() -> None:
    """The dream settings are the API's and the worker's; the MCP process holds none."""
    assert not any("dream" in name for name in Settings.model_fields)


def test_the_dream_tool_survives_a_narrowed_tool_profile() -> None:
    """A tool in no profile is invisible under every profile but the shipped default.

    ``mcp_tool_profile`` defaults to ``"all"``, which exposes everything, so a tool that belongs
    to no profile works out of the box and then silently disappears the moment an operator
    narrows the profile.  ``tce.dreams`` reads open proposals and records that they were shown,
    which is part of the takeover surface, so it belongs to ``autonomy`` and to every profile
    that contains it.
    """
    from tce_mcp.tool_profiles import exposed_tool_names

    available = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "tce.dreams" in available
    for profile in ("autonomy", "research", "all", "admin"):
        assert "tce.dreams" in exposed_tool_names(profile, available), (
            f"tce.dreams is removed at process start under the {profile!r} profile"
        )
