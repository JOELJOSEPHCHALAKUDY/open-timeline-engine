"""Budget accounting, with the enforceable/estimate distinction in the data model rather than the prose.

Two honesty notes that belong in the code, not only in a doc:

* Claude's ``total_cost_usd`` is an explicit **client-side estimate** computed from a price table
  bundled at build time.  The vendor documentation says not to trigger financial decisions from it.
* On a budget error the two usage surfaces disagree in opposite directions: ``usage`` omits the
  response that crossed the line while ``total_cost_usd`` includes it, and ``usage`` excludes
  subagents while ``total_cost_usd`` includes them.  A crashed result may carry an all-zero cost.
  :func:`reconcile` therefore treats all-zero usage on an errored result as ``"unavailable"``, not
  as ``0`` — a zero that means "we do not know" must not be recorded as a zero that means "free".

``spend_enforcement`` is per surface and **no surface is ``"enforced"`` today**.  The ``enforced``
label would rest entirely on a result subtype (``error_max_budget_usd``) that nobody has observed on
this host, and both surfaces that could carry it ship unavailable.  Labelling them ``enforced`` would
be a table lookup measuring nothing.  When a captured transcript containing that subtype exists, the
table flips and a gate asserts it.

Standard library only — the supervisor imports this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

SPEND_ENFORCEMENT: tuple[str, str, str] = ("enforced", "estimated", "unsupported")
COST_SOURCES: tuple[str, str, str] = ("provider_reported", "estimated", "unavailable")

# The refusal code for a reservation past the cap.  It is deliberately DISTINCT from the pre-existing
# latency code below, which means "the turn ran out of time", not "the turn ran out of money".
BUDGET_EXCEEDED_CODE: str = "budget_exceeded"
BUDGET_LATENCY_CODE: str = "budget_exhausted"  # already in use for the latency budget — DO NOT REUSE

_SPEND_ENFORCEMENT_BY_SURFACE: dict[str, str] = {
    "claude/stream": "estimated",  # NOT "enforced" — see the module docstring
    "claude/oneshot": "estimated",
    "codex/app-server": "unsupported",  # no currency anywhere in the protocol
    "codex/exec": "unsupported",
}

# Minor units per million tokens, keyed by price-table column.
_TOKEN_PRICE_KEYS: tuple[str, ...] = ("input", "output", "cached_input", "reasoning")
_TOKEN_USAGE_KEYS: dict[str, str] = {
    "input": "tokens_input",
    "output": "tokens_output",
    "cached_input": "tokens_cached_input",
    "reasoning": "tokens_reasoning",
}


class BudgetExceeded(RuntimeError):
    reserved_minor_units: int
    requested_minor_units: int
    cap_minor_units: int

    def __init__(self, reserved_minor_units: int, requested_minor_units: int, cap_minor_units: int) -> None:
        super().__init__(
            f"{BUDGET_EXCEEDED_CODE}: reserving {requested_minor_units} on top of {reserved_minor_units} "
            f"exceeds the cap of {cap_minor_units}"
        )
        self.reserved_minor_units = reserved_minor_units
        self.requested_minor_units = requested_minor_units
        self.cap_minor_units = cap_minor_units


@dataclass(frozen=True, slots=True)
class Reservation:
    reserved_minor_units: int
    currency: str
    spend_enforcement: Literal["enforced", "estimated", "unsupported"]
    cap_applied: Mapping[str, float] | None  # e.g. {"max_budget_usd": 2.0}; None when not enforceable


@dataclass(frozen=True, slots=True)
class Reconciliation:
    cost_minor_units: int
    cost_source: Literal["provider_reported", "estimated", "unavailable"]
    tokens_input: int
    tokens_output: int
    tokens_cached_input: int
    tokens_reasoning: int
    overshoot_minor_units: int


def spend_enforcement_for(surface: str) -> Literal["enforced", "estimated", "unsupported"]:
    return _enforcement_literal(_SPEND_ENFORCEMENT_BY_SURFACE.get(str(surface), "unsupported"))


def reserve(
    *,
    cap_minor_units: int,
    already_reserved_minor_units: int,
    request_minor_units: int,
    currency: str,
    surface: str,
) -> Reservation:
    """Reserve ``request_minor_units`` against a cap.

    ``cap_applied`` is populated only for an ``enforced`` surface, because it is the value that gets
    passed to the runtime as a real cap.  For ``estimated`` and ``unsupported`` surfaces it is None
    and the supervisor caps by wall clock, turn count and concurrency instead — those are the
    controls that actually hold.
    """
    reserved = int(already_reserved_minor_units)
    request = int(request_minor_units)
    cap = int(cap_minor_units)
    if request < 0:
        raise ValueError("request_minor_units must be >= 0")
    if reserved + request > cap:
        raise BudgetExceeded(reserved, request, cap)
    enforcement = spend_enforcement_for(surface)
    cap_applied: Mapping[str, float] | None = None
    if enforcement == "enforced":
        cap_applied = {"max_budget_usd": request / 100.0}
    return Reservation(
        reserved_minor_units=request,
        currency=str(currency or "USD"),
        spend_enforcement=enforcement,
        cap_applied=cap_applied,
    )


def reconcile(
    *,
    surface: str,
    result_tokens: Mapping[str, int],
    provider_cost_minor_units: int | None,
    price_table: Mapping[str, Mapping[str, int]],
    model_id: str,
    reservation: Reservation,
) -> Reconciliation:
    """Turn a runtime's usage report into a recorded cost, saying which kind of number it is.

    ``result_tokens`` may carry an ``is_error`` flag alongside the token counts.  An errored result
    whose usage is entirely zero is ``"unavailable"``: we do not know what it cost.
    """
    tokens = {name: int(result_tokens.get(key, 0) or 0) for key, name in _TOKEN_USAGE_KEYS.items()}
    is_error = bool(result_tokens.get("is_error", 0))
    all_zero = not any(tokens.values())

    cost_source: Literal["provider_reported", "estimated", "unavailable"]
    if provider_cost_minor_units is not None:
        cost = int(provider_cost_minor_units)
        cost_source = "provider_reported"
    elif all_zero and is_error:
        cost = 0
        cost_source = "unavailable"
    else:
        prices = price_table.get(str(model_id))
        if not prices or all_zero:
            cost = 0
            cost_source = "unavailable"
        else:
            total = 0
            for key in _TOKEN_PRICE_KEYS:
                per_million = int(prices.get(key, 0) or 0)
                total += tokens[_TOKEN_USAGE_KEYS[key]] * per_million
            cost = total // 1_000_000
            cost_source = "estimated"

    return Reconciliation(
        cost_minor_units=cost,
        cost_source=cost_source,
        tokens_input=tokens["tokens_input"],
        tokens_output=tokens["tokens_output"],
        tokens_cached_input=tokens["tokens_cached_input"],
        tokens_reasoning=tokens["tokens_reasoning"],
        overshoot_minor_units=max(0, cost - int(reservation.reserved_minor_units)),
    )


def reservation_to_json(reservation: Reservation) -> dict[str, Any]:
    return {
        "reserved_minor_units": reservation.reserved_minor_units,
        "currency": reservation.currency,
        "spend_enforcement": reservation.spend_enforcement,
        "cap_applied": dict(reservation.cap_applied) if reservation.cap_applied is not None else None,
    }


def reservation_from_json(value: Mapping[str, Any]) -> Reservation:
    cap_applied = value.get("cap_applied")
    return Reservation(
        reserved_minor_units=int(value.get("reserved_minor_units") or 0),
        currency=str(value.get("currency") or "USD"),
        spend_enforcement=_enforcement_literal(value.get("spend_enforcement")),
        cap_applied=({str(key): float(item) for key, item in cap_applied.items()} if isinstance(cap_applied, Mapping) else None),
    )


def reconciliation_to_json(reconciliation: Reconciliation) -> dict[str, Any]:
    return {
        "cost_minor_units": reconciliation.cost_minor_units,
        "cost_source": reconciliation.cost_source,
        "tokens_input": reconciliation.tokens_input,
        "tokens_output": reconciliation.tokens_output,
        "tokens_cached_input": reconciliation.tokens_cached_input,
        "tokens_reasoning": reconciliation.tokens_reasoning,
        "overshoot_minor_units": reconciliation.overshoot_minor_units,
    }


def reconciliation_from_json(value: Mapping[str, Any]) -> Reconciliation:
    return Reconciliation(
        cost_minor_units=int(value.get("cost_minor_units") or 0),
        cost_source=_cost_source_literal(value.get("cost_source")),
        tokens_input=int(value.get("tokens_input") or 0),
        tokens_output=int(value.get("tokens_output") or 0),
        tokens_cached_input=int(value.get("tokens_cached_input") or 0),
        tokens_reasoning=int(value.get("tokens_reasoning") or 0),
        overshoot_minor_units=int(value.get("overshoot_minor_units") or 0),
    )


def _enforcement_literal(value: Any) -> Literal["enforced", "estimated", "unsupported"]:
    raw = str(value or "unsupported")
    if raw == "enforced":
        return "enforced"
    if raw == "estimated":
        return "estimated"
    return "unsupported"


def _cost_source_literal(value: Any) -> Literal["provider_reported", "estimated", "unavailable"]:
    raw = str(value or "unavailable")
    if raw == "provider_reported":
        return "provider_reported"
    if raw == "estimated":
        return "estimated"
    return "unavailable"
