"""Permuted-block allocation: exact within a block, stratified, and reproducible.

The properties asserted here are the reason P6 does not hash an identifier into an arm.  A hash
is balanced only in expectation, and a caller who can vary any component of the hashed key can
draw again; a block counter is balanced exactly, and the only input a caller controls is the work
itself.  A complete block is also a *matched set*, which is where ``claim_a``'s paired test gets
its pairs from without a second mechanism.
"""

from __future__ import annotations

import pytest
from tce_shared.pilot_enrollment import (
    ARM_SET_SHA,
    RANDOMIZED_ARMS,
    AllocationKind,
    PilotArm,
    allocate_arm,
    allocation_salt_sha256,
    block_permutation,
    elect_arm,
    stratum_id_for,
)

SALT = "tce-pilot-p6-v1"
WIDTH = len(RANDOMIZED_ARMS)


def _stratum(index: int, family: str = "needs_human") -> str:
    return stratum_id_for(
        workspace_id="personal",
        subject_user_id=f"subject-{index}",
        project_id=f"proj-{index}",
        decision_family=family,
    )


def test_blocks_are_exact() -> None:
    """Over 2000 simulated strata, every complete block holds each arm exactly once."""

    for index in range(2000):
        stratum = _stratum(index)
        draws = [allocate_arm(stratum_id=stratum, slot=slot, salt=SALT).arm_id for slot in range(WIDTH * 3)]
        for block in range(3):
            window = draws[block * WIDTH : (block + 1) * WIDTH]
            assert sorted(window) == sorted(RANDOMIZED_ARMS), f"stratum {index} block {block}: {window}"


def test_arm_counts_never_differ_by_more_than_one() -> None:
    """At ANY moment — not only on a block boundary — a cell's arms differ by at most one."""

    stratum = _stratum(7)
    counts: dict[PilotArm, int] = dict.fromkeys(RANDOMIZED_ARMS, 0)
    for slot in range(200):
        counts[allocate_arm(stratum_id=stratum, slot=slot, salt=SALT).arm_id] += 1
        assert max(counts.values()) - min(counts.values()) <= 1, (slot, counts)


def test_strata_do_not_mix() -> None:
    """Two decision families never share a block: they do not even share a stratum id."""

    safety = _stratum(1, "safety_confirmation")
    needs_human = _stratum(1, "needs_human")
    assert safety != needs_human
    # and their block streams are drawn independently
    differed = any(
        block_permutation(stratum_id=safety, block_ordinal=block, salt=SALT)
        != block_permutation(stratum_id=needs_human, block_ordinal=block, salt=SALT)
        for block in range(40)
    )
    assert differed


def test_allocation_is_reproducible_from_salt_and_slot() -> None:
    stratum = _stratum(3)
    first = [allocate_arm(stratum_id=stratum, slot=slot, salt=SALT) for slot in range(30)]
    second = [allocate_arm(stratum_id=stratum, slot=slot, salt=SALT) for slot in range(30)]
    assert [item.arm_id for item in first] == [item.arm_id for item in second]
    assert {item.allocation_salt_sha256 for item in first} == {allocation_salt_sha256(SALT)}
    assert {item.arm_set_sha for item in first} == {ARM_SET_SHA}


def test_a_different_salt_draws_a_different_sequence() -> None:
    stratum = _stratum(4)
    mine = [allocate_arm(stratum_id=stratum, slot=slot, salt=SALT).arm_id for slot in range(60)]
    theirs = [allocate_arm(stratum_id=stratum, slot=slot, salt="some-other-salt").arm_id for slot in range(60)]
    assert mine != theirs
    assert allocation_salt_sha256(SALT) != allocation_salt_sha256("some-other-salt")


def test_the_permutation_is_not_degenerate() -> None:
    """All six orderings of three arms are reachable, so the block order is not a fixed cycle."""

    stratum = _stratum(5)
    seen = {block_permutation(stratum_id=stratum, block_ordinal=block, salt=SALT) for block in range(400)}
    assert len(seen) == 6


def test_block_ordinal_and_position_track_the_slot() -> None:
    stratum = _stratum(6)
    for slot in range(20):
        allocation = allocate_arm(stratum_id=stratum, slot=slot, salt=SALT)
        assert allocation.block_ordinal == slot // WIDTH
        assert allocation.block_position == slot % WIDTH
        assert allocation.allocation_kind is AllocationKind.RANDOMIZED
        assert allocation.arm_class == "runtime"


def test_a_negative_slot_is_refused() -> None:
    with pytest.raises(ValueError, match="slot must be >= 0"):
        allocate_arm(stratum_id=_stratum(8), slot=-1, salt=SALT)


def test_an_elected_arm_consumes_no_slot_and_is_never_randomized() -> None:
    """An elected arm must not perturb the randomised blocks it sits beside."""

    allocation = elect_arm(stratum_id=_stratum(9), arm=PilotArm.OWNER_UNASSISTED, salt=SALT)
    assert allocation.allocation_kind is AllocationKind.ELECTED
    assert allocation.arm_class == "human_workflow"
    assert allocation.slot == -1 and allocation.block_ordinal == -1 and allocation.block_position == -1


def test_a_randomized_arm_cannot_be_elected() -> None:
    with pytest.raises(ValueError, match="randomised"):
        elect_arm(stratum_id=_stratum(10), arm=PilotArm.TCE_ASSISTED, salt=SALT)
