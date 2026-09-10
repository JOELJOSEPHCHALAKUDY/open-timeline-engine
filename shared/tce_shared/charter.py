"""The authority charter — the standing grant that sits *above* the per-directive permit.

A permit answers "may this actor do this one thing now?".  A charter answers "what is this
executor allowed to be, for the next N hours?" — which roots it may write, which capabilities the
broker may issue, which runtimes may be dispatched, and what the caps are.

Honesty is structural here.  ``CHARTER_FIELD_ENFORCEMENT`` names, for every single field of
``ResolvedCharter``, *who actually enforces it*: the operating system, the capability broker, the
manager process, or nobody.  ``"unsupported"`` is a real value and one field carries it:
``credential_risk_acknowledged``.  On this host a mutating ``os_sandbox`` dispatch holds a copy of
the runtime credential inside its task directory and has an unrestricted TLS channel on port 443;
nothing at the operating-system level prevents exfiltration.  The charter records that the human
acknowledged it.  It does not pretend to prevent it.

Pure and deterministic: standard library plus ``.behavior_control`` (the capability registry) and
``.execution_transitions`` (``canonical_json``).  No I/O, no pydantic, no SQLAlchemy.
"""

from __future__ import annotations

import hashlib
import json
import os.path
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Literal

from .behavior_control import CAPABILITY_REGISTRY
from .budget import SPEND_ENFORCEMENT, spend_enforcement_for
from .execution_transitions import canonical_json
from .runtime_contract import SURFACES

# ---- constants ---------------------------------------------------------------------------------
CHARTER_SCHEMA_VERSION: str = "v1"
CHARTER_POLICY_REVISION: str = "p3-2026-09"
CHARTER_STATUSES: frozenset[str] = frozenset({"draft", "active", "superseded", "revoked", "expired"})
ENFORCEMENT_TIERS: tuple[str, str, str] = ("os_sandbox", "container", "advisory")
EGRESS_MODES: tuple[str, str] = ("deny_all", "https_only")
FIELD_ENFORCED_BY: tuple[str, ...] = ("os", "broker", "manager", "advisory", "unsupported")
DEFAULT_PROTECTED_WRITE_PREFIXES: tuple[str, ...] = ("tests/", ".github/", ".local/")
DEFAULT_DENIED_READ_PATHS: tuple[str, ...] = ("~/.ssh", "~/Library/Keychains", "~/.aws", "~/.claude", "~/.codex")

# lower is stronger; a narrowing may only move DOWN
TIER_STRENGTH: dict[str, int] = {"os_sandbox": 0, "container": 1, "advisory": 2}

# Closed map: the action_kind values that exist in the tree -> CAPABILITY_REGISTRY keys.
# An unknown kind falls through to "process.execute" (the highest-risk mutating key) — fail closed,
# the same discipline as capability_policy in behavior_control.py.
CAPABILITY_FOR_ACTION_KIND: dict[str, str] = {
    "takeover_step": "process.execute",
    "execute": "process.execute",
    "edit": "filesystem.write",
    "write": "filesystem.write",
    "delete": "filesystem.write",
    "commit": "git.write",
    "read": "filesystem.read",
    "review": "filesystem.read",
}

# Which action kinds require an ACTIVE charter before claim_execution may proceed.
MUTATING_ACTION_KINDS: frozenset[str] = frozenset({"takeover_step", "execute", "edit", "write", "delete", "commit"})

# Reader for dispatch_records.task_family: the only families an "advisory" tier may host.
READ_ONLY_TASK_FAMILIES: frozenset[str] = frozenset({"read_only_review", "read_only_analysis", "read_only_probe"})

# The legacy inline token list, kept so that charter_sensitive_path_hit(None, ...) is byte-identical
# to the pre-P3 behaviour at main.py / store.py.
LEGACY_SENSITIVE_TOKENS: tuple[str, ...] = ("services/tce_mcp", "infra/", "secrets", ".env")

CAPTURE_CHANNEL_PAUSE_PREFIX: str = "AUTONOMOUS MODE PAUSED: capture channel"

# The honesty table.  Total over ResolvedCharter's fields — no orphans, no extras.
CHARTER_FIELD_ENFORCEMENT: dict[str, str] = {
    "charter_id": "manager",
    "workspace_id": "manager",
    "owner_id": "manager",
    "project_id": "manager",
    "charter_version": "manager",
    "policy_revision": "manager",
    "status": "manager",
    "approved_by": "manager",
    "approved_at": "manager",
    "expires_at": "manager",
    "revoked_at": "manager",
    "narrowing_ids": "manager",
    "charter_digest": "manager",
    "schema_version": "manager",
    "enforcement_tier": "os",  # the Seatbelt wrapper / the container
    "permitted_roots": "os",  # (allow file-write* (subpath <root>))
    "protected_write_prefixes": "os",  # one (deny file-write* (subpath <prefix>)) PER prefix
    # OS-enforced ONLY by the profile's blanket ``(deny file-read* (subpath HOMEDIR))``.  There are
    # no per-entry "targeted literal" clauses: ``render_profile`` never reads this field, and the
    # shipped ``executor_v2.sb`` (whose text is frozen by measurement) carries none.  So an entry
    # that expands under ``$HOME`` — every DEFAULT_DENIED_READ_PATHS entry does — is genuinely
    # denied, and an entry anywhere else is denied only if it happens to fall outside the profile's
    # read allow-list.  Measured: a charter naming "/private/etc/hosts" here reads it rc=0 inside
    # the live rendered profile, because /private/etc is an allowed subpath.
    # ``unenforced_denied_read_paths`` names exactly which entries are in that second class, and
    # ``build_governance_status`` publishes them as a limitation line rather than letting the "os"
    # label above cover them silently.
    "denied_read_paths": "os",
    "egress_mode": "os",  # deny network* [+ port 443 + the DNS unix socket]
    "permitted_capabilities": "broker",  # a narrowing of CAPABILITY_REGISTRY
    "confirm_required_capabilities": "broker",
    "runtime_allowlist": "manager",  # asserted against --version at dispatch
    "caps": "manager",  # see CAPS_FIELD_ENFORCEMENT
    # The one field whose named enforcer does not exist, and deliberately so: containment of the
    # runtime credential is not enforceable on this host.  See the module docstring and docs/charter.md.
    "credential_risk_acknowledged": "unsupported",
}

CAPS_FIELD_ENFORCEMENT: dict[str, str] = {
    "max_attempts": "manager",  # min()-ed at the retry-mint site
    "max_concurrent_dispatches": "manager",  # open_dispatch's 409 dispatch_concurrency_exceeded
    "max_wall_seconds": "manager",  # the observe-loop deadline -> adapter.kill
    "budget_minor_units": "manager",  # ENFORCED only where spend_enforcement == "enforced"
    "budget_currency": "manager",
    "spend_enforcement": "manager",
}

# The limitation line that must be emitted whenever an os_sandbox charter is active.  This is the
# text that makes the "unsupported" entry above visible to an operator rather than buried in a table.
CREDENTIAL_CONTAINMENT_LIMITATION: str = (
    "A mutating dispatch under enforcement_tier=os_sandbox holds a copy of the runtime credential "
    "inside its task directory and has an unrestricted TLS channel on port 443 to any host; the "
    "credential is exfiltrable and nothing at the operating-system level prevents it."
)

# Emitted, with the offending entries appended, whenever an active charter names a denied_read_path
# the shipped profile does not actually deny.  See CHARTER_FIELD_ENFORCEMENT["denied_read_paths"].
DENIED_READ_PATHS_LIMITATION: str = (
    "These denied_read_paths entries are NOT enforced by the sandbox: the profile denies reads "
    "under $HOME wholesale and renders no per-entry clause, so an entry outside $HOME is advisory "
    "only"
)


# ---- exceptions --------------------------------------------------------------------------------
class CharterInvalid(ValueError):
    """A create/approve payload that cannot become a charter.  ``field`` names the offending key."""

    field: str

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class CharterExpired(RuntimeError):
    charter_id: str
    expired_at: datetime

    def __init__(self, charter_id: str, expired_at: datetime) -> None:
        super().__init__(f"charter {charter_id} expired at {expired_at.isoformat()}")
        self.charter_id = charter_id
        self.expired_at = expired_at


class CharterRevoked(RuntimeError):
    """Raised when a resolve finds status='revoked'.  Distinct from CharterExpired so the route can
    return a distinct code; both map to 409 charter_not_active."""

    charter_id: str
    revoked_at: datetime

    def __init__(self, charter_id: str, revoked_at: datetime) -> None:
        super().__init__(f"charter {charter_id} was revoked at {revoked_at.isoformat()}")
        self.charter_id = charter_id
        self.revoked_at = revoked_at


class CharterRequired(RuntimeError):
    """Raised by the shared guard when a mutating action kind has no active charter and charter
    enforcement is on.  Carries exactly what the refusal body must name."""

    action_kind: str
    reason: str

    def __init__(self, action_kind: str, reason: str) -> None:
        super().__init__(f"action {action_kind!r} requires an active charter: {reason}")
        self.action_kind = action_kind
        self.reason = reason


# ---- helpers -----------------------------------------------------------------------------------
def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _norm_rel(path: str) -> str:
    """Normalise a repo-relative POSIX path.  Returns "" for anything that escapes the tree."""
    raw = str(path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return ""
    parts: list[str] = []
    for chunk in raw.split("/"):
        if chunk in ("", "."):
            continue
        if chunk == "..":
            return ""
        parts.append(chunk)
    return "/".join(parts)


def _under(path: str, prefix: str) -> bool:
    """True iff normalised ``path`` sits at or below normalised ``prefix``."""
    normalized_prefix = _norm_rel(prefix)
    normalized_path = _norm_rel(path)
    if not normalized_path:
        return False
    if not normalized_prefix:
        # "." / "" means the whole tree, but only when the prefix itself was a repo-root token.
        return str(prefix or "").strip() in (".", "./", "")
    return normalized_path == normalized_prefix or normalized_path.startswith(normalized_prefix + "/")


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _parse_dt_opt(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_dt(value)


# ---- dataclasses -------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CharterCaps:
    max_attempts: int
    max_concurrent_dispatches: int
    max_wall_seconds: int
    budget_minor_units: int
    budget_currency: str
    spend_enforcement: Literal["enforced", "estimated", "unsupported"]


@dataclass(frozen=True, slots=True)
class ResolvedCharter:
    charter_id: str
    workspace_id: str
    owner_id: str
    project_id: str | None
    charter_version: str
    policy_revision: str
    status: str
    enforcement_tier: Literal["os_sandbox", "container", "advisory"]
    permitted_roots: tuple[str, ...]
    protected_write_prefixes: tuple[str, ...]
    denied_read_paths: tuple[str, ...]
    permitted_capabilities: frozenset[str]
    confirm_required_capabilities: frozenset[str]
    egress_mode: Literal["deny_all", "https_only"]
    runtime_allowlist: tuple[tuple[str, str, str], ...]  # (runtime_id, runtime_version, surface)
    caps: CharterCaps
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    narrowing_ids: tuple[str, ...]
    charter_digest: str
    schema_version: str = CHARTER_SCHEMA_VERSION
    credential_risk_acknowledged: bool = False

    def is_active(self, now: datetime) -> bool:
        if self.status != "active":
            return False
        if self.revoked_at is not None:
            return False
        return now < self.expires_at

    def allows_capability(self, capability: str) -> bool:
        return str(capability) in self.permitted_capabilities

    def allows_path(self, path: str) -> bool:
        """True iff ``path`` (repo-relative, normalised, no '..') is under some permitted root."""
        return any(_under(path, root) for root in self.permitted_roots)

    def is_protected_write(self, path: str) -> bool:
        """True iff ``path`` is under ANY protected_write_prefixes entry.

        Iterates EVERY prefix — a loop that stops at the first is exactly the bug the sandbox
        profile renderer had, where only the first deny clause was emitted.
        """
        return any(_under(path, prefix) for prefix in self.protected_write_prefixes)

    def allows_runtime(self, runtime_id: str, runtime_version: str, surface: str) -> bool:
        candidate = (str(runtime_id), str(runtime_version), str(surface))
        return candidate in self.runtime_allowlist


# ---- functions ---------------------------------------------------------------------------------
def charter_payload_digest(payload: Mapping[str, Any]) -> str:
    return _sha256_hex(canonical_json(dict(payload)))


def capability_for_action_kind(action_kind: str) -> str:
    return CAPABILITY_FOR_ACTION_KIND.get(str(action_kind or "").strip().lower(), "process.execute")


def is_mutating_action_kind(action_kind: str) -> bool:
    return str(action_kind or "").strip().lower() in MUTATING_ACTION_KINDS


def validate_charter_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise + validate a charter create/approve body.  Raises ``CharterInvalid(field, message)``.

    TTL bounds are NOT checked here — they are settings, and this module is settings-free by design
    (Full's and Lite's ``Settings`` are different classes with the same name).  ``create_charter``
    enforces them.
    """
    for key in payload:
        if "domain" in str(key).lower():
            # Hostname/domain egress control has no OS backing on this host; a field named for it
            # would be a promise the sandbox cannot keep.
            raise CharterInvalid(str(key), "domain-level network policy is unsupported; no charter field may name it")

    tier = str(payload.get("enforcement_tier") or "").strip()
    if tier not in ENFORCEMENT_TIERS:
        raise CharterInvalid("enforcement_tier", f"enforcement_tier must be one of {ENFORCEMENT_TIERS!r}")

    egress = str(payload.get("egress_mode") or "deny_all").strip()
    if egress not in EGRESS_MODES:
        raise CharterInvalid("egress_mode", f"egress_mode must be one of {EGRESS_MODES!r}")

    capabilities = _str_tuple(payload.get("permitted_capabilities"))
    for capability in capabilities:
        if capability not in CAPABILITY_REGISTRY:
            raise CharterInvalid("permitted_capabilities", f"unknown capability {capability!r}")
    confirm_capabilities = _str_tuple(payload.get("confirm_required_capabilities"))
    for capability in confirm_capabilities:
        if capability not in CAPABILITY_REGISTRY:
            raise CharterInvalid("confirm_required_capabilities", f"unknown capability {capability!r}")

    mutating = [c for c in capabilities if bool(CAPABILITY_REGISTRY[c]["mutating"])]
    if tier == "advisory" and mutating:
        raise CharterInvalid(
            "permitted_capabilities",
            "enforcement_tier='advisory' applies no OS boundary and may not carry a mutating capability",
        )
    if tier == "os_sandbox" and mutating and not bool(payload.get("credential_risk_acknowledged")):
        raise CharterInvalid(
            "credential_risk_acknowledged",
            "a mutating os_sandbox charter must acknowledge that the runtime credential is exfiltrable "
            "on this host; see docs/charter.md",
        )

    roots = _str_tuple(payload.get("permitted_roots"))
    if not roots:
        raise CharterInvalid("permitted_roots", "permitted_roots must not be empty")
    prefixes = _str_tuple(payload.get("protected_write_prefixes"))
    if not prefixes:
        # Measured: a zero-clause render SILENTLY disables the deny — `(deny file-write*)` with no
        # subpath denied nothing — and the DDL defaults the column to '[]'.
        raise CharterInvalid("protected_write_prefixes", "protected_write_prefixes must not be empty")

    for field_name, values in (("permitted_roots", roots), ("protected_write_prefixes", prefixes)):
        for value in values:
            raw = str(value).strip()
            if not raw or raw.startswith("/") or raw.startswith("~") or "\\" in raw or ".." in raw.split("/"):
                raise CharterInvalid(field_name, f"{raw!r} must be a relative POSIX path with no '..' and no leading '/'")

    caps_payload = dict(payload.get("caps") or {})
    # A charter may not ADVERTISE a spend ceiling this host cannot apply.  `spend_enforcement` is
    # the one cap whose value is read straight onto the wire: `build_governance_status` reports it
    # as `spend_enforcement` and SUPPRESSES the "recorded cost is an estimate and may overshoot"
    # limitation line when it reads "enforced".  Left unvalidated, an operator typing "enforced"
    # into a create body would turn the honest line off while every dispatch row still recorded
    # `unsupported`/`estimated` from `budget.spend_enforcement_for(surface)` — the measured table.
    # So the claim is checked against that table rather than trusted.  This refusal lifts itself:
    # when a captured `error_max_budget_usd` transcript flips a surface to "enforced", the set below
    # gains a member and the charter field becomes legal in the same commit.
    spend = str(caps_payload.get("spend_enforcement") or "unsupported").strip()
    if spend not in SPEND_ENFORCEMENT:
        raise CharterInvalid("caps", f"caps.spend_enforcement must be one of {SPEND_ENFORCEMENT!r}")
    if spend == "enforced" and "enforced" not in {spend_enforcement_for(surface) for surface in SURFACES}:
        raise CharterInvalid(
            "caps",
            "caps.spend_enforcement='enforced' is not available on this host: no runtime surface in "
            "tce_shared.budget enforces a spend ceiling, so the label would be a claim with nothing "
            "behind it and would suppress the governance limitation line. Use 'estimated' or 'unsupported'.",
        )
    caps: dict[str, Any] = {
        "max_attempts": int(caps_payload.get("max_attempts", 3)),
        "max_concurrent_dispatches": int(caps_payload.get("max_concurrent_dispatches", 1)),
        "max_wall_seconds": int(caps_payload.get("max_wall_seconds", 1800)),
        "budget_minor_units": int(caps_payload.get("budget_minor_units", 0)),
        "budget_currency": str(caps_payload.get("budget_currency") or "USD"),
        "spend_enforcement": spend,
    }
    for name in ("max_attempts", "max_concurrent_dispatches", "max_wall_seconds"):
        if int(caps[name]) < 1:
            raise CharterInvalid("caps", f"caps.{name} must be >= 1")
    if int(caps["budget_minor_units"]) < 0:
        raise CharterInvalid("caps", "caps.budget_minor_units must be >= 0")

    runtime_allowlist: list[list[str]] = []
    for entry in payload.get("runtime_allowlist") or ():
        triple = [str(item) for item in entry]
        if len(triple) != 3:
            raise CharterInvalid("runtime_allowlist", "each runtime_allowlist entry is [runtime_id, runtime_version, surface]")
        runtime_allowlist.append(triple)

    normalized: dict[str, Any] = {
        "project_id": (str(payload["project_id"]) if payload.get("project_id") else None),
        "charter_version": str(payload.get("charter_version") or CHARTER_SCHEMA_VERSION),
        "policy_revision": str(payload.get("policy_revision") or CHARTER_POLICY_REVISION),
        "enforcement_tier": tier,
        "egress_mode": egress,
        "credential_risk_acknowledged": bool(payload.get("credential_risk_acknowledged")),
        "permitted_roots": [_norm_rel(root) or str(root).strip() for root in roots],
        "protected_write_prefixes": sorted({str(prefix).strip() for prefix in prefixes}),
        "denied_read_paths": sorted({str(path).strip() for path in (_str_tuple(payload.get("denied_read_paths")) or DEFAULT_DENIED_READ_PATHS)}),
        "permitted_capabilities": sorted(set(capabilities)),
        "confirm_required_capabilities": sorted(set(confirm_capabilities)),
        "runtime_allowlist": runtime_allowlist,
        "task_families": sorted({str(item).strip() for item in (payload.get("task_families") or ())}),
        "caps": caps,
    }
    normalized["charter_digest"] = charter_payload_digest(normalized)
    return normalized


def charter_from_row(row: Mapping[str, Any], *, narrowings: Sequence[Mapping[str, Any]] = ()) -> ResolvedCharter:
    """Build the resolved charter from an ``authority_charters`` row, then fold every narrowing row
    through :func:`narrow_charter` in the order supplied.

    THIS IS THE READER of ``charter_narrowings``.  Without it the trusted narrowing channel is
    write-only.  The caller (``load_active_charter``) is responsible for excluding expired
    narrowings in its query; every row handed here is applied and recorded in ``narrowing_ids``.
    """
    caps = CharterCaps(
        max_attempts=int(row.get("max_attempts", 3) or 3),
        max_concurrent_dispatches=int(row.get("max_concurrent_dispatches", 1) or 1),
        max_wall_seconds=int(row.get("max_wall_seconds", 1800) or 1800),
        budget_minor_units=int(row.get("budget_minor_units", 0) or 0),
        budget_currency=str(row.get("budget_currency") or "USD"),
        spend_enforcement=_spend_enforcement_literal(row.get("spend_enforcement")),
    )
    charter = ResolvedCharter(
        charter_id=str(row.get("id") or row.get("charter_id") or ""),
        workspace_id=str(row.get("workspace_id") or ""),
        owner_id=str(row.get("owner_id") or ""),
        project_id=(str(row["project_id"]) if row.get("project_id") else None),
        charter_version=str(row.get("charter_version") or CHARTER_SCHEMA_VERSION),
        policy_revision=str(row.get("policy_revision") or CHARTER_POLICY_REVISION),
        status=str(row.get("status") or "draft"),
        enforcement_tier=_tier_literal(row.get("enforcement_tier")),
        permitted_roots=_str_tuple(_json_list(row.get("permitted_roots_json") or row.get("permitted_roots"))),
        protected_write_prefixes=_str_tuple(_json_list(row.get("protected_write_prefixes_json") or row.get("protected_write_prefixes"))),
        denied_read_paths=_str_tuple(_json_list(row.get("denied_read_paths_json") or row.get("denied_read_paths"))),
        permitted_capabilities=frozenset(_str_tuple(_json_list(row.get("permitted_capabilities_json") or row.get("permitted_capabilities")))),
        confirm_required_capabilities=frozenset(
            _str_tuple(_json_list(row.get("confirm_required_capabilities_json") or row.get("confirm_required_capabilities")))
        ),
        egress_mode=_egress_literal(row.get("egress_mode")),
        runtime_allowlist=_runtime_allowlist(_json_list(row.get("runtime_allowlist_json") or row.get("runtime_allowlist"))),
        caps=caps,
        approved_by=str(row.get("approved_by") or ""),
        approved_at=_parse_dt(row.get("approved_at") or row.get("created_at")),
        expires_at=_parse_dt(row.get("expires_at")),
        revoked_at=_parse_dt_opt(row.get("revoked_at")),
        narrowing_ids=(),
        charter_digest=str(row.get("charter_digest") or ""),
        schema_version=str(row.get("schema_version") or CHARTER_SCHEMA_VERSION),
        credential_risk_acknowledged=bool(row.get("credential_risk_acknowledged")),
    )
    applied: list[str] = []
    for narrowing_row in narrowings:
        payload = narrowing_row.get("narrowing_json") or narrowing_row.get("narrowing") or {}
        if isinstance(payload, str):
            payload = _json_obj(payload)
        charter = narrow_charter(charter, payload)
        applied.append(str(narrowing_row.get("id") or narrowing_row.get("narrowing_id") or ""))
    if applied:
        charter = _replace_narrowing_ids(charter, tuple(applied))
    return charter


def narrow_charter(charter: ResolvedCharter, narrowing: Mapping[str, Any]) -> ResolvedCharter:
    """Monotone narrowing.  Never raises; an unrecognised key is ignored.

    Adding to a deny list (``protected_write_prefixes``, ``denied_read_paths``) narrows; removing
    from an allow list (``permitted_roots``, ``permitted_capabilities``) narrows; every cap is a
    ``min``; ``egress_mode`` may only move to ``deny_all``; the tier may only strengthen.
    """
    # `remove_roots` is matched on the NORMALISED form as well as the raw one.  The roots stored on
    # an approved charter have already been through `validate_charter_payload`, which normalises
    # "docs/" to "docs"; matching raw-only meant a narrowing that named "docs/" removed nothing and
    # reported success.  A narrowing that silently no-ops is worse than one that is refused, because
    # the operator is told the authority shrank when it did not.  Normalising can only ever match
    # MORE stored roots, never fewer, so the monotonicity G11 asserts is unaffected.
    requested_removals = _str_tuple(narrowing.get("remove_roots"))
    removed_roots = {str(item).strip() for item in requested_removals if str(item).strip()}
    removed_roots |= {_norm_rel(item) for item in requested_removals if _norm_rel(item)}
    roots = tuple(
        root
        for root in charter.permitted_roots
        if str(root).strip() not in removed_roots
        and not (_norm_rel(root) and _norm_rel(root) in removed_roots)
    )
    capabilities = charter.permitted_capabilities - frozenset(_str_tuple(narrowing.get("remove_capabilities")))
    protected = tuple(sorted(set(charter.protected_write_prefixes) | set(_str_tuple(narrowing.get("add_protected_write_prefixes")))))
    denied = tuple(sorted(set(charter.denied_read_paths) | set(_str_tuple(narrowing.get("add_denied_read_paths")))))

    caps = CharterCaps(
        max_attempts=min(charter.caps.max_attempts, _int_or(narrowing.get("max_attempts"), charter.caps.max_attempts)),
        max_concurrent_dispatches=min(
            charter.caps.max_concurrent_dispatches,
            _int_or(narrowing.get("max_concurrent_dispatches"), charter.caps.max_concurrent_dispatches),
        ),
        max_wall_seconds=min(charter.caps.max_wall_seconds, _int_or(narrowing.get("max_wall_seconds"), charter.caps.max_wall_seconds)),
        budget_minor_units=min(charter.caps.budget_minor_units, _int_or(narrowing.get("budget_minor_units"), charter.caps.budget_minor_units)),
        budget_currency=charter.caps.budget_currency,
        spend_enforcement=charter.caps.spend_enforcement,
    )

    tier = str(narrowing.get("enforcement_tier") or charter.enforcement_tier)
    if tier not in TIER_STRENGTH or TIER_STRENGTH[tier] > TIER_STRENGTH[charter.enforcement_tier]:
        tier = charter.enforcement_tier
    egress = "deny_all" if str(narrowing.get("egress_mode") or "") == "deny_all" else charter.egress_mode

    expires_at = charter.expires_at
    narrowed_expiry = _parse_dt_opt(narrowing.get("expires_at"))
    if narrowed_expiry is not None and narrowed_expiry < expires_at:
        expires_at = narrowed_expiry

    return ResolvedCharter(
        charter_id=charter.charter_id,
        workspace_id=charter.workspace_id,
        owner_id=charter.owner_id,
        project_id=charter.project_id,
        charter_version=charter.charter_version,
        policy_revision=charter.policy_revision,
        status=charter.status,
        enforcement_tier=_tier_literal(tier),
        permitted_roots=roots,
        protected_write_prefixes=protected,
        denied_read_paths=denied,
        permitted_capabilities=capabilities,
        confirm_required_capabilities=charter.confirm_required_capabilities,
        egress_mode=_egress_literal(egress),
        runtime_allowlist=charter.runtime_allowlist,
        caps=caps,
        approved_by=charter.approved_by,
        approved_at=charter.approved_at,
        expires_at=expires_at,
        revoked_at=charter.revoked_at,
        narrowing_ids=charter.narrowing_ids,
        charter_digest=charter.charter_digest,
        schema_version=charter.schema_version,
        credential_risk_acknowledged=charter.credential_risk_acknowledged,
    )


def resolved_charter_to_json(charter: ResolvedCharter) -> dict[str, Any]:
    return {
        "charter_id": charter.charter_id,
        "workspace_id": charter.workspace_id,
        "owner_id": charter.owner_id,
        "project_id": charter.project_id,
        "charter_version": charter.charter_version,
        "policy_revision": charter.policy_revision,
        "status": charter.status,
        "enforcement_tier": charter.enforcement_tier,
        "permitted_roots": list(charter.permitted_roots),
        "protected_write_prefixes": list(charter.protected_write_prefixes),
        "denied_read_paths": list(charter.denied_read_paths),
        "permitted_capabilities": sorted(charter.permitted_capabilities),
        "confirm_required_capabilities": sorted(charter.confirm_required_capabilities),
        "egress_mode": charter.egress_mode,
        "runtime_allowlist": [list(entry) for entry in charter.runtime_allowlist],
        "caps": {
            "max_attempts": charter.caps.max_attempts,
            "max_concurrent_dispatches": charter.caps.max_concurrent_dispatches,
            "max_wall_seconds": charter.caps.max_wall_seconds,
            "budget_minor_units": charter.caps.budget_minor_units,
            "budget_currency": charter.caps.budget_currency,
            "spend_enforcement": charter.caps.spend_enforcement,
        },
        "approved_by": charter.approved_by,
        "approved_at": _iso(charter.approved_at),
        "expires_at": _iso(charter.expires_at),
        "revoked_at": _iso(charter.revoked_at),
        "narrowing_ids": list(charter.narrowing_ids),
        "charter_digest": charter.charter_digest,
        "schema_version": charter.schema_version,
        "credential_risk_acknowledged": charter.credential_risk_acknowledged,
    }


def resolved_charter_from_json(value: Mapping[str, Any]) -> ResolvedCharter:
    caps_value = dict(value.get("caps") or {})
    caps = CharterCaps(
        max_attempts=int(caps_value.get("max_attempts", 3)),
        max_concurrent_dispatches=int(caps_value.get("max_concurrent_dispatches", 1)),
        max_wall_seconds=int(caps_value.get("max_wall_seconds", 1800)),
        budget_minor_units=int(caps_value.get("budget_minor_units", 0)),
        budget_currency=str(caps_value.get("budget_currency") or "USD"),
        spend_enforcement=_spend_enforcement_literal(caps_value.get("spend_enforcement")),
    )
    return ResolvedCharter(
        charter_id=str(value.get("charter_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        owner_id=str(value.get("owner_id") or ""),
        project_id=(str(value["project_id"]) if value.get("project_id") else None),
        charter_version=str(value.get("charter_version") or CHARTER_SCHEMA_VERSION),
        policy_revision=str(value.get("policy_revision") or CHARTER_POLICY_REVISION),
        status=str(value.get("status") or "draft"),
        enforcement_tier=_tier_literal(value.get("enforcement_tier")),
        permitted_roots=_str_tuple(value.get("permitted_roots")),
        protected_write_prefixes=_str_tuple(value.get("protected_write_prefixes")),
        denied_read_paths=_str_tuple(value.get("denied_read_paths")),
        permitted_capabilities=frozenset(_str_tuple(value.get("permitted_capabilities"))),
        confirm_required_capabilities=frozenset(_str_tuple(value.get("confirm_required_capabilities"))),
        egress_mode=_egress_literal(value.get("egress_mode")),
        runtime_allowlist=_runtime_allowlist(value.get("runtime_allowlist")),
        caps=caps,
        approved_by=str(value.get("approved_by") or ""),
        approved_at=_parse_dt(value.get("approved_at")),
        expires_at=_parse_dt(value.get("expires_at")),
        revoked_at=_parse_dt_opt(value.get("revoked_at")),
        narrowing_ids=_str_tuple(value.get("narrowing_ids")),
        charter_digest=str(value.get("charter_digest") or ""),
        schema_version=str(value.get("schema_version") or CHARTER_SCHEMA_VERSION),
        credential_risk_acknowledged=bool(value.get("credential_risk_acknowledged")),
    )


def charter_sensitive_path_hit(charter: ResolvedCharter | None, target_paths: Sequence[str]) -> bool:
    """The single path-sensitivity question, replacing three inconsistent inline lists.

    ``charter is None`` falls back to the four legacy tokens so behaviour is byte-identical without
    a charter.  With a charter: a path is sensitive when it is a protected write, or when it is not
    under any permitted root at all.
    """
    paths = [str(path) for path in (target_paths or ())]
    if charter is None:
        lowered = [path.lower() for path in paths]
        return any(token in path for path in lowered for token in LEGACY_SENSITIVE_TOKENS)
    return any(charter.is_protected_write(path) or not charter.allows_path(path) for path in paths)


def charter_scope_narrowing(charter: ResolvedCharter | None, action_kind: str, target_paths: Sequence[str]) -> tuple[bool, str]:
    """``(blocked, reason)`` for the charter clause of ``evaluate_execution_permit``.

    A charter blocks outright only for a *mutating* action kind: a read outside the roots is not an
    escalation, and blocking it would change read behaviour that the permit ladder never gated.
    """
    if charter is None:
        return (False, "")
    capability = capability_for_action_kind(action_kind)
    if not charter.allows_capability(capability):
        return (True, f"charter: capability {capability} is not permitted")
    if not is_mutating_action_kind(action_kind):
        return (False, "")
    for path in target_paths or ():
        if charter.is_protected_write(path):
            return (True, f"charter: {path} is under a protected write prefix")
        if not charter.allows_path(path):
            return (True, f"charter: {path} is outside every permitted root")
    return (False, "")


def unenforced_denied_read_paths(charter: ResolvedCharter) -> tuple[str, ...]:
    """The ``denied_read_paths`` entries no OS clause actually denies.

    The shipped Seatbelt profile denies reads under ``$HOME`` wholesale and renders **no** clause
    from this field — ``render_profile`` never reads it.  So an entry that expands under ``$HOME``
    is genuinely OS-denied and everything else is a statement of intent that the sandbox may or may
    not happen to cover.  Measured: a charter naming ``/private/etc/hosts`` reads it ``rc=0`` inside
    the live rendered profile, because ``/private/etc`` is one of the profile's read allows.

    Returned rather than raised, and published as a limitation rather than refused, because the
    field is also consumed by non-``os_sandbox`` tiers and by narrowings — and ``narrow_charter``
    is documented never to raise.
    """
    home = os.path.expanduser("~").rstrip("/")
    unenforced: list[str] = []
    for entry in charter.denied_read_paths:
        raw = str(entry).strip()
        if not raw:
            continue
        expanded = os.path.expanduser(raw).rstrip("/")
        if expanded == home or expanded.startswith(home + "/"):
            continue
        unenforced.append(raw)
    return tuple(unenforced)


def charter_constraints(charter: ResolvedCharter) -> list[dict[str, Any]]:
    """The machine-readable constraint rules an executor receives, floor first.

    The two floor rules are prepended unconditionally: a charter may only ADD to the floor, never
    remove it.  Replacing the floor wholesale would let the first approved charter silently delete
    the constraint protecting the enforcement code and the one protecting the MCP firewall that the
    whole constraint channel depends on.

    ``polarity`` is mandatory on every rule.  ``scope.path_prefixes`` is a DENY list in every rule
    but ``charter-roots-only``, where it is an ALLOW list; without the flag a conforming executor
    reads the allow-list as a deny-list and inverts the charter.
    """
    rules: list[dict[str, Any]] = [
        {
            "directive_type": "hard_constraint",
            "rule_id": "no-edit-protected-dirs",
            "polarity": "deny",
            "scope": {
                "path_prefixes": [
                    "shared/tce_shared/",
                    "services/tce_api/",
                    "services/tce_lite_api/",
                    "services/tce_mcp/",
                    "scripts/",
                    "infra/",
                ],
                "actions": ["edit", "write", "delete"],
            },
            "enforcement": "block_and_escalate",
            "reason": (
                "These directories contain core TCE infrastructure maintained "
                "by the advisor session. Report bugs to the user instead of fixing them."
            ),
        },
        {
            "directive_type": "hard_constraint",
            "rule_id": "no-edit-firewall-null-response",
            "polarity": "deny",
            "scope": {
                "path_prefixes": ["services/tce_mcp/"],
                "actions": ["edit"],
            },
            "enforcement": "block_and_escalate",
            "reason": (
                "final_response=null when has_directive=true is INTENTIONAL. "
                "The directive is delivered via next_step to prevent LLM echoing. "
                "Do NOT treat this as a bug. Do NOT edit MCP code to 'fix' it."
            ),
        },
        {
            "directive_type": "hard_constraint",
            "rule_id": "charter-no-write-protected-prefixes",
            "polarity": "deny",
            "scope": {
                "path_prefixes": list(charter.protected_write_prefixes),
                "actions": ["edit", "write", "delete"],
            },
            "enforcement": "block_and_escalate",
            "reason": (
                "The active authority charter protects these prefixes from writes. The OS sandbox "
                "denies them for the dispatched process tree; outside that tree this rule is the "
                "only statement of the boundary."
            ),
        },
        {
            "directive_type": "hard_constraint",
            "rule_id": "charter-roots-only",
            "polarity": "allow_only",
            "scope": {
                "path_prefixes": list(charter.permitted_roots),
                "actions": ["edit", "write", "delete"],
            },
            "enforcement": "block_and_escalate",
            "reason": (
                "polarity=allow_only: these path_prefixes are the ONLY writable roots under the "
                "active charter. A write anywhere else is refused."
            ),
        },
    ]
    for capability in sorted(charter.confirm_required_capabilities):
        rules.append(
            {
                "directive_type": "hard_constraint",
                "rule_id": f"charter-capability-confirm:{capability}",
                "polarity": "deny",
                "scope": {"path_prefixes": [], "actions": [capability]},
                "enforcement": "pre_action_required",
                "reason": (
                    f"The active authority charter requires explicit human confirmation before using "
                    f"the {capability} capability."
                ),
            }
        )
    rules.append(
        {
            "directive_type": "hard_constraint",
            "rule_id": "pause-when-capture-channel-down",
            "polarity": "deny",
            "scope": {
                "actions": ["edit", "write", "delete", "execute"],
            },
            "enforcement": "pre_action_required",
            "reason": (
                "Before any mutating action, check capture_delivery_state in this result. If it is 'gap' or "
                "'unavailable' and the autonomy profile is unattended, do NOT edit, write, delete, or execute; "
                "tell the user the trusted human-input capture channel is down and ask them to restore the host "
                "capture hook or approve continuing consultatively (autonomy profile human_consultative)."
            ),
        }
    )
    return rules


def require_charter_for_action(
    charter: ResolvedCharter | None,
    *,
    action_kind: str,
    enforcement_enabled: bool,
    now: datetime,
) -> None:
    """The single shared refusal.  Raises ``CharterRequired(action_kind, reason)``.

    ``reason`` is exactly one of ``"no_active_charter"``, ``"charter_expired"``,
    ``"charter_revoked"``.  When ``enforcement_enabled`` is False this ALWAYS returns None — that is
    the documented off switch (``TCE_CHARTER_ENFORCEMENT_ENABLED=0``).

    It takes ``enforcement_enabled: bool`` and not a ``settings`` object deliberately: Full's
    ``tce_api.config.Settings`` and Lite's ``tce_lite_api.config.Settings`` are different classes
    with the same name, so a shared helper annotated ``settings: Settings`` is ambiguous.
    """
    if not enforcement_enabled:
        return
    if not is_mutating_action_kind(action_kind):
        return
    if charter is None:
        raise CharterRequired(action_kind, "no_active_charter")
    if charter.revoked_at is not None:
        raise CharterRequired(action_kind, "charter_revoked")
    if not charter.is_active(now):
        if now >= charter.expires_at:
            raise CharterRequired(action_kind, "charter_expired")
        raise CharterRequired(action_kind, "no_active_charter")
    return


# ---- private coercion helpers ------------------------------------------------------------------
def _tier_literal(value: Any) -> Literal["os_sandbox", "container", "advisory"]:
    raw = str(value or "advisory").strip()
    if raw == "os_sandbox":
        return "os_sandbox"
    if raw == "container":
        return "container"
    return "advisory"


def _egress_literal(value: Any) -> Literal["deny_all", "https_only"]:
    return "https_only" if str(value or "deny_all").strip() == "https_only" else "deny_all"


def _spend_enforcement_literal(value: Any) -> Literal["enforced", "estimated", "unsupported"]:
    raw = str(value or "unsupported").strip()
    if raw == "enforced":
        return "enforced"
    if raw == "estimated":
        return "estimated"
    return "unsupported"


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _json_obj(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _runtime_allowlist(value: Any) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for entry in _json_list(value):
        triple = [str(item) for item in entry]
        if len(triple) == 3:
            entries.append((triple[0], triple[1], triple[2]))
    return tuple(entries)


def _int_or(value: Any, fallback: int) -> int:
    if value is None:
        return int(fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _replace_narrowing_ids(charter: ResolvedCharter, narrowing_ids: tuple[str, ...]) -> ResolvedCharter:
    values = {field.name: getattr(charter, field.name) for field in fields(charter)}
    values["narrowing_ids"] = narrowing_ids
    return ResolvedCharter(**values)
