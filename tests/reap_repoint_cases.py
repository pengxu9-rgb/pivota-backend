"""WP4c — revoke on repoint. The cases, written ONCE and collected under BOTH dialects.

NOT A TEST MODULE ITSELF (the name does not match `test_*.py`, so pytest never collects it
directly). Two thin modules collect it, the same shape tests/reap_cart_link_cases.py established:

    tests/test_reap_buyer_link_repoint.py            SQLite — skips itself under Postgres
    tests/test_reap_buyer_link_repoint_postgres.py   Postgres — skips itself under SQLite, and is
                                                     what .github/workflows/
                                                     postgres-dialect-gate.yml picks up by glob

WHAT IS UNDER TEST. `routes/buyer_api._upsert_buyer_identity_link` REPOINTS an
`(agent_id, hash(agent_user_ref))` link from one buyer id to another. When the buyer id it moved
off was one `routes/agent_commerce_reap._buyer_id_for` minted (`u_<16hex>`), everything the Reap
rail hangs off that id — its `reap_agentic_buyer_refs` row and every enrollment on that row's
`reap_buyer_ref` — becomes unreachable, and the enrollment stays `active` at Reap holding a card.
WP4c retires that state at the moment of the repoint instead of leaving it for an operator sweep.

WHAT IS REAL HERE: `_upsert_buyer_identity_link` itself (called DIRECTLY — it is module-level, and
neither collector imports `main`, because a Postgres gate module that imports `main` poisons the
whole gate's `create_all`), the ledger, `hash_agent_user_ref`, and the schema, built by the
SELF-HEAL on both engines because production skips db/migrations and the self-heal is what ships.

WHAT IS FAKE: nothing, and that is the point — there is no partner call anywhere on this path and
the autouse `no_network` fixture is the control that proves it. An `httpx.AsyncClient` constructed
under any case here raises.

NEVER `TestClient`. It hangs against the asyncpg pool; this file does not build an app at all.

FIXTURE NAMES HAVE NO LEADING UNDERSCORE ON PURPOSE: the collectors `import *`, and `*` skips
underscored names.

EVERY CASE BELOW WAS MUTATED ONE AT A TIME AND CONFIRMED TO KILL AT LEAST ONE TEST; the table is
in the PR body. The guards are: the hook itself, the `old != new` conjunct, the `if not old`
conjunct, the enrollment sweep inside the retire, the refs DELETE, the remaining-links guard, the
try/except around the hook, the reason validation, the hook in the FALLBACK branch, and the
absence of ids from the log lines.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from db.database import IS_POSTGRES, database, metadata  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402
import routes.buyer_api as buyer_api  # noqa: E402

#: Captured before `no_network` can replace the attribute, in case a case ever needs a real one.
REAL_ASYNC_CLIENT = httpx.AsyncClient

AGENT = "agent_repoint"
OTHER_AGENT = "agent_repoint_other"
USER_REF = "end-user-ada"


def ref_hash() -> str:
    """The stored hash of `USER_REF`, computed AT CALL TIME and never at import.

    A MODULE CONSTANT HERE WAS ORDER-DEPENDENT, and it failed in a way worth recording.
    `db.buyer_vault.hash_agent_user_ref` reads the ENVIRONMENT on every call —
    `BUYER_IDENTITY_LINK_SECRET`, then three fallbacks, then a bare SHA-256 when none is set — so
    the hash of one string is not one value, it is a value per environment. Several test modules
    set `CHECKOUT_TOKEN_SECRET` at IMPORT (tests/test_buyer_vault_mvp.py is one), and pytest
    imports every collected module before it runs any of them. A constant bound at this module's
    import therefore held the keyed hash or the unkeyed one depending on which files were
    collected alongside it — and the code under test, which hashes during the test, would then
    disagree with the fixture. Standalone: green. In a wider run: every case failed on an id that
    matched nothing.

    Called rather than stored, both sides hash under the same environment and agree by
    construction, whatever that environment is.
    """
    return hash_agent_user_ref(USER_REF)


#: A buyer id in the space `_mint_buyer_id` mints into — `u_` + 16 hex. Written out rather than
#: generated so the assertions can name it, and shaped like the real thing because the code under
#: test must NOT be able to tell the difference: the decision to retire is made from the LINK
#: TABLE, never from the shape of the id.
MINTED_BUYER = "u_0123456789abcdef"
REAL_BUYER = "buy_ada_real"
SECOND_REAL_BUYER = "buy_ada_second"

REAP_REF = "pw_repoint_fixture_ref"
OTHER_REAP_REF = "pw_repoint_fixture_ref_other"
CONSENT = "reap-agentic-v1"

#: Every value that must never reach a log line from this hook. The `reap_buyer_ref` is on the
#: list although it is opaque: it is the identity a PARTNER also holds, so a log that carries it
#: joins our access log to Reap's records.
def secret_strings() -> tuple:
    """Computed per call, for the same reason `ref_hash` is."""
    return (MINTED_BUYER, REAL_BUYER, SECOND_REAL_BUYER, REAP_REF, ref_hash(), USER_REF)

RAIL_TABLES = (
    "reap_agentic_purchase_keys",
    "reap_agentic_buyer_refs",
    "reap_agentic_eligibility",
    "reap_agentic_purchases",
    "reap_agentic_enrollments",
)


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def repoint_db():
    """The rail's tables the way PRODUCTION builds them — the self-heal, on BOTH engines, because
    production never runs db/migrations — and `buyer_identity_links` from the repo's own
    `metadata`, so the columns the upsert writes are the columns the table really has.

    Every dialect-dependent decision is read at CALL time (`IS_POSTGRES`), never at import, which
    is what lets one fixture serve both collectors.
    """
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    cascade = " CASCADE" if IS_POSTGRES else ""
    for table in RAIL_TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}{cascade}")
    await ensure_required_schema_light()

    import sqlalchemy
    from db.buyer_vault import buyer_identity_links

    url = (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(engine, tables=[buyer_identity_links], checkfirst=True)
    engine.dispose()

    await database.execute("DELETE FROM buyer_identity_links")
    try:
        yield
    finally:
        await database.execute("DELETE FROM buyer_identity_links")
        if not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """THE CONTROL FOR "THIS PATH MAKES NO PARTNER CALL". The hosted checkout's save path must
    not reach Reap — that is the whole reason WP4c retires locally instead of revoking inline —
    so an `httpx.AsyncClient` constructed anywhere under a case here raises."""

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "the repoint hook made an outbound HTTP call; it runs on the hosted checkout's "
                "save path and must never reach a partner"
            )

    monkeypatch.setattr(httpx, "AsyncClient", _Forbidden)


@pytest.fixture
def retire_calls(monkeypatch):
    """Records every call the hook makes into the ledger, and then makes the real one.

    A SPY AND NOT AN OBSERVATION OF THE ROWS, because the two mutants that matter here are
    invisible in the rows. "Fires on a first insert" calls `retire_buyer_refs_for_buyer("")`,
    which raises a ValueError the hook swallows — no row changes, and a state-only assertion
    passes. The call list sees it.

    Patched on the LEDGER MODULE, which is where the hook's lazy `from … import` resolves it.
    """
    calls = []
    real = ledger.retire_buyer_refs_for_buyer

    async def spy(buyer_id, *, reason):
        calls.append((buyer_id, reason))
        return await real(buyer_id, reason=reason)

    monkeypatch.setattr(ledger, "retire_buyer_refs_for_buyer", spy)
    return calls


# ── seeding ──────────────────────────────────────────────────────────────────────────────────


async def seed_link(*, agent_id: str, ref_hash: str, buyer_id: str) -> None:
    await database.execute(
        """
        INSERT INTO buyer_identity_links (agent_id, agent_user_ref_hash, buyer_id)
        VALUES (:agent_id, :agent_user_ref_hash, :buyer_id)
        """,
        {"agent_id": agent_id, "agent_user_ref_hash": ref_hash, "buyer_id": buyer_id},
    )


async def seed_buyer_ref(*, buyer_id: str, reap_buyer_ref: str) -> None:
    await database.execute(
        """
        INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref, consent_version)
        VALUES (:buyer_id, :reap_buyer_ref, :consent_version)
        """,
        {
            "buyer_id": buyer_id,
            "reap_buyer_ref": reap_buyer_ref,
            "consent_version": CONSENT,
        },
    )


async def seed_enrollment(*, reap_buyer_ref: str, status: str = "active") -> str:
    """One enrollment on a ref, in `status`. Built through the ledger's own writers so the row is
    the shape production writes, not a hand-assembled INSERT that could outlive a column."""
    # AN EXPLICIT (and deliberately unmatched) id forces the INSERT arm, so every call here is
    # a DISTINCT row. Left to itself `upsert_pending_enrollment` REFRESHES the ref's existing
    # pending row — correct in production, and useless for a fixture that needs a stack of them.
    row = await ledger.upsert_pending_enrollment(
        buyer_ref=reap_buyer_ref,
        agent_id=AGENT,
        hosted_url="https://pay.reap.global/enroll/abc",
        enrollment_id=ledger.new_enrollment_id(),
    )
    enrollment_id = str(row["id"])
    if status == "active":
        await ledger.mark_enrollment_active(
            enrollment_id,
            reap_enrollment_id="00000000-0000-4000-8000-" + enrollment_id[-12:].rjust(12, "0"),
            card_network="VISA",
            card_last4="4242",
        )
    elif status != "pending":
        raise AssertionError(f"unsupported seed status {status!r}")
    return enrollment_id


async def enrollment_row(enrollment_id: str):
    return await database.fetch_one(
        "SELECT id, status, reap_status, hosted_url FROM reap_agentic_enrollments WHERE id = :id",
        {"id": enrollment_id},
    )


async def buyer_ref_count(buyer_id: str) -> int:
    return int(
        await database.fetch_val(
            "SELECT COUNT(*) FROM reap_agentic_buyer_refs WHERE buyer_id = :b",
            {"b": buyer_id},
        )
    )


async def link_buyer_id(*, agent_id: str, ref_hash: str):
    row = await database.fetch_one(
        """
        SELECT buyer_id FROM buyer_identity_links
         WHERE agent_id = :a AND agent_user_ref_hash = :h
        """,
        {"a": agent_id, "h": ref_hash},
    )
    return str(dict(row)["buyer_id"]) if row else None


async def upsert(buyer_id: str, *, agent_id: str = AGENT, user_ref: str = USER_REF):
    """The function under test, called DIRECTLY. It is module-level, so neither collector needs
    an app, a request, or `main`."""
    return await buyer_api._upsert_buyer_identity_link(
        buyer_id=buyer_id, agent_id=agent_id, agent_user_ref=user_ref
    )


# ── the hook: when it fires ──────────────────────────────────────────────────────────────────


async def test_a_first_insert_retires_nothing(retire_calls):
    """No link existed, so nothing was repointed and there is no old identity to retire.

    The state assertion alone would not catch a hook that fired here — `retire_buyer_refs_for_
    buyer("")` raises inside the swallowing try and changes nothing. The empty call list does.
    """
    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    assert await upsert(REAL_BUYER) == ref_hash()

    assert retire_calls == []
    assert await buyer_ref_count(REAL_BUYER) == 1
    assert dict(await enrollment_row(enrollment_id))["status"] == "active"


async def test_an_idempotent_reupsert_with_the_same_buyer_is_not_a_repoint_at_all(retire_calls, caplog):
    """The link already names this buyer. A second POST from the same signed-in human is not a
    repoint, and retiring on it would kill the card of the buyer who just proved who they are —
    the most damaging mutant on this path, because it fires on the COMMON case.

    ── WHY THIS ASSERTS ON THE LOG AND NOT ONLY ON THE CALL LIST ───────────────────────────

    A MUTANT SURVIVED THE FIRST VERSION OF THIS TEST, and it is worth recording why. Delete the
    `old == new` conjunct and the hook falls through to the REMAINING-LINKS guard — which finds
    the link that was just written, naming this very buyer — and returns without retiring. The
    call list stays empty, the rows are untouched, and the test passed. The second guard was
    masking the first, which is exactly the `an unreachable guard reads as protection that does
    not exist` shape.

    What distinguishes them is what reaches the log. With the conjunct this is not a repoint and
    says nothing; without it, every ordinary re-save of a signed-in buyer's checkout emits
    `event=reap_buyer_link_repointed_kept` — a line whose whole meaning is "a repoint happened
    and was declined", on a path where no repoint happened at all. That is a real defect (an
    operator counting repoints would count sign-ins) and it is what this assertion kills.
    """
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=REAL_BUYER)
    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    with caplog.at_level(logging.INFO, logger="routes.buyer_api"):
        assert await upsert(REAL_BUYER) == ref_hash()

    assert retire_calls == []
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "reap_buyer_link_repointed" not in text, (
        "a re-upsert onto the same buyer id produced a repoint log line; it is not a repoint"
    )
    assert await buyer_ref_count(REAL_BUYER) == 1
    row = dict(await enrollment_row(enrollment_id))
    assert row["status"] == "active"
    # `mark_enrollment_active` clears `hosted_url` itself, so its absence proves nothing here —
    # the reason column is what a retirement would have written.
    assert row["reap_status"] != "buyer_link_repointed"


async def test_a_minted_to_real_repoint_retires_the_minted_identity(retire_calls):
    """THE CASE WP4c EXISTS FOR, end to end.

    A `u_<16hex>` identity the Reap rail minted on an agent's first purchase, with a live card
    enrolled against it, is repointed to the human's real account by the hosted checkout. After
    it: the enrollment is dead and says WHY, its hosted page is gone, the refs row is gone, the
    link names the real account — and the PURCHASE is untouched and still readable by the agent
    that made it, because a purchase is owned by `(agent_id, agent_user_ref_hash)` on its own
    row and the repoint does not change either.
    """
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)
    purchase = await ledger.create_purchase(
        buyer_ref=REAP_REF,
        agent_id=AGENT,
        agent_user_ref_hash=ref_hash(),
        merchant_domain="brand.example",
    )

    assert await upsert(REAL_BUYER) == ref_hash()

    assert retire_calls == [(MINTED_BUYER, "buyer_link_repointed")]
    assert await link_buyer_id(agent_id=AGENT, ref_hash=ref_hash()) == REAL_BUYER

    row = dict(await enrollment_row(enrollment_id))
    assert row["status"] == "dead"
    assert row["reap_status"] == "buyer_link_repointed"
    # The live card-entry page is the reason this matters at all.
    assert row["hosted_url"] is None

    assert await buyer_ref_count(MINTED_BUYER) == 0

    kept = await ledger.get_purchase_for_owner(purchase["id"], AGENT, ref_hash())
    assert kept is not None and kept["id"] == purchase["id"]


async def test_a_pending_enrollment_is_retired_too(retire_calls):
    """`status <> 'dead'`, not `status = 'active'`. A PENDING enrollment carries a live
    `hosted_url` — a page on which a card can still be entered — so sweeping only the active one
    leaves the reachable half of the residue behind."""
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF, status="pending")

    await upsert(REAL_BUYER)

    row = dict(await enrollment_row(enrollment_id))
    assert row["status"] == "dead"
    assert row["reap_status"] == "buyer_link_repointed"
    assert row["hosted_url"] is None


async def test_a_repoint_between_real_accounts_keeps_a_buyer_that_still_has_links(retire_calls):
    """THE CASE THE HOOK DECLINES, and it is the one that would do real damage.

    `reap_agentic_buyer_refs` is keyed on `buyer_id`, NOT on `(agent_id, agent_user_ref_hash)`.
    So one enrollment serves EVERY agent that buyer transacts through. If agent A's link is
    repointed away from a buyer who is also linked through agent B, retiring "that buyer's refs"
    kills a card agent B is actively using — a repoint on one door silently breaking another.

    The test is asked of the LINK TABLE ("does this buyer still own any link?"), never inferred
    from the shape of the id: `old.startswith("u_")` would read the mint format as a type tag,
    which is precisely what `_mint_buyer_id` copied the format to prevent.
    """
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=REAL_BUYER)
    await seed_link(agent_id=OTHER_AGENT, ref_hash=ref_hash(), buyer_id=REAL_BUYER)
    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    assert await upsert(SECOND_REAL_BUYER) == ref_hash()

    assert retire_calls == []
    assert await buyer_ref_count(REAL_BUYER) == 1
    row = dict(await enrollment_row(enrollment_id))
    assert row["status"] == "active"
    assert row["reap_status"] != "buyer_link_repointed"


async def test_a_repoint_of_a_buyers_last_link_does_retire_it(retire_calls):
    """The control for the case above. Same two-account shape, but the old buyer has no OTHER
    link left after the repoint — so it is fully unlinked, its Reap state is unreachable by every
    query on this rail, and the guard must NOT suppress the retirement.

    Without this test, "never retire on a real→real repoint" would pass the suite, and the guard
    would be an unconditional refusal wearing a condition.
    """
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=REAL_BUYER)
    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    await upsert(SECOND_REAL_BUYER)

    assert retire_calls == [(REAL_BUYER, "buyer_link_repointed")]
    assert await buyer_ref_count(REAL_BUYER) == 0
    assert dict(await enrollment_row(enrollment_id))["status"] == "dead"


async def test_the_new_buyers_own_state_is_never_touched(retire_calls):
    """A repoint retires the OLD identity and only the old one. The account being repointed TO is
    the one that just proved who they are; its refs and its enrollment must survive intact."""
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    old_enrollment = await seed_enrollment(reap_buyer_ref=REAP_REF)
    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=OTHER_REAP_REF)
    new_enrollment = await seed_enrollment(reap_buyer_ref=OTHER_REAP_REF)

    await upsert(REAL_BUYER)

    assert dict(await enrollment_row(old_enrollment))["status"] == "dead"
    assert dict(await enrollment_row(new_enrollment))["status"] == "active"
    assert await buyer_ref_count(REAL_BUYER) == 1


# ── the hook: it may never break the checkout ────────────────────────────────────────────────


async def test_a_raising_retire_leaves_the_checkout_unaffected(monkeypatch, caplog):
    """THE PROPERTY THE HOSTED CHECKOUT DEPENDS ON. A human is waiting on this response. Whatever
    the Reap rail does — a missing table, a driver error, a bug in the ledger — the upsert must
    still return its ref hash, the link must still be repointed, and nothing may escape.

    The exception's TYPE is logged; the exception is not. A driver error stringifies the statement
    AND its bound parameters, and the bound parameters on this path are buyer ids.
    """

    async def boom(buyer_id, *, reason):
        raise RuntimeError(f"ledger exploded while binding buyer_id={buyer_id!r}")

    monkeypatch.setattr(ledger, "retire_buyer_refs_for_buyer", boom)

    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)

    with caplog.at_level(logging.WARNING, logger="routes.buyer_api"):
        assert await upsert(REAL_BUYER) == ref_hash()

    assert await link_buyer_id(agent_id=AGENT, ref_hash=ref_hash()) == REAL_BUYER

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=reap_buyer_link_repoint_failed" in text
    assert "RuntimeError" in text
    # The message the ledger raised carried the buyer id. It must not have been logged.
    assert MINTED_BUYER not in text


# THE TWO FALLBACK-ARM CASES LIVE IN THE SQLITE COLLECTOR, NOT HERE, and that is a statement
# about the BASE function rather than about WP4c. On Postgres the fallback's
# `await database.execute(buyer_identity_links.update()…)` returns None — `databases` 0.7.0 gives
# no rowcount on this backend (measured: the UPDATE lands, the call answers None) — so
# `if int(updated or 0) > 0` is False, the function falls through to its INSERT, that INSERT
# violates `uq_buyer_identity_links_agent_ref_hash`, and `_upsert_buyer_identity_link` returns
# None for a repoint it actually performed. The arm is structurally unreachable on the engine
# production runs, and it was before WP4c. tests/test_reap_buyer_link_repoint_postgres.py
# ::test_the_fallback_arm_is_unreachable_on_postgres pins that so it cannot change silently.


async def test_a_failed_upsert_retires_nothing(monkeypatch, retire_calls):
    """Both write arms fail, so no repoint happened. Retiring the old identity on a write that
    did not land would destroy the card of a buyer whose link still names them."""
    real_execute = database.execute

    async def execute(query, *args, **kwargs):
        if "buyer_identity_links" in str(query).lower():
            raise RuntimeError("the link table is unavailable")
        return await real_execute(query, *args, **kwargs)

    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    monkeypatch.setattr(database, "execute", execute)
    assert await upsert(REAL_BUYER) is None
    monkeypatch.undo()

    assert retire_calls == []
    assert await link_buyer_id(agent_id=AGENT, ref_hash=ref_hash()) == MINTED_BUYER
    assert dict(await enrollment_row(enrollment_id))["status"] == "active"
    assert await buyer_ref_count(MINTED_BUYER) == 1


# ── the log lines ────────────────────────────────────────────────────────────────────────────


async def test_the_repoint_log_line_carries_counts_and_no_identifiers(caplog):
    """Two integers, and nothing else. No buyer id (durable, joins to orders and addresses), no
    agent id, no ref hash, no raw user ref, and no `reap_buyer_ref` — which is opaque but is the
    identity a PARTNER also holds, so logging it joins our access log to Reap's records."""
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    await seed_enrollment(reap_buyer_ref=REAP_REF)

    with caplog.at_level(logging.INFO, logger="routes.buyer_api"):
        await upsert(REAL_BUYER)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=reap_buyer_link_repointed refs=1 enrollments=1" in text
    for secret in secret_strings():
        assert secret not in text


async def test_the_kept_log_line_carries_no_identifiers(caplog):
    """Same rule on the arm that declines. The line exists so an operator can see the decision
    was made; the decision is the whole of what it may say."""
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=REAL_BUYER)
    await seed_link(agent_id=OTHER_AGENT, ref_hash=ref_hash(), buyer_id=REAL_BUYER)
    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=REAP_REF)
    await seed_enrollment(reap_buyer_ref=REAP_REF)

    with caplog.at_level(logging.INFO, logger="routes.buyer_api"):
        await upsert(SECOND_REAL_BUYER)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=reap_buyer_link_repointed_kept" in text
    for secret in secret_strings():
        assert secret not in text


# ── the ledger function on its own ───────────────────────────────────────────────────────────


async def test_retire_is_idempotent():
    """A second call returns zeros. The caller is a hook on a route a buyer can hit twice in a
    second, so "already done" must be a measurement and not an error."""
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    first = await ledger.retire_buyer_refs_for_buyer(MINTED_BUYER, reason="buyer_link_repointed")
    assert (first.refs_retired, first.enrollments_marked_dead) == (1, 1)

    second = await ledger.retire_buyer_refs_for_buyer(MINTED_BUYER, reason="buyer_link_repointed")
    assert (second.refs_retired, second.enrollments_marked_dead) == (0, 0)

    assert dict(await enrollment_row(enrollment_id))["status"] == "dead"


async def test_retire_of_an_unknown_buyer_is_zeros_not_an_error():
    report = await ledger.retire_buyer_refs_for_buyer("u_nobody", reason="buyer_link_repointed")
    assert (report.refs_retired, report.enrollments_marked_dead) == (0, 0)


async def test_retire_counts_every_enrollment_on_the_ref():
    """`mark_enrollment_dead` is called per ROW, not once per ref: a ref can carry a stack of
    pending attempts beside its active one, and each of them is a page a card can be entered on.

    The count is what the log line reports, so it is asserted rather than inferred from "they are
    all dead now" — a sweep that dealt with one and reported three would read as healthy.
    """
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    first = await seed_enrollment(reap_buyer_ref=REAP_REF, status="pending")
    second = await seed_enrollment(reap_buyer_ref=REAP_REF, status="pending")
    third = await seed_enrollment(reap_buyer_ref=REAP_REF)

    report = await ledger.retire_buyer_refs_for_buyer(MINTED_BUYER, reason="buyer_link_repointed")

    assert (report.refs_retired, report.enrollments_marked_dead) == (1, 3)
    for enrollment_id in (first, second, third):
        assert dict(await enrollment_row(enrollment_id))["status"] == "dead"


async def test_retire_deletes_the_refs_row():
    """Stated on its own, because "the enrollments are dead" and "the refs row is gone" are two
    assertions and a mutant that drops the DELETE satisfies the first."""
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    await seed_enrollment(reap_buyer_ref=REAP_REF)

    await ledger.retire_buyer_refs_for_buyer(MINTED_BUYER, reason="buyer_link_repointed")

    assert await buyer_ref_count(MINTED_BUYER) == 0
    assert (
        await database.fetch_val(
            "SELECT COUNT(*) FROM reap_agentic_buyer_refs WHERE reap_buyer_ref = :r",
            {"r": REAP_REF},
        )
        == 0
    )


async def test_retire_touches_nobody_elses_ref():
    """Scoped by construction. A second buyer's ref and enrollment sit in the same tables and
    must be untouched — the statements are keyed on the buyer id, not on the table."""
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    mine = await seed_enrollment(reap_buyer_ref=REAP_REF)
    await seed_buyer_ref(buyer_id=REAL_BUYER, reap_buyer_ref=OTHER_REAP_REF)
    theirs = await seed_enrollment(reap_buyer_ref=OTHER_REAP_REF)

    report = await ledger.retire_buyer_refs_for_buyer(MINTED_BUYER, reason="buyer_link_repointed")

    assert (report.refs_retired, report.enrollments_marked_dead) == (1, 1)
    assert dict(await enrollment_row(mine))["status"] == "dead"
    assert dict(await enrollment_row(theirs))["status"] == "active"
    assert await buyer_ref_count(REAL_BUYER) == 1


@pytest.mark.parametrize(
    "reason",
    [
        "Buyer_Link_Repointed",          # folded, and a folded reason is a different reason
        "buyer link repointed",          # a sentence, not a code
        "b" * 65,                        # past the VARCHAR(64) the column is
        "",
        None,
        123,
        "buyer_link_repointed\nevent=ok",  # a forged log line
    ],
)
async def test_retire_refuses_a_reason_outside_the_vocabulary(reason):
    """REFUSED, NOT FOLDED OR TRUNCATED, and refused BEFORE anything is written.

    `reap_status` is what an operator reads to find out why a card stopped being honoured, so a
    reason that is silently a different reason is worse than no reason. The row surviving is half
    the assertion: a validator that ran after the sweep would let the state change anyway.
    """
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    with pytest.raises(ValueError):
        await ledger.retire_buyer_refs_for_buyer(MINTED_BUYER, reason=reason)

    assert await buyer_ref_count(MINTED_BUYER) == 1
    assert dict(await enrollment_row(enrollment_id))["status"] == "active"


@pytest.mark.parametrize("buyer_id", ["", "   ", None, 123])
async def test_retire_refuses_a_bad_buyer_id(buyer_id):
    """A non-string id produces a DIFFERENT wrong answer per engine — a quiet None on SQLite, a
    raw asyncpg DataError on Postgres. One ValueError beats both, and `""` must never mean
    "every row"."""
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)

    with pytest.raises(ValueError):
        await ledger.retire_buyer_refs_for_buyer(buyer_id, reason="buyer_link_repointed")

    assert await buyer_ref_count(MINTED_BUYER) == 1


async def test_the_report_is_frozen_and_carries_only_counts():
    """`RetireReport` reaches a log line. Anything on it that identified a person would be one
    `logger.info("%s", report)` away from an access log — so the type holds two ints, and is
    frozen because it measures something that already happened."""
    import dataclasses

    report = await ledger.retire_buyer_refs_for_buyer("u_nobody", reason="buyer_link_repointed")
    fields = {f.name: f.type for f in dataclasses.fields(report)}
    assert set(fields) == {"refs_retired", "enrollments_marked_dead"}
    assert all(str(t) == "int" for t in fields.values())
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.refs_retired = 99


# ── the statements ───────────────────────────────────────────────────────────────────────────


def test_every_statement_wp4c_added_is_a_literal():
    """PREPARE-SWEEPABLE. tests/test_repo_sql_prepare_postgres.py plans every SQL constant in the
    repo against the real schema, and it finds them by reading module-level STRING CONSTANTS. A
    statement built with an f-string or a `.format()` is invisible to it — the #1588 shape this
    rail avoids everywhere else — so the three WP4c statements are written out."""
    for module, names in (
        (
            ledger,
            (
                "_SELECT_BUYER_REFS_FOR_BUYER_SQL",
                "_SELECT_LIVE_ENROLLMENTS_FOR_REF_SQL",
                "_DELETE_BUYER_REF_SQL",
            ),
        ),
        (buyer_api, ("_EXISTING_LINK_BUYER_ID_SQL", "_ANY_LINK_FOR_BUYER_SQL")),
    ):
        for name in names:
            sql = getattr(module, name)
            assert isinstance(sql, str) and sql.strip()
            assert "{" not in sql and "%" not in sql, f"{name} is not a literal statement"


def test_the_peek_and_the_upsert_agree_on_the_key():
    """The pre-upsert peek must read the row the upsert is about to change. Its WHERE clause and
    the upsert's ON CONFLICT target are the same two columns, and a drift between them would make
    the hook decide about a DIFFERENT link — silently, and only for buyers who have two."""
    import inspect

    upsert_src = inspect.getsource(buyer_api._upsert_buyer_identity_link)
    assert "ON CONFLICT (agent_id, agent_user_ref_hash)" in upsert_src
    peek = buyer_api._EXISTING_LINK_BUYER_ID_SQL
    assert "agent_id = :agent_id" in peek
    assert "agent_user_ref_hash = :agent_user_ref_hash" in peek
