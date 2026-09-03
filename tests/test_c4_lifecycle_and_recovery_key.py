"""C4's verification lifecycle, and the recovery key that makes Prove possible.

TWO THINGS, ONE PR, because neither is useful alone. C4 gives a fix a state
that means "prove me"; the recovery key gives the proof something to attach to.

The key's whole job is to be the join commerce_interactions never had. That
table records a complete agent loop on one row — prompt, click, cart, order —
but carried no reference to the finding or action that preceded it, so a
conversion could be observed and never attributed. Everything below is about
the two properties that make such a join trustworthy: it must be STABLE across
re-audits, and it must AGREE with the dedupe rule it is derived from.
"""

from __future__ import annotations

import pytest

import db.merchant_tasks as mt


# ---- C4: the verification lifecycle ----------------------------------------

def test_the_four_verification_states_exist():
    for status in ("ready_for_retest", "verifying", "verified", "regressed"):
        assert status in mt.VALID_STATUSES, status


def test_done_and_verified_are_different_things():
    """"done" is the merchant saying they changed something. "verified" is a
    replay saying it worked. Collapsing them would make Prove unfalsifiable —
    every fix would prove itself the moment it was marked done."""
    assert "done" in mt.VALID_STATUSES and "verified" in mt.VALID_STATUSES
    assert "done" != "verified"


def test_verified_is_terminal_and_regressed_is_not():
    """A verified fix is finished. A regressed one is actionable AGAIN, and
    stamping it complete would bury the exact case Prove exists to surface."""
    assert "verified" in mt.TERMINAL_STATUSES
    assert "regressed" not in mt.TERMINAL_STATUSES


def test_the_in_flight_states_are_not_terminal():
    for status in ("ready_for_retest", "verifying"):
        assert status not in mt.TERMINAL_STATUSES, status


def test_the_pre_existing_terminal_set_is_unchanged():
    """The lifecycle is additive: nothing that used to complete a task stopped
    completing it."""
    for status in ("done", "dismissed", "failed"):
        assert status in mt.TERMINAL_STATUSES, status


# ---- the recovery key ------------------------------------------------------

def _key(**over):
    base = dict(
        merchant_id="m-1", lever="content",
        target_host="anua.com", product_key="anua-niacinamide-serum",
    )
    base.update(over)
    return mt.recovery_key_for(**base)


def test_the_same_gap_yields_the_same_key():
    """THE property the design rests on. A gap found again next week must
    produce the same key, or the clock between shipping a fix and an order
    landing resets at every audit and nothing can ever be attributed."""
    assert _key() == _key()


def test_the_key_does_NOT_depend_on_the_rendered_title():
    """The defect the first cut shipped. Titles are rendered per run and
    splice in run-variable data — real examples from the tree:

        f"Reach out to {n} matched creator{'s' if n != 1 else ''}"
        f"{len(briefs)} content brief(s) drafted for failed category queries"
        f"When buyers ask for alternatives, AI names {substitute}, not {sku}."

    A count that ticks from 3 to 4, or a competitor the model names
    differently this run, changed the key — so a fix shipped in week 1 and an
    order in week 3 could never match and the join silently returned nothing.
    The key takes no title at all now, which is why this test can only be
    written as a signature assertion.
    """
    import inspect
    params = inspect.signature(mt.recovery_key_for).parameters
    assert "title" not in params, (
        "the key must not depend on a rendered title — they drift per run"
    )
    assert set(params) >= {"merchant_id", "lever", "target_host", "product_key"}


def test_the_key_is_insensitive_to_casing_and_whitespace():
    # merchant_id included: ids are system-generated so casing is consistent
    # today, but an unnormalized component is a silent way for one merchant's
    # key to fork in two — and nothing else would notice.
    assert _key(merchant_id="  M-1 ") == _key()
    assert _key(target_host="  ANUA.com ") == _key()
    assert _key(product_key="ANUA-Niacinamide-Serum") == _key()
    assert _key(lever="Content") == _key()


def test_two_merchants_with_identical_gaps_never_collide():
    """Without merchant scoping, one merchant's order would attribute to
    another's fix — the worst failure available to this join."""
    assert _key(merchant_id="m-1") != _key(merchant_id="m-2")


def test_a_different_gap_yields_a_different_key():
    """The positive counterpart: a key ignoring its inputs would pass every
    stability test above."""
    assert _key(lever="outreach") != _key()
    assert _key(target_host="other.com") != _key()
    assert _key(product_key="another-sku") != _key()


def test_the_key_is_opaque_and_fixed_width():
    """It rides on a published link. The digest is what keeps the merchant id,
    the host and the product out of it — asserting "the plaintext is absent"
    would be tautological for any hash, so this asserts the SHAPE instead."""
    key = _key()
    assert key.startswith("rk_")
    assert len(key) == 35
    assert all(c in "0123456789abcdef" for c in key[3:])


def test_the_gap_identity_is_NOT_the_dedupe_identity():
    """Two rules, deliberately. canonical_action_identity answers "is this the
    same TASK ROW" for list-time dedupe and must keep using the title, or it
    would collapse genuinely distinct tasks. recovery_identity answers "is
    this the same GAP". Merging them is what produced a key nobody could rely
    on."""
    assert "title" in inspect_params(mt.canonical_action_identity)
    assert "title" not in inspect_params(mt.recovery_identity)


def inspect_params(fn):
    import inspect
    return set(inspect.signature(fn).parameters)


def test_missing_inputs_do_not_explode():
    assert mt.recovery_key_for(merchant_id="m-1", lever=None).startswith("rk_")
    assert mt.recovery_key_for(
        merchant_id="m-1", lever="content",
        target_host=None, product_key=None).startswith("rk_")


# ---- the key is actually WRITTEN -------------------------------------------

async def test_a_created_task_carries_its_recovery_key(monkeypatch):
    """Without this the migration adds two columns that stay 100% NULL — the
    review's finding, and the difference between a join and a dead schema."""
    captured = {}

    class _Capture:
        async def execute(self, query):
            captured["values"] = dict(query.compile().params)
            return None

    monkeypatch.setattr(mt, "database", _Capture())
    monkeypatch.setattr(mt, "_DDL_READY", True)
    task_id = await mt.record_task_created(
        merchant_id="m-1", title="Reach out to 3 matched creators",
        lever="outreach",
        evidence={"target_host": "anua.com", "product_key": "sku-1"},
    )
    assert task_id
    assert captured["values"]["recovery_key"] == mt.recovery_key_for(
        merchant_id="m-1", lever="outreach",
        target_host="anua.com", product_key="sku-1",
    )


async def test_the_written_key_survives_a_title_change(monkeypatch):
    """The end-to-end version of the property: the SAME gap written twice with
    the run-variable titles the tree actually produces must carry one key."""
    seen = []

    class _Capture:
        async def execute(self, query):
            seen.append(dict(query.compile().params)["recovery_key"])
            return None

    monkeypatch.setattr(mt, "database", _Capture())
    monkeypatch.setattr(mt, "_DDL_READY", True)
    ev = {"target_host": "anua.com", "product_key": "sku-1"}
    await mt.record_task_created(
        merchant_id="m-1", title="Reach out to 3 matched creators",
        lever="outreach", evidence=ev)
    await mt.record_task_created(
        merchant_id="m-1", title="Reach out to 4 matched creators",
        lever="outreach", evidence=ev)
    assert seen[0] == seen[1], "a count in the title must not move the key"




async def test_marking_a_task_verified_actually_completes_it(monkeypatch):
    """M1 from review: every C4 test inspected the frozenset and none drove
    the WRITE, so reverting update_task_status to the old literal — leaving
    `verified` without completed_at — survived the whole suite. This is the
    delivering line."""
    captured = {}

    class _Capture:
        async def execute(self, query):
            captured["values"] = dict(query.compile().params)
            return None

    monkeypatch.setattr(mt, "database", _Capture())
    monkeypatch.setattr(mt, "_DDL_READY", True)
    assert await mt.update_task_status(task_id="t-1", status="verified") is True
    assert captured["values"].get("completed_at") is not None


async def test_marking_a_task_regressed_does_NOT_complete_it(monkeypatch):
    """The counterpart, and the reason regressed is excluded: a gap that
    re-opened is actionable again, and stamping completed_at would bury it."""
    captured = {}

    class _Capture:
        async def execute(self, query):
            captured["values"] = dict(query.compile().params)
            return None

    monkeypatch.setattr(mt, "database", _Capture())
    monkeypatch.setattr(mt, "_DDL_READY", True)
    assert await mt.update_task_status(task_id="t-1", status="regressed") is True
    assert captured["values"].get("completed_at") is None


def test_the_in_flight_states_stay_in_the_merchants_open_queue():
    """list_tasks_for_merchant's default is the OPEN queue. Omitting the
    verification states would make a task vanish the moment it became ready to
    prove — open forever, with nothing showing it."""
    import inspect
    src = inspect.getsource(mt.list_tasks_for_merchant)
    for status in ("ready_for_retest", "verifying", "regressed"):
        assert status in src, status
    # ...and the terminal ones stay out of it.
    assert '"verified"' not in src.split("status_filter = [")[1].split("]")[0]
