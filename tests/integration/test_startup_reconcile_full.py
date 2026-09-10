"""The FULL reaper's effect resolution: a reaped directive's irreversible effect becomes ``unknown``.

Skips (never errors) without ``TCE_DATABASE_URL``, and skips when the P3 tables are absent —
the migration is applied by the deploy step, not by the test.

What this pins is the difference between "closed" and "we do not know".  The reaper turns a stuck
``in_progress`` directive into ``abandoned``, which is terminal.  If nothing records that a
possibly-irreversible effect was in flight when that attempt died, the effect row still reads
``running``, ``pause_required`` calls that a healthy dispatch, and ``takeover_step`` cheerfully
mints a fresh directive over an outcome nobody knows.

The load-bearing test is ``test_takeover_step_that_reaps_a_stale_directive_pauses_in_the_same_turn``:
it drives the reap through the real ``POST /v1/takeover/step``, which is the only path production
actually takes.  ``startup_reconcile`` is covered too, but it is the restart path — asserting only
against it would have tested a branch the live system never reaches.

Measured before and after the fix, against the live Postgres: with the in-turn resolution stubbed
out, the effect stays ``running``, the session's directive count goes 1 -> 2 and a fresh ``pending``
directive appears; with it in place the effect is ``unknown``, the count stays 1, and the turn
returns the pause.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

pytestmark = pytest.mark.integration

_P3_TABLES = ("effect_journal", "directive_executions")


@pytest.fixture()
def engine() -> Iterator[object]:
    url = os.environ.get("TCE_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TCE_DATABASE_URL is not set")
    import sqlalchemy as sa

    eng = sa.create_engine(url, future=True)
    with eng.connect() as conn:
        present = {
            str(row[0])
            for row in conn.execute(
                sa.text("SELECT table_name FROM information_schema.tables WHERE table_name = ANY(:names)"),
                {"names": list(_P3_TABLES)},
            )
        }
    missing = set(_P3_TABLES) - present
    if missing:
        pytest.skip(f"P3 schema not applied (missing {sorted(missing)}); run alembic upgrade head")
    yield eng
    eng.dispose()


def _irreversible_intent() -> Any:
    from tce_shared.effect_journal import EffectIntent

    return EffectIntent(
        kind="commit",
        capability="git.write",
        resource="refs/heads/main",
        argv=("/opt/homebrew/bin/git", "push"),
        reversibility="irreversible",
        description="push to main",
    )


def _seed_stale_directive_with_open_effect(
    db: Any,
    *,
    workspace: str,
    owner: str,
    session_id: str,
    directive_id: uuid.UUID,
    effect_id: uuid.UUID,
    stale: datetime,
) -> None:
    import sqlalchemy as sa
    from tce_shared.effect_journal import effect_intent_digest

    intent = _irreversible_intent()
    db.execute(
        sa.text(
            """
            INSERT INTO directive_executions(
              directive_id, session_id, workspace_id, user_id, action_kind, attempt, state,
              requires_permit, meta, created_at, updated_at, lease_generation, claimed_executor,
              verification_state
            ) VALUES (
              :directive_id, :session_id, :workspace_id, :user_id, 'takeover_step', 1,
              'in_progress', FALSE, CAST('{}' AS jsonb), :created_at, :updated_at, 1,
              'stale-worker', 'unverified'
            )
            """
        ),
        {
            "directive_id": directive_id,
            "session_id": session_id,
            "workspace_id": workspace,
            "user_id": owner,
            "created_at": stale,
            "updated_at": stale,
        },
    )
    db.execute(
        sa.text(
            """
            INSERT INTO effect_journal(
              effect_id, workspace_id, owner_id, session_id, directive_id, seq, state, kind,
              reversibility, capability, resource, argv_json, description, intent_digest,
              enforcement_tier, action_tracing, lease_generation, claimed_executor,
              opened_at, evidence_json, created_at
            ) VALUES (
              :effect_id, :workspace_id, :owner_id, :session_id, :directive_id, 1, 'running',
              :kind, :reversibility, :capability, :resource, CAST(:argv AS jsonb), :description,
              :digest, 'os_sandbox', 'unavailable', 1, 'stale-worker', :opened_at,
              CAST('{}' AS jsonb), :opened_at
            )
            """
        ),
        {
            "effect_id": effect_id,
            "workspace_id": workspace,
            "owner_id": owner,
            "session_id": session_id,
            "directive_id": directive_id,
            "kind": intent.kind,
            "reversibility": intent.reversibility,
            "capability": intent.capability,
            "resource": intent.resource,
            "argv": json.dumps(list(intent.argv)),
            "description": intent.description,
            "digest": effect_intent_digest(intent),
            "opened_at": stale,
        },
    )


def _cleanup(db: Any, workspace: str) -> None:
    import sqlalchemy as sa

    db.rollback()
    db.execute(sa.text("DELETE FROM effect_journal WHERE workspace_id = :w"), {"w": workspace})
    db.execute(sa.text("DELETE FROM directive_executions WHERE workspace_id = :w"), {"w": workspace})
    db.execute(sa.text("DELETE FROM takeover_sessions WHERE workspace_id = :w"), {"w": workspace})
    db.commit()


def test_takeover_step_that_reaps_a_stale_directive_pauses_in_the_same_turn(engine) -> None:
    """The pause must fire on the turn that reaps, not only after a restart.

    ``POST /v1/takeover/step`` is where the reap actually happens in production
    (``_load_pending_directive``) and it is also where work restarts (the mint below the pause
    guard).  Both are in one request, so resolving the journal only at startup left a live window
    in which the reaper abandoned the directive and the very same turn started the work again.
    """
    import sqlalchemy as sa
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from tce_api.config import get_settings
    from tce_api.main import app
    from tce_api.reconcile import PAUSE_TEXT_PREFIX
    from tce_api.takeover_store import save_takeover_state
    from tce_shared.events import TakeoverMode, TakeoverState

    settings = get_settings()
    # Charter enforcement is off so that "no replacement directive was minted" can only be the
    # pause talking.  With it on, a missing charter would refuse the mint for an unrelated reason
    # and the assertion would pass even with the pause broken.  tests/conftest.py restores the
    # settings singleton after the test.
    settings.charter_enforcement_enabled = False
    assert bool(settings.effect_journal_enabled), "the journal kill switch must be on for this gate"
    assert bool(settings.effect_unknown_pause_enabled), "the pause kill switch must be on for this gate"

    token = next(iter(settings.token_set))
    workspace = f"reapstep-{uuid.uuid4().hex[:8]}"
    owner = "reapstep-owner"
    session_id = f"session-{uuid.uuid4().hex[:8]}"
    directive_id = uuid.uuid4()
    effect_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    stale = now - timedelta(seconds=max(600, settings.takeover_execution_claim_ttl_seconds * 6))

    with Session(engine) as db:
        try:
            save_takeover_state(
                db,
                TakeoverState(
                    session_id=session_id,
                    workspace_id=workspace,
                    user_id=owner,
                    active=True,
                    mode=TakeoverMode.TAKEOVER,
                    persona_mode="shadow",
                    expires_at=now + timedelta(minutes=60),
                    activated_at=now,
                    last_message_at=now,
                    takeover_context={"objective": "finish the deploy"},
                    updated_at=now,
                ),
            )
            _seed_stale_directive_with_open_effect(
                db,
                workspace=workspace,
                owner=owner,
                session_id=session_id,
                directive_id=directive_id,
                effect_id=effect_id,
                stale=stale,
            )
            db.commit()

            before = db.execute(
                sa.text("SELECT count(*) FROM directive_executions WHERE session_id = :s"),
                {"s": session_id},
            ).scalar()
            assert int(before or 0) == 1

            # No `with TestClient(app)`: the context manager runs the lifespan, and the lifespan
            # runs startup_reconcile — which would reap the row before the step ever saw it and
            # turn this back into the restart-path test.
            response = TestClient(app).post(
                "/v1/takeover/step",
                json={
                    "message": "continue with the deploy",
                    "session_id": session_id,
                    "persona_mode": "shadow",
                    "activation_mode_default": "takeover",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-TCE-Consumer": "reapstep-caller",
                    "X-TCE-Role": "user",
                    "X-TCE-Workspace": workspace,
                    "X-TCE-User": owner,
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()

            # The turn that reaps says so. The response text lives in `note` under the P3 note
            # strategy (final_response is held back from the executor); accept either carrier,
            # but one of them must be the pause.
            spoken = str(payload.get("final_response") or payload.get("note") or "")
            assert spoken.startswith(PAUSE_TEXT_PREFIX), payload
            assert str(effect_id) in spoken, "the pause must name the effect that has to be resolved"
            assert payload["takeover_enforcement"].get("autonomy_pause") == "unresolved_irreversible_effect", payload
            assert payload["decision_source"] == "safety_gate", payload

            # And it does not hand back fresh work.
            assert payload["directive_id"] is None, payload
            assert payload["retry_scheduled"] is False, payload

            db.rollback()
            directive = db.execute(
                sa.text("SELECT state, lease_generation FROM directive_executions WHERE directive_id = :id"),
                {"id": directive_id},
            ).mappings().one()
            assert str(directive["state"]) == "abandoned"
            assert int(directive["lease_generation"]) == 2, "the reap must bump the lease so a returning worker is fenced out"

            effect = db.execute(
                sa.text("SELECT state, resolution_source, resolved_by_actor FROM effect_journal WHERE effect_id = :id"),
                {"id": effect_id},
            ).mappings().one()
            assert str(effect["state"]) == "unknown", "an irreversible effect must not be laundered into 'failed'"
            assert str(effect["resolution_source"]) == "reaper"
            assert str(effect["resolved_by_actor"]) == "system:reconciler"

            pending = db.execute(
                sa.text("SELECT count(*) FROM directive_executions WHERE session_id = :s AND state = 'pending'"),
                {"s": session_id},
            ).scalar()
            assert int(pending or 0) == 0, "the paused turn must not mint replacement work"
            after = db.execute(
                sa.text("SELECT count(*) FROM directive_executions WHERE session_id = :s"),
                {"s": session_id},
            ).scalar()
            assert int(after or 0) == int(before or 0), "the directive count must be unchanged across the step"
        finally:
            _cleanup(db, workspace)


def test_startup_reconcile_resolves_the_same_way_after_a_restart(engine) -> None:
    """The restart path. Same resolution function as the in-turn reaper, so the two cannot drift."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from tce_api.config import get_settings
    from tce_api.reconcile import reset_for_tests, startup_reconcile

    settings = get_settings()
    workspace = f"reconcile-{uuid.uuid4().hex[:8]}"
    owner = "reconcile-owner"
    session_id = f"session-{uuid.uuid4().hex[:8]}"
    directive_id = uuid.uuid4()
    effect_id = uuid.uuid4()
    stale = datetime.now(tz=UTC) - timedelta(seconds=max(600, settings.takeover_execution_claim_ttl_seconds * 6))

    with Session(engine) as db:
        _seed_stale_directive_with_open_effect(
            db,
            workspace=workspace,
            owner=owner,
            session_id=session_id,
            directive_id=directive_id,
            effect_id=effect_id,
            stale=stale,
        )
        db.commit()

        try:
            reset_for_tests()
            counts = startup_reconcile(db, settings=settings)
            assert counts["scanned"] >= 1

            directive = db.execute(
                sa.text("SELECT state, lease_generation FROM directive_executions WHERE directive_id = :id"),
                {"id": directive_id},
            ).mappings().one()
            assert str(directive["state"]) == "abandoned"
            assert int(directive["lease_generation"]) == 2, "the reap must bump the lease so a returning worker is fenced out"

            effect = db.execute(
                sa.text("SELECT state, resolution_source, resolved_by_actor FROM effect_journal WHERE effect_id = :id"),
                {"id": effect_id},
            ).mappings().one()
            assert str(effect["state"]) == "unknown", "an irreversible effect must not be laundered into 'failed'"
            assert str(effect["resolution_source"]) == "reaper"
            assert str(effect["resolved_by_actor"]) == "system:reconciler"

            pending = db.execute(
                sa.text(
                    "SELECT count(*) FROM directive_executions WHERE session_id = :s AND state = 'pending'"
                ),
                {"s": session_id},
            ).scalar()
            assert int(pending or 0) == 0, "the reconcile must not mint replacement work"
        finally:
            _cleanup(db, workspace)


def test_pause_guard_reads_the_unknown_effect(engine) -> None:
    """The guard is what stops takeover_step minting a fresh directive after a reap."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from tce_api.config import get_settings
    from tce_api.reconcile import pause_guard_for_session
    from tce_shared.effect_journal import EffectIntent, effect_intent_digest

    settings = get_settings()
    workspace = f"pause-{uuid.uuid4().hex[:8]}"
    owner = "pause-owner"
    session_id = f"session-{uuid.uuid4().hex[:8]}"
    directive_id = uuid.uuid4()
    effect_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    intent = EffectIntent(
        kind="command",
        capability="process.execute",
        resource="deploy.sh",
        argv=("/bin/sh", "deploy.sh"),
        reversibility="unknown",
        description="deploy",
    )
    with Session(engine) as db:
        try:
            db.execute(
                sa.text(
                    """
                    INSERT INTO effect_journal(
                      effect_id, workspace_id, owner_id, session_id, directive_id, seq, state, kind,
                      reversibility, capability, resource, argv_json, description, intent_digest,
                      enforcement_tier, action_tracing, lease_generation, claimed_executor,
                      opened_at, evidence_json, created_at
                    ) VALUES (
                      :effect_id, :workspace_id, :owner_id, :session_id, :directive_id, 1, 'unknown',
                      :kind, :reversibility, :capability, :resource, CAST(:argv AS jsonb), :description,
                      :digest, 'os_sandbox', 'unavailable', 1, 'stale-worker', :now,
                      CAST('{}' AS jsonb), :now
                    )
                    """
                ),
                {
                    "effect_id": effect_id,
                    "workspace_id": workspace,
                    "owner_id": owner,
                    "session_id": session_id,
                    "directive_id": directive_id,
                    "kind": intent.kind,
                    "reversibility": intent.reversibility,
                    "capability": intent.capability,
                    "resource": intent.resource,
                    "argv": json.dumps(list(intent.argv)),
                    "description": intent.description,
                    "digest": effect_intent_digest(intent),
                    "now": now,
                },
            )
            db.commit()
            paused, reason, paused_effect = pause_guard_for_session(
                db, workspace_id=workspace, owner_id=owner, session_id=session_id, settings=settings
            )
            assert paused is True
            assert reason == "unresolved_irreversible_effect"
            assert paused_effect == str(effect_id)
        finally:
            _cleanup(db, workspace)


def test_a_header_asserted_verifier_cannot_bury_an_irreversible_effect(engine) -> None:
    """F1's fourth door, and the burial G2 exists to refuse.

    ``validate_effect_transition`` rule 5 refuses either terminal state on an irreversible effect
    for a non-system actor, and ``resolve_effect``'s fence exempts a system actor from the
    ``claimed_executor`` comparison.  Both ask only "is this actor a system source?", so the whole
    of G2 rests on how ``POST /v1/effects/{id}/resolve`` picks the actor.  It picked it with
    ``_runner_principal_for(auth)``, which on the compat path is ``X-TCE-Consumer`` with the
    credential-kind prefix stripped — a header.

    Measured against the live Postgres before the fix: asserting ``X-TCE-Consumer: system:verifier``
    on an ordinary bearer moved a ``running`` irreversible ``git push`` to ``failed`` (HTTP 200,
    ``resolved_by_actor='system:verifier'``) while the honest executor got
    ``409 irreversible_actor``.  ``failed`` is terminal and is reopened only by a system actor, so
    the reap that followed found nothing open, the pause never fired, and the same
    ``POST /v1/takeover/step`` minted replacement work over the unresolved push.

    The last case is the non-vacuity half: a genuinely server-bound verifier still gets the
    promotion, so this test pins WHERE the identity comes from, not that the branch is dead.
    """
    import sqlalchemy as sa
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from tce_api.config import get_settings
    from tce_api.main import app
    from tce_shared.identity import credential_fingerprint

    settings = get_settings()
    settings.identity_claims_mode = "compat"
    settings.identity_claims_json = "{}"
    settings.workspace_access_mode = "compat"
    token = next(iter(settings.token_set))
    runner = str(settings.verification_runner_principal)

    workspace = f"bury-{uuid.uuid4().hex[:8]}"
    owner = "bury-owner"
    session_id = f"session-{uuid.uuid4().hex[:8]}"
    directive_id = uuid.uuid4()
    effect_id = uuid.uuid4()
    now = datetime.now(tz=UTC)

    def _resolve(consumer: str, target: str) -> Any:
        return TestClient(app).post(
            f"/v1/effects/{effect_id}/resolve",
            json={"target_state": target, "resolution_source": "provider_read", "expected_lease": 1},
            headers={
                "Authorization": f"Bearer {token}",
                "X-TCE-Consumer": consumer,
                "X-TCE-Role": "executor",
                "X-TCE-Workspace": workspace,
                "X-TCE-User": owner,
            },
        )

    def _row() -> Any:
        return db.execute(
            sa.text("SELECT state, resolved_by_actor FROM effect_journal WHERE effect_id = :id"),
            {"id": effect_id},
        ).mappings().one()

    with Session(engine) as db:
        try:
            _seed_stale_directive_with_open_effect(
                db,
                workspace=workspace,
                owner=owner,
                session_id=session_id,
                directive_id=directive_id,
                effect_id=effect_id,
                stale=now,
            )
            db.commit()

            # The honest executor is refused by rule 5 — the control the attack must match.
            honest = _resolve("honest-worker", "failed")
            assert honest.status_code == 409, honest.text
            assert honest.json()["detail"]["reason"] == "irreversible_actor", honest.text

            # The attack: same token, same role, one header changed.  Both terminal states.
            for target in ("failed", "confirmed"):
                attack = _resolve(runner, target)
                assert attack.status_code == 409, f"{target}: {attack.text}"
                assert attack.json()["detail"]["reason"] == "irreversible_actor", attack.text
                db.rollback()
                assert str(_row()["state"]) == "running", f"{target} laundered an irreversible effect closed"
                assert _row()["resolved_by_actor"] is None

            # Non-vacuity: bind the SAME token to the runner principal as a server claim, and the
            # promotion to EFFECT_ACTOR_VERIFIER is restored.  The refusals above are about the
            # source of the identity, not about the branch being unreachable.
            settings.identity_claims_json = json.dumps(
                {
                    credential_fingerprint("bearer", token): {
                        "consumer": runner,
                        "role": "user",
                        "workspace_id": workspace,
                        "user_id": owner,
                    }
                }
            )
            bound = _resolve(runner, "failed")
            assert bound.status_code == 200, bound.text
            db.rollback()
            assert str(_row()["state"]) == "failed"
            assert str(_row()["resolved_by_actor"]) == "system:verifier"
        finally:
            _cleanup(db, workspace)


def test_the_row_cap_cannot_hide_an_unknown_effect_from_the_pause(engine) -> None:
    """The pause must depend on the table, not on how many rows happen to precede the unknown one.

    ``effect_store.list_open_effects`` caps its window at 200 rows, and
    ``pause_guard_for_session`` decides the autonomy pause from exactly that window.  Ordered by
    ``opened_at`` alone, 200 open rows opened before the ``unknown`` one fill the window and the
    pause silently does not fire — measured against the live Postgres as
    ``pause_guard_for_session -> (False, '', '')`` with the unknown row still sitting in the table.

    The window is agent-reachable: nothing forces a ``prepared`` effect to be resolved, so opening
    cheap effects early and the risky one afterwards produces precisely that ordering.  Lite's twin
    has no cap, so this is a Full-only clause and there is no Lite parity test to add.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from tce_api.config import get_settings
    from tce_api.effect_store import list_open_effects
    from tce_api.reconcile import pause_guard_for_session
    from tce_shared.effect_journal import EffectIntent, effect_intent_digest

    settings = get_settings()
    workspace = f"rowcap-{uuid.uuid4().hex[:8]}"
    owner = "rowcap-owner"
    session_id = f"session-{uuid.uuid4().hex[:8]}"
    directive_id = uuid.uuid4()
    unknown_id = uuid.uuid4()
    base = datetime.now(tz=UTC) - timedelta(hours=5)

    def _insert(
        db: Any,
        effect_id: uuid.UUID,
        seq: int,
        state: str,
        reversibility: Literal["reversible", "irreversible", "unknown"],
        when: datetime,
    ) -> None:
        intent = EffectIntent(
            kind="write",
            capability="filesystem.write",
            resource=f"noise-{seq}.txt",
            argv=("write", f"noise-{seq}"),
            reversibility=reversibility,
            description="filler",
        )
        db.execute(
            sa.text(
                """
                INSERT INTO effect_journal(
                  effect_id, workspace_id, owner_id, session_id, directive_id, seq, state, kind,
                  reversibility, capability, resource, argv_json, description, intent_digest,
                  enforcement_tier, action_tracing, lease_generation, claimed_executor, opened_at,
                  evidence_json, created_at
                ) VALUES (
                  :effect_id, :workspace_id, :owner_id, :session_id, :directive_id, :seq, :state,
                  'write', :reversibility, :capability, :resource, CAST(:argv AS jsonb),
                  :description, :digest, 'os_sandbox', 'unavailable', 1, 'filler-worker', :opened_at,
                  CAST('{}' AS jsonb), :opened_at
                )
                """
            ),
            {
                "effect_id": effect_id,
                "workspace_id": workspace,
                "owner_id": owner,
                "session_id": session_id,
                "directive_id": directive_id,
                "seq": seq,
                "state": state,
                "reversibility": reversibility,
                "capability": intent.capability,
                "resource": intent.resource,
                "argv": json.dumps(list(intent.argv)),
                "description": intent.description,
                "digest": effect_intent_digest(intent),
                "opened_at": when,
            },
        )

    with Session(engine) as db:
        try:
            db.execute(
                sa.text(
                    """
                    INSERT INTO directive_executions(
                      directive_id, session_id, workspace_id, user_id, action_kind, attempt, state,
                      requires_permit, meta, created_at, updated_at, lease_generation,
                      claimed_executor, verification_state
                    ) VALUES (
                      :directive_id, :session_id, :workspace_id, :user_id, 'takeover_step', 1,
                      'in_progress', FALSE, CAST('{}' AS jsonb), :t, :t, 1, 'filler-worker',
                      'unverified'
                    )
                    """
                ),
                {"directive_id": directive_id, "session_id": session_id, "workspace_id": workspace,
                 "user_id": owner, "t": base},
            )
            # Exactly enough older open rows to fill the cap on their own ...
            for i in range(200):
                _insert(db, uuid.uuid4(), i + 1, "running", "reversible", base + timedelta(seconds=i))
            # ... and the row the pause exists for, opened after every one of them.
            _insert(db, unknown_id, 999, "unknown", "irreversible", base + timedelta(seconds=500))
            db.commit()

            records = list_open_effects(db, workspace_id=workspace, owner_id=owner, session_id=session_id)
            assert len(records) == 200, "the cap itself must still hold"
            assert any(record.state == "unknown" for record in records), (
                "the unknown row fell outside the capped window, so the pause cannot see it"
            )

            paused, reason, effect_id = pause_guard_for_session(
                db, workspace_id=workspace, owner_id=owner, session_id=session_id, settings=settings
            )
            assert paused is True, "200 healthy in-flight rows must not suppress the pause"
            assert reason == "unresolved_irreversible_effect"
            assert effect_id == str(unknown_id)
        finally:
            _cleanup(db, workspace)
