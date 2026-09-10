"""Reservation arithmetic, and the enforceable-versus-estimate distinction.

The honest claim this file pins: **no runtime surface is ``"enforced"`` today.**  Labelling one
``enforced`` on the strength of an error subtype nobody has observed would make the column a table
lookup measuring nothing.  When a captured transcript containing that subtype exists, the table
flips and this test is the thing that has to be edited to allow it.
"""

from __future__ import annotations

import pytest
from tce_shared.budget import (
    BUDGET_EXCEEDED_CODE,
    BUDGET_LATENCY_CODE,
    COST_SOURCES,
    SPEND_ENFORCEMENT,
    BudgetExceeded,
    Reconciliation,
    Reservation,
    reconcile,
    reconciliation_from_json,
    reconciliation_to_json,
    reservation_from_json,
    reservation_to_json,
    reserve,
    spend_enforcement_for,
)
from tce_shared.runtime_contract import SURFACES

PRICES = {"gpt-5-codex": {"input": 125, "output": 1000, "cached_input": 13, "reasoning": 1000}}


def _reservation(**overrides: object) -> Reservation:
    values: dict[str, object] = {
        "reserved_minor_units": 200,
        "currency": "USD",
        "spend_enforcement": "unsupported",
        "cap_applied": None,
    }
    values.update(overrides)
    return Reservation(**values)  # type: ignore[arg-type]


# --- G10's pure half: honest labels ----------------------------------------------------------------


def test_spend_enforcement_is_per_surface() -> None:
    assert spend_enforcement_for("claude/stream") == "estimated"
    assert spend_enforcement_for("claude/oneshot") == "estimated"
    assert spend_enforcement_for("codex/app-server") == "unsupported"
    assert spend_enforcement_for("codex/exec") == "unsupported"


def test_no_surface_is_enforced_today() -> None:
    assert "enforced" not in {spend_enforcement_for(surface) for surface in SURFACES}
    assert set(SPEND_ENFORCEMENT) == {"enforced", "estimated", "unsupported"}


def test_an_unknown_surface_is_unsupported_not_enforced() -> None:
    assert spend_enforcement_for("gemini/whatever") == "unsupported"


def test_error_code_is_not_the_latency_one() -> None:
    """``budget_exhausted`` already means "the turn ran out of time".  Reusing it for money would
    make two different refusals indistinguishable at the call site."""
    assert BUDGET_EXCEEDED_CODE == "budget_exceeded"
    assert BUDGET_LATENCY_CODE == "budget_exhausted"
    assert BUDGET_EXCEEDED_CODE != BUDGET_LATENCY_CODE


# --- reserve -----------------------------------------------------------------------------------------


def test_reserve_returns_the_request_and_the_surface_label() -> None:
    reservation = reserve(
        cap_minor_units=1000, already_reserved_minor_units=0, request_minor_units=200, currency="USD", surface="codex/exec"
    )
    assert reservation.reserved_minor_units == 200
    assert reservation.currency == "USD"
    assert reservation.spend_enforcement == "unsupported"
    # No enforceable cap exists for this surface, so none is claimed.
    assert reservation.cap_applied is None


def test_reserve_accumulates_against_the_cap() -> None:
    reserve(cap_minor_units=500, already_reserved_minor_units=300, request_minor_units=200, currency="USD", surface="codex/exec")
    with pytest.raises(BudgetExceeded) as excinfo:
        reserve(
            cap_minor_units=500, already_reserved_minor_units=300, request_minor_units=201, currency="USD", surface="codex/exec"
        )
    assert excinfo.value.reserved_minor_units == 300
    assert excinfo.value.requested_minor_units == 201
    assert excinfo.value.cap_minor_units == 500
    assert BUDGET_EXCEEDED_CODE in str(excinfo.value)


def test_a_zero_cap_refuses_any_nonzero_request() -> None:
    reserve(cap_minor_units=0, already_reserved_minor_units=0, request_minor_units=0, currency="USD", surface="codex/exec")
    with pytest.raises(BudgetExceeded):
        reserve(cap_minor_units=0, already_reserved_minor_units=0, request_minor_units=1, currency="USD", surface="codex/exec")


def test_a_negative_request_is_a_value_error_not_a_free_refund() -> None:
    with pytest.raises(ValueError):
        reserve(
            cap_minor_units=500, already_reserved_minor_units=0, request_minor_units=-100, currency="USD", surface="codex/exec"
        )


# --- reconcile ---------------------------------------------------------------------------------------


def test_a_provider_reported_cost_wins() -> None:
    result = reconcile(
        surface="claude/stream",
        result_tokens={"input": 1000, "output": 500},
        provider_cost_minor_units=42,
        price_table=PRICES,
        model_id="gpt-5-codex",
        reservation=_reservation(),
    )
    assert result.cost_minor_units == 42
    assert result.cost_source == "provider_reported"


def test_tokens_are_priced_when_the_provider_reports_nothing() -> None:
    result = reconcile(
        surface="codex/exec",
        result_tokens={"input": 1_000_000, "output": 1_000_000, "cached_input": 0, "reasoning": 0},
        provider_cost_minor_units=None,
        price_table=PRICES,
        model_id="gpt-5-codex",
        reservation=_reservation(),
    )
    assert result.cost_source == "estimated"
    assert result.cost_minor_units == 1125
    assert result.tokens_input == 1_000_000
    assert result.tokens_output == 1_000_000


def test_an_unknown_model_is_unavailable_not_zero() -> None:
    result = reconcile(
        surface="codex/exec",
        result_tokens={"input": 10, "output": 10},
        provider_cost_minor_units=None,
        price_table=PRICES,
        model_id="a-model-nobody-priced",
        reservation=_reservation(),
    )
    assert result.cost_source == "unavailable"
    assert result.cost_minor_units == 0
    # The token counts survive even when the price does not.
    assert result.tokens_input == 10


def test_zero_usage_with_error_is_unavailable_not_zero() -> None:
    """A crashed result may carry an all-zero usage block.  A zero that means "we do not know" must
    not be recorded as a zero that means "free"."""
    result = reconcile(
        surface="claude/stream",
        result_tokens={"input": 0, "output": 0, "cached_input": 0, "reasoning": 0, "is_error": 1},
        provider_cost_minor_units=None,
        price_table=PRICES,
        model_id="gpt-5-codex",
        reservation=_reservation(),
    )
    assert result.cost_source == "unavailable"
    assert result.cost_minor_units == 0
    assert set(COST_SOURCES) == {"provider_reported", "estimated", "unavailable"}


def test_overshoot_is_recorded_not_clamped() -> None:
    result = reconcile(
        surface="claude/stream",
        result_tokens={"input": 0, "output": 0},
        provider_cost_minor_units=750,
        price_table=PRICES,
        model_id="gpt-5-codex",
        reservation=_reservation(reserved_minor_units=200),
    )
    assert result.cost_minor_units == 750
    assert result.overshoot_minor_units == 550


def test_an_undershoot_is_not_a_negative_overshoot() -> None:
    result = reconcile(
        surface="claude/stream",
        result_tokens={"input": 0, "output": 0},
        provider_cost_minor_units=50,
        price_table=PRICES,
        model_id="gpt-5-codex",
        reservation=_reservation(reserved_minor_units=200),
    )
    assert result.overshoot_minor_units == 0


# --- codecs -------------------------------------------------------------------------------------------


def test_reservation_and_reconciliation_codecs_round_trip() -> None:
    reservation = _reservation(spend_enforcement="estimated", cap_applied={"max_budget_usd": 2.0})
    assert reservation_from_json(reservation_to_json(reservation)) == reservation
    assert reservation_from_json(reservation_to_json(_reservation())) == _reservation()
    reconciliation = Reconciliation(
        cost_minor_units=42,
        cost_source="provider_reported",
        tokens_input=1,
        tokens_output=2,
        tokens_cached_input=3,
        tokens_reasoning=4,
        overshoot_minor_units=0,
    )
    assert reconciliation_from_json(reconciliation_to_json(reconciliation)) == reconciliation
