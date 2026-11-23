"""db/partner_agent_merchant_blocks and its operator tool: the ADR-030 D11 challenge/block record.

Two layers, each against the real thing below it:

  * the store, on the process database (SQLite here; the same cases run on Postgres through
    tests/test_partner_agent_merchant_blocks_postgres.py, which star-imports this module);
  * the operator tool (`scripts.partner_agent_merchant_blocks`), driven through its own parser
    and `amain` / `main` over the real store.

No domain, name or purchase id here is a real one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from db import partner_agent_merchant_blocks as blocks
from db.database import IS_POSTGRES, database
from scripts import partner_agent_merchant_blocks as tool
from services.payment_orchestration import router
from utils import database_readiness

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO / "db/migrations/295_partner_agent_merchant_blocks.sql"
_DOWN = _REPO / "db/migrations/down/295_partner_agent_merchant_blocks_down.sql"
TABLE = blocks.TABLE

SHOP = "brand-a.example"
OTHER = "brand-b.example"
STAFF = "Ops Person One"
EVIDENCE = "ticket OPS-0001, merchant letter on file"

#: The SQLite copy of migration 295's table and indexes, for this suite only.
_CREATE_SQL_SQLITE = [
    """
CREATE TABLE IF NOT EXISTS partner_agent_merchant_blocks (
    id VARCHAR(40) PRIMARY KEY,
    rail VARCHAR(16) NOT NULL
        CONSTRAINT ck_partner_agent_merchant_blocks_rail CHECK (rail IN ('reap', 'a2a')),
    merchant_domain VARCHAR(255) NOT NULL,
    kind VARCHAR(16) NOT NULL
        CONSTRAINT ck_partner_agent_merchant_blocks_kind
        CHECK (kind IN ('captcha', 'bot_challenge', 'block')),
    source VARCHAR(16) NOT NULL
        CONSTRAINT ck_partner_agent_merchant_blocks_source CHECK (source IN ('worker', 'operator')),
    purchase_id VARCHAR(64),
    recorded_by VARCHAR(200),
    observed_at TIMESTAMP NOT NULL,
    cleared_at TIMESTAMP,
    cleared_basis VARCHAR(24)
        CONSTRAINT ck_partner_agent_merchant_blocks_basis
        CHECK (cleared_basis IS NULL OR cleared_basis IN ('verified_recognition', 'written_permission')),
    cleared_by VARCHAR(200),
    cleared_evidence VARCHAR(500),
    CONSTRAINT ck_partner_agent_merchant_blocks_cleared_together CHECK (
        (cleared_at IS NULL AND cleared_basis IS NULL AND cleared_by IS NULL AND cleared_evidence IS NULL)
        OR (cleared_at IS NOT NULL AND cleared_basis IS NOT NULL AND cleared_by IS NOT NULL
            AND cleared_evidence IS NOT NULL)
    )
)
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_agent_merchant_blocks_purchase
    ON partner_agent_merchant_blocks (purchase_id) WHERE purchase_id IS NOT NULL
""",
    """
CREATE INDEX IF NOT EXISTS idx_partner_agent_merchant_blocks_open
    ON partner_agent_merchant_blocks (rail, merchant_domain) WHERE cleared_at IS NULL
""",
]

#: This suite DROPS and empties its table, so on Postgres it runs only against a throwaway
#: database (the sibling gates' convention).
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_")


def _assert_throwaway_database() -> None:
    if not IS_POSTGRES:
        return
    url = (os.getenv("DATABASE_URL") or "").strip()
    dbname = url.rsplit("/", 1)[-1].split("?")[0]
    if not any(marker in dbname for marker in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop {TABLE} in database {dbname!r}: throwaway databases only")


def _migration_statements() -> List[str]:
    body = "\n".join(line for line in _MIGRATION.read_text("utf-8").splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


async def _create_table() -> None:
    """Postgres: migration 295 itself, split the way the migration runner splits it. SQLite: this
    suite's copy."""
    if IS_POSTGRES:
        from db.sql_migrations import split_statements

        for statement in split_statements(_MIGRATION.read_text("utf-8")):
            await database.execute(statement)
    else:
        for statement in _CREATE_SQL_SQLITE:
            await database.execute(statement)


@pytest.fixture
async def _blocks_db():
    """The table, empty, on the process database. It is this suite's alone: emptied before and
    after, and rebuilt afterwards for the test that drops it."""
    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _create_table()
    await database.execute(f"DELETE FROM {TABLE}")
    yield
    await _create_table()
    await database.execute(f"DELETE FROM {TABLE}")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _rows(**where: Any) -> List[Dict[str, Any]]:
    found = await blocks.list_blocks(**where)
    assert found is not None
    return found


async def _count() -> int:
    return int(await database.fetch_val(f"SELECT COUNT(*) FROM {TABLE}"))


async def _worker_event(domain: str = SHOP, rail: str = "a2a", purchase: str = "ap_test_0001", kind: str = "captcha"):
    return await blocks.record_block(rail, domain, kind, source="worker", purchase_id=purchase)


NO_SUCH_EVENT = "pab_" + "f" * 32
_OPEN = object()


async def _open_ids(domain: str = SHOP, rail: str = "a2a") -> List[str]:
    return [row["id"] for row in await _rows(rail=rail, domain=domain, open_only=True)]


async def _clear(domain: Any = SHOP, rail: Any = "a2a", basis: Any = "written_permission",
                 by: Any = STAFF, evidence: Any = EVIDENCE, ids: Any = _OPEN) -> Optional[int]:
    """Clear the listed events; by default the ones open for that rail and domain right now."""
    if ids is _OPEN:
        ids = await _open_ids(SHOP if not isinstance(domain, str) or not domain.strip() else domain,
                              "a2a" if rail not in router.RAILS else rail)
    return await blocks.clear_blocks(rail, domain, ids=ids, basis=basis, cleared_by=by, evidence=evidence)


_EVENT_COLUMNS = ("id", "rail", "merchant_domain", "kind", "source", "purchase_id", "recorded_by", "observed_at")
_CLEARED_COLUMNS = ("cleared_at", "cleared_basis", "cleared_by", "cleared_evidence")


# ── the store ────────────────────────────────────────────────────────────────────────────────


async def test_a_merchant_with_no_event_is_clear(_blocks_db):
    assert await blocks.is_clear("a2a", SHOP) is True and await blocks.is_clear("reap", SHOP) is True


async def test_an_event_closes_the_merchant_for_that_rail_only(_blocks_db):
    assert await _worker_event() == blocks.RECORDED
    assert await blocks.is_clear("a2a", SHOP) is False
    assert await blocks.is_clear("reap", SHOP) is True, "an event on one rail says nothing about the other"
    assert await blocks.is_clear("a2a", OTHER) is True
    (row,) = await _rows()
    assert (row["rail"], row["merchant_domain"], row["kind"], row["source"], row["purchase_id"]) == (
        "a2a", SHOP, "captcha", "worker", "ap_test_0001")
    assert row["id"].startswith("pab_") and row["observed_at"] is not None
    assert [row[c] for c in _CLEARED_COLUMNS] == [None] * 4 and row["recorded_by"] is None


@pytest.mark.parametrize("spelling", ["WWW.Brand-A.example", "https://www.brand-a.example/cart/1:1", "brand-a.example."])
async def test_the_event_is_found_under_any_spelling_of_the_domain(_blocks_db, spelling):
    await blocks.record_block("a2a", "www.Brand-A.example", "block", source="worker", purchase_id="ap_test_0001")
    assert (await _rows())[0]["merchant_domain"] == SHOP
    assert await blocks.is_clear("a2a", spelling) is False
    assert len(await _rows(domain=spelling)) == 1


async def test_one_purchase_records_one_event(_blocks_db):
    assert await _worker_event() == blocks.RECORDED
    assert await _worker_event() == blocks.DUPLICATE
    assert await _worker_event(kind="block") == blocks.DUPLICATE, "a retry never rewrites the first event"
    (row,) = await _rows()
    assert row["kind"] == "captcha"
    assert await _worker_event(purchase="ap_test_0002") == blocks.RECORDED and await _count() == 2


@pytest.mark.parametrize("rail,domain", [("a2a", OTHER), ("reap", SHOP), ("reap", OTHER)])
async def test_a_purchase_id_recorded_for_another_merchant_or_rail_is_a_conflict_not_a_duplicate(_blocks_db, rail, domain):
    """Answering "duplicate" here would tell the caller the event is on record for a merchant that
    has none, and that merchant would stay clear."""
    assert await _worker_event() == blocks.RECORDED
    assert await blocks.record_block(rail, domain, "captcha", source="worker", purchase_id="ap_test_0001") is None
    assert await _count() == 1
    assert await blocks.is_clear(rail, domain) is True, "nothing was recorded for it, and nothing said it was"
    assert await _worker_event() == blocks.DUPLICATE


async def test_operator_events_carry_a_name_and_are_never_merged(_blocks_db):
    for _ in range(2):
        assert await blocks.record_block("reap", SHOP, "bot_challenge", source="operator", recorded_by=f"  {STAFF} ") == blocks.RECORDED
    rows = await _rows(rail="reap")
    assert len(rows) == 2 and {r["recorded_by"] for r in rows} == {STAFF} and {r["purchase_id"] for r in rows} == {None}
    assert await blocks.is_clear("reap", SHOP) is False


@pytest.mark.parametrize("kwargs", [
    dict(rail="rail_x"), dict(rail=""), dict(rail=None), dict(rail="A2A"),
    dict(domain=""), dict(domain=None), dict(domain="   "),
    dict(domain="not a domain"), dict(domain="a.example,b.example"), dict(domain="localhost"),
    dict(domain="10.0.0.1"), dict(domain="brand_a.example"),
    dict(kind="issuer_step_up"), dict(kind=""), dict(kind=None), dict(kind="CAPTCHA"),
    dict(source="request"), dict(source=None),
    dict(source="worker", purchase_id=None),                    # a worker event needs its purchase
    dict(source="operator", purchase_id=None, recorded_by=None),  # an operator event needs a name
    dict(source="operator", recorded_by="   "),
    dict(purchase_id="ap test"), dict(purchase_id="x" * 65), dict(purchase_id=""), dict(purchase_id=7),
    dict(source="operator", recorded_by="line one\nline two"), dict(source="operator", recorded_by="n" * 201),
    dict(source="operator", recorded_by="Ops \u202eenO nosreP"), dict(source="operator", recorded_by="Ops\u0085One"),
    # A part that does not fit refuses the whole event. It is never dropped to let the rest through.
    dict(source="operator", recorded_by=STAFF, purchase_id="ap test"),
    dict(source="worker", recorded_by="line one\nline two"),
], ids=lambda k: ",".join(f"{a}={ascii(b)[:14]}" for a, b in k.items()))
async def test_an_event_that_does_not_fit_is_refused_and_nothing_is_written(_blocks_db, kwargs):
    args = dict(rail="a2a", domain=SHOP, kind="captcha", source="worker", purchase_id="ap_test_0001", recorded_by=None)
    args.update(kwargs)
    rail, domain, kind = args.pop("rail"), args.pop("domain"), args.pop("kind")
    assert blocks.event_values(rail, domain, kind, **args) is None
    assert await blocks.record_block(rail, domain, kind, **args) is None
    assert await _count() == 0


async def test_clearing_reopens_the_merchant_and_keeps_the_event(_blocks_db):
    await _worker_event()
    (before,) = await _rows()
    assert await _clear(basis="verified_recognition") == 1
    assert await blocks.is_clear("a2a", SHOP) is True
    (after,) = await _rows()
    assert [after[c] for c in _EVENT_COLUMNS] == [before[c] for c in _EVENT_COLUMNS], "the event itself is untouched"
    assert (after["cleared_basis"], after["cleared_by"], after["cleared_evidence"]) == (
        "verified_recognition", STAFF, EVIDENCE)
    assert after["cleared_at"] is not None and await _count() == 1


async def test_the_first_clearance_of_an_event_stands(_blocks_db):
    await _worker_event()
    (event_id,) = await _open_ids()
    await _clear(basis="written_permission")
    (first,) = await _rows()
    assert await _clear(basis="verified_recognition", by="Ops Person Two", evidence="another reference",
                        ids=[event_id]) == 0
    (again,) = await _rows()
    assert [again[c] for c in _CLEARED_COLUMNS] == [first[c] for c in _CLEARED_COLUMNS]


async def test_a_new_event_after_a_clearance_closes_the_merchant_again(_blocks_db):
    await _worker_event()
    await _clear()
    await _worker_event(purchase="ap_test_0002", kind="block")
    assert await blocks.is_clear("a2a", SHOP) is False
    assert len(await _rows(open_only=True)) == 1 and len(await _rows()) == 2
    assert await _clear(basis="verified_recognition", by="Ops Person Two", evidence="second reference") == 1
    by_purchase = {r["purchase_id"]: r for r in await _rows()}
    assert by_purchase["ap_test_0001"]["cleared_basis"] == "written_permission", "the older clearance is not rewritten"
    assert by_purchase["ap_test_0002"]["cleared_basis"] == "verified_recognition"
    assert await blocks.is_clear("a2a", SHOP) is True


async def test_a_clearance_touches_only_its_own_rail_and_domain(_blocks_db):
    await _worker_event()
    await _worker_event(rail="reap", purchase="ap_test_0002")
    await _worker_event(domain=OTHER, purchase="ap_test_0003")
    assert await _clear() == 1
    assert [await blocks.is_clear("a2a", SHOP), await blocks.is_clear("reap", SHOP), await blocks.is_clear("a2a", OTHER)] == [
        True, False, False]


async def test_clearing_an_id_that_is_no_event_clears_nothing(_blocks_db):
    assert await _clear(ids=[NO_SUCH_EVENT]) == 0 and await _count() == 0


async def test_a_clearance_clears_the_events_that_were_reviewed_and_no_later_one(_blocks_db):
    """The race the review reproduced: evidence looked at on Monday must not clear a challenge
    that arrives on Tuesday, between the review and the apply."""
    await _worker_event()
    reviewed = await _open_ids()
    await _worker_event(purchase="ap_test_0002", kind="block")       # arrives after the review
    assert await _clear(ids=reviewed) == 1
    still_open = await _rows(open_only=True)
    assert [row["purchase_id"] for row in still_open] == ["ap_test_0002"]
    assert await blocks.is_clear("a2a", SHOP) is False


async def test_a_clearance_is_all_or_nothing(_blocks_db):
    await _worker_event()
    await _worker_event(domain=OTHER, purchase="ap_test_0002")
    await _worker_event(rail="reap", purchase="ap_test_0003")
    (mine,), (others,), (other_rail,) = await _open_ids(), await _open_ids(OTHER), await _open_ids(SHOP, "reap")
    for listed in ([mine, others], [mine, other_rail], [mine, NO_SUCH_EVENT]):
        assert await _clear(ids=listed) == 0, listed
        assert len(await _rows(open_only=True)) == 3, "one id that is not this merchant's open event stops all of it"
    await _clear(ids=[mine])
    assert await _clear(ids=[mine]) == 0, "an event that is already cleared is not an open event"
    assert await _clear(domain=OTHER, ids=[others]) == 1


async def test_a_clearance_of_several_events_clears_each_of_them(_blocks_db):
    for n in range(3):
        await _worker_event(purchase=f"ap_test_000{n}")
    listed = await _open_ids()
    assert await _clear(ids=listed[:2]) == 2
    assert await _open_ids() == [i for i in listed if i not in listed[:2]]


@pytest.mark.parametrize("kwargs", [
    dict(basis="later_order"), dict(basis="completed_order"), dict(basis=""), dict(basis=None),
    dict(basis="WRITTEN_PERMISSION"),
    dict(by=""), dict(by="   "), dict(by=None), dict(by="a\tb"), dict(by="n" * 201),
    dict(evidence=""), dict(evidence="  "), dict(evidence=None), dict(evidence="e" * 501), dict(evidence="x\x00y"),
    dict(rail="rail_x"), dict(domain=""), dict(domain="not a domain"), dict(domain="a.example,b.example"),
    # The evidence field says where the evidence is kept. It never carries a link with a query
    # string, a key=value pair or a credential.
    dict(evidence="https://files.example/letter.pdf?token=abc123&sig=def456"),
    dict(evidence="https://files.example/letter.pdf?download"), dict(evidence="ref=OPS-0001"),
    dict(evidence="letter a & b"), dict(evidence="portal password hunter2"), dict(evidence="Bearer abc.def"),
    dict(evidence="api-key in vault"), dict(evidence="session_id 9f"),
    # No format or control character: a direction override hides what a reader sees.
    dict(by="Ops \u202eenO nosreP"), dict(by="Ops\u0085Person"), dict(by="Ops\u200bPerson"),
    dict(evidence="ticket OPS-0001\u202e"), dict(evidence="line\u2028two"),
    dict(ids=[]), dict(ids=None), dict(ids="pab_" + "a" * 32), dict(ids=["pab_short"]),
    dict(ids=["PAB_" + "a" * 32]), dict(ids=[NO_SUCH_EVENT, NO_SUCH_EVENT]), dict(ids=[7]),
    dict(ids=[f"pab_{n:032x}" for n in range(blocks.LIST_MAX + 1)]),
], ids=lambda k: ",".join(f"{a}={ascii(b)[:14]}" for a, b in k.items()))
async def test_a_clearance_without_a_d11_basis_a_name_and_evidence_is_refused(_blocks_db, kwargs):
    await _worker_event()
    (open_id,) = await _open_ids()
    assert blocks.clearance_values(
        kwargs.get("rail", "a2a"), kwargs.get("domain", SHOP), ids=kwargs.get("ids", [open_id]),
        basis=kwargs.get("basis", "written_permission"),
        cleared_by=kwargs.get("by", STAFF), evidence=kwargs.get("evidence", EVIDENCE)) is None
    assert await _clear(**{"ids": [open_id], **kwargs}) is None
    (row,) = await _rows()
    assert [row[c] for c in _CLEARED_COLUMNS] == [None] * 4 and await blocks.is_clear("a2a", SHOP) is False


@pytest.mark.parametrize("evidence", [
    EVIDENCE, "https://docs.example/letters/brand-a-2026-11", "Drive: Merchant letters / brand-a (signed 2026-11-20)",
    "card network programme listing, checked by Ops Person One", "e" * 500, "工单 OPS-0001",
])
async def test_a_plain_reference_to_where_the_evidence_is_kept_is_accepted(_blocks_db, evidence):
    await _worker_event()
    assert await _clear(evidence=evidence) == 1
    assert (await _rows())[0]["cleared_evidence"] == evidence


def test_only_the_two_bases_d11_names_exist():
    assert blocks.BASES == {"verified_recognition", "written_permission"}
    assert blocks.KINDS == {"captcha", "bot_challenge", "block"}


@pytest.mark.parametrize("rail,domain", [("rail_x", SHOP), ("", SHOP), (None, SHOP), ("a2a", ""), ("a2a", None), ("A2A", SHOP),
                                         ("a2a", "not a domain"), ("a2a", "a.example,b.example"), ("a2a", "localhost")])
async def test_an_unusable_key_is_never_clear(_blocks_db, rail, domain):
    assert await blocks.is_clear(rail, domain) is False


async def test_a_missing_table_is_never_clear_and_never_an_error(_blocks_db):
    await database.execute(f"DROP TABLE {TABLE}")
    assert await blocks.table_is_readable() is False
    assert await blocks.is_clear("a2a", SHOP) is False
    assert await _worker_event() is None and await _clear(ids=[NO_SUCH_EVENT]) is None
    assert await blocks.list_blocks() is None


async def test_a_slow_read_is_not_clear(_blocks_db, monkeypatch):
    real = database.fetch_one

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        return await real(*args, **kwargs)

    monkeypatch.setattr(blocks, "READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(database, "fetch_one", slow)
    assert await blocks.is_clear("a2a", SHOP) is False
    assert await blocks.table_is_readable() is False


async def test_a_failed_write_is_none_and_does_not_raise(_blocks_db, monkeypatch):
    await _worker_event()
    listed = await _open_ids()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("connection lost")

    monkeypatch.setattr(database, "fetch_one", broken)
    monkeypatch.setattr(database, "fetch_all", broken)
    assert await _worker_event(purchase="ap_test_0002") is None
    assert await _clear(ids=listed) is None
    assert await blocks.list_blocks() is None


async def test_the_list_filters_and_an_unusable_filter_is_not_no_filter(_blocks_db):
    await _worker_event()
    await _worker_event(rail="reap", purchase="ap_test_0002")
    await _worker_event(domain=OTHER, purchase="ap_test_0003")
    await _clear()
    assert len(await _rows()) == 3
    assert {r["purchase_id"] for r in await _rows(rail="a2a")} == {"ap_test_0001", "ap_test_0003"}
    assert {r["purchase_id"] for r in await _rows(domain=SHOP)} == {"ap_test_0001", "ap_test_0002"}
    assert {r["purchase_id"] for r in await _rows(open_only=True)} == {"ap_test_0002", "ap_test_0003"}
    assert {r["purchase_id"] for r in await _rows(rail="a2a", domain=SHOP, open_only=True)} == set()
    assert len(await _rows(limit=1)) == 1
    assert await blocks.list_blocks(rail="rail_x") is None
    assert await blocks.list_blocks(domain="   ") is None
    assert await blocks.list_blocks(domain="not a domain") is None


async def test_the_database_itself_refuses_a_half_clearance(_blocks_db):
    """Not only the module: a statement that sets one `cleared_*` column alone is refused by the
    table's own CHECK, on both dialects."""
    await _worker_event()
    with pytest.raises(Exception):
        await database.execute(f"UPDATE {TABLE} SET cleared_basis = 'written_permission'")
    with pytest.raises(Exception):
        await database.execute(f"UPDATE {TABLE} SET cleared_at = CURRENT_TIMESTAMP")
    assert await blocks.is_clear("a2a", SHOP) is False


# ── the operator tool ────────────────────────────────────────────────────────────────────────


class Ran:
    def __init__(self, code: int, lines: List[str]) -> None:
        self.code, self.lines = code, lines

    def payloads(self, prefix: str) -> List[Dict[str, Any]]:
        return [json.loads(x[len(prefix):]) for x in self.lines if x.startswith(prefix)]

    def refusals(self) -> List[str]:
        return [x for x in self.lines if x.startswith(tool.REFUSED_PREFIX)]


async def _tool(*argv: str) -> Ran:
    lines: List[str] = []
    code = await tool.amain(tool.build_parser().parse_args(list(argv)), out=lines.append)
    return Ran(code, lines)


_RECORD = ("record", "--rail", "reap", "--domain", SHOP, "--kind", "bot_challenge", "--by", STAFF)
_CLEAR = ("clear", "--rail", "reap", "--domain", SHOP, "--basis", "written_permission", "--by", STAFF, "--evidence", EVIDENCE)


async def test_the_tool_lists_nothing_on_an_empty_table(_blocks_db):
    got = await _tool("list")
    assert got.code == tool.EXIT_OK and got.lines == ['PABLOCK_LIST {"rows":0}']


async def test_record_is_a_dry_run_until_apply(_blocks_db):
    dry = await _tool(*_RECORD)
    assert dry.code == tool.EXIT_OK and await _count() == 0
    assert dry.payloads(tool.DRY_RUN_PREFIX) == [
        {"action": "record", "rail": "reap", "domain": SHOP, "kind": "bot_challenge", "purchase_id": None, "by": STAFF}]
    done = await _tool(*_RECORD, "--apply")
    assert done.code == tool.EXIT_OK
    assert done.payloads(tool.RECORDED_PREFIX) == [
        {"rail": "reap", "domain": SHOP, "kind": "bot_challenge", "result": "recorded"}]
    (row,) = await _rows()
    assert (row["source"], row["recorded_by"], row["purchase_id"]) == ("operator", STAFF, None)
    assert await blocks.is_clear("reap", SHOP) is False


async def test_record_says_duplicate_for_a_purchase_that_has_its_event(_blocks_db):
    args = (*_RECORD, "--purchase-id", "rp_test_0001", "--apply")
    assert (await _tool(*args)).payloads(tool.RECORDED_PREFIX)[0]["result"] == "recorded"
    again = await _tool(*args)
    assert again.code == tool.EXIT_OK and again.payloads(tool.RECORDED_PREFIX)[0]["result"] == "duplicate"
    assert await _count() == 1


def _ids_of(run: "Ran") -> str:
    return run.payloads(tool.DRY_RUN_PREFIX)[0]["ids"]


async def test_clear_is_a_dry_run_until_apply_and_shows_what_it_would_clear(_blocks_db):
    await _tool(*_RECORD, "--apply")
    await _tool(*_RECORD, "--apply")
    dry = await _tool(*_CLEAR)
    assert dry.code == tool.EXIT_OK and len(dry.payloads(tool.ROW_PREFIX)) == 2
    listed = sorted(row["id"] for row in dry.payloads(tool.ROW_PREFIX))
    assert dry.payloads(tool.DRY_RUN_PREFIX) == [
        {"action": "clear", "rail": "reap", "domain": SHOP, "basis": "written_permission", "by": STAFF,
         "would_clear": 2, "ids": _ids_of(dry)}]
    assert sorted(_ids_of(dry).split(",")) == listed
    assert await blocks.is_clear("reap", SHOP) is False, "a dry run clears nothing"
    done = await _tool(*_CLEAR, "--ids", _ids_of(dry), "--apply")
    assert done.code == tool.EXIT_OK and done.payloads(tool.CLEARED_PREFIX) == [
        {"rail": "reap", "domain": SHOP, "basis": "written_permission", "cleared": 2}]
    assert await blocks.is_clear("reap", SHOP) is True and await _count() == 2
    # A dry run shows only what is OPEN: the two cleared events are not offered again.
    await _tool(*_RECORD, "--apply")
    again = await _tool(*_CLEAR)
    assert len(again.payloads(tool.ROW_PREFIX)) == 1 and again.payloads(tool.DRY_RUN_PREFIX)[0]["would_clear"] == 1
    assert again.payloads(tool.ROW_PREFIX)[0]["cleared_at"] is None


async def test_apply_clears_what_the_dry_run_listed_and_not_what_arrived_since(_blocks_db):
    """The review's reproduction, at the tool: a dry run, then a new event, then --apply."""
    nothing_yet = await _tool(*_CLEAR)
    assert nothing_yet.code == tool.EXIT_OK and nothing_yet.payloads(tool.DRY_RUN_PREFIX)[0]["would_clear"] == 0
    await _tool(*_RECORD, "--apply")                                    # arrives after that dry run
    blind = await _tool(*_CLEAR, "--apply")
    assert blind.code == tool.EXIT_REFUSED and blind.refusals()[0].startswith("PABLOCK_REFUSED ids_required")
    assert await blocks.is_clear("reap", SHOP) is False, "--apply without the reviewed ids clears nothing"

    reviewed = _ids_of(await _tool(*_CLEAR))
    await _tool(*_RECORD, "--apply")                                    # arrives after the review
    done = await _tool(*_CLEAR, "--ids", reviewed, "--apply")
    assert done.payloads(tool.CLEARED_PREFIX)[0]["cleared"] == 1
    assert len(await _rows(open_only=True)) == 1 and await blocks.is_clear("reap", SHOP) is False


async def test_apply_refuses_a_list_that_holds_an_id_that_is_not_open(_blocks_db):
    await _tool(*_RECORD, "--apply")
    reviewed = _ids_of(await _tool(*_CLEAR))
    for listed in (f"{reviewed},{NO_SUCH_EVENT}", NO_SUCH_EVENT):
        for apply in ((), ("--apply",)):
            got = await _tool(*_CLEAR, "--ids", listed, *apply)
            assert got.code == tool.EXIT_REFUSED and got.refusals()[0].startswith("PABLOCK_REFUSED ids_not_open")
    assert await blocks.is_clear("reap", SHOP) is False
    assert (await _tool(*_CLEAR, "--ids", reviewed)).payloads(tool.DRY_RUN_PREFIX)[0]["would_clear"] == 1
    assert (await _tool(*_CLEAR, "--ids", f" {reviewed} ,", "--apply")).code == tool.EXIT_OK
    again = await _tool(*_CLEAR, "--ids", reviewed, "--apply")
    assert again.code == tool.EXIT_REFUSED, "an event that is already cleared is not open"


async def test_the_tool_prints_every_column_of_an_event_as_json(_blocks_db):
    await _tool(*_RECORD, "--apply")
    await _tool(*_CLEAR, "--ids", _ids_of(await _tool(*_CLEAR)), "--apply")
    (row,) = (await _tool("list", "--rail", "reap", "--domain", f"www.{SHOP}")).payloads(tool.ROW_PREFIX)
    assert set(row) == set(_EVENT_COLUMNS) | set(_CLEARED_COLUMNS)
    assert row["cleared_evidence"] == EVIDENCE and isinstance(row["observed_at"], str)
    assert (await _tool("list", "--open")).payloads(tool.LIST_PREFIX) == [{"rows": 0}]


@pytest.mark.parametrize("apply", [(), ("--apply",)], ids=["dry-run", "apply"])
async def test_a_dry_run_refuses_exactly_what_apply_refuses(_blocks_db, apply):
    await _tool(*_RECORD, "--apply")
    listed = _ids_of(await _tool(*_CLEAR))
    bad_record = ("record", "--rail", "reap", "--domain", "   ", "--kind", "block", "--by", STAFF, *apply)
    worse_record = ("record", "--rail", "reap", "--domain", "not a domain", "--kind", "block", "--by", STAFF, *apply)
    bad_clear = ("clear", "--rail", "reap", "--domain", SHOP, "--basis", "written_permission",
                 "--by", STAFF, "--evidence", "   ", "--ids", listed, *apply)
    leaky_clear = ("clear", "--rail", "reap", "--domain", SHOP, "--basis", "written_permission", "--by", STAFF,
                   "--evidence", "https://files.example/letter.pdf?token=abc123", "--ids", listed, *apply)
    for argv in (bad_record, worse_record, bad_clear, leaky_clear):
        got = await _tool(*argv)
        assert got.code == tool.EXIT_REFUSED and len(got.refusals()) == 1 and len(got.lines) == 1
    assert await _count() == 1 and await blocks.is_clear("reap", SHOP) is False


@pytest.mark.parametrize("argv", [
    ("record", "--rail", "rail_x", "--domain", SHOP, "--kind", "block", "--by", STAFF),
    ("record", "--rail", "reap", "--domain", SHOP, "--kind", "issuer_step_up", "--by", STAFF),
    ("clear", "--rail", "reap", "--domain", SHOP, "--basis", "later_order", "--by", STAFF, "--evidence", EVIDENCE),
    ("clear", "--rail", "reap", "--domain", SHOP, "--basis", "written_permission", "--by", STAFF),
    ("clear", "--rail", "reap", "--domain", SHOP, "--basis", "written_permission", "--evidence", EVIDENCE),
    ("delete", "--rail", "reap", "--domain", SHOP),
    (),
], ids=["unknown-rail", "unknown-kind", "basis-not-in-d11", "no-evidence", "no-name", "no-delete-command", "no-command"])
def test_the_parser_itself_refuses_what_d11_does_not_allow(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        tool.build_parser().parse_args(list(argv))
    assert stopped.value.code == 2
    capsys.readouterr()


async def test_the_tool_refuses_before_anything_when_the_table_is_missing(_blocks_db):
    await database.execute(f"DROP TABLE {TABLE}")
    for argv in (("list",), (*_RECORD, "--apply"), (*_CLEAR, "--ids", NO_SUCH_EVENT, "--apply")):
        got = await _tool(*argv)
        assert got.code == tool.EXIT_REFUSED and got.lines == [f"PABLOCK_REFUSED store_unavailable table={TABLE}"]


async def test_the_tool_refuses_when_the_database_cannot_be_reached(monkeypatch):
    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("no database")

    # Both doors: on Postgres the bounded connect builds its own pool and never calls
    # `database.connect`.
    monkeypatch.setattr(database, "is_connected", False)
    monkeypatch.setattr(database, "connect", refuse)
    monkeypatch.setattr(database_readiness, "connect_database_with_timeout", refuse)
    monkeypatch.setattr(tool, "connect_database_with_timeout", refuse)
    got = await _tool("list")
    assert got.code == tool.EXIT_REFUSED and got.lines == ["PABLOCK_REFUSED store_unavailable error_type=OSError"]


def test_the_command_line_connects_and_closes_what_it_opened():
    if database.is_connected:
        pytest.skip("another test left the shared database object connected")
    _assert_throwaway_database()

    async def prepare() -> None:
        await database.connect()
        try:
            await _create_table()
            await database.execute(f"DELETE FROM {TABLE}")
        finally:
            await database.disconnect()

    asyncio.run(prepare())
    lines: List[str] = []
    code = tool.main(["list"], out=lines.append)
    assert code == tool.EXIT_OK and lines == ['PABLOCK_LIST {"rows":0}']
    assert database.is_connected is False


# ── the table: one creation path, one vocabulary, and no way to lose an event ────────────────


def _check_values(name: str, ddl: str) -> set:
    """The quoted values of one named CHECK constraint."""
    found = re.search(name + r"\s+CHECK \(([^;]*?)\)\s*(?:,|\)$)", ddl, flags=re.S | re.M)
    assert found, name
    return set(re.findall(r"'([a-z0-9_]+)'", found.group(1)))


def test_the_table_speaks_the_codes_own_vocabulary():
    migration = "\n".join(_migration_statements())
    for ddl in (migration, _CREATE_SQL_SQLITE[0]):
        assert _check_values("ck_partner_agent_merchant_blocks_rail", ddl) == set(router.RAILS)
        assert _check_values("ck_partner_agent_merchant_blocks_kind", ddl) == set(blocks.KINDS)
        assert _check_values("ck_partner_agent_merchant_blocks_source", ddl) == set(blocks.SOURCES)
        assert _check_values("ck_partner_agent_merchant_blocks_basis", ddl) == set(blocks.BASES)


def test_the_sqlite_copy_has_the_migrations_columns_and_indexes():
    def columns(ddl: str) -> List[str]:
        return re.findall(r"^\s{4}([a-z_]+) (?:VARCHAR|TIMESTAMP)", ddl, flags=re.M)

    statements = _migration_statements()
    assert len(statements) == len(_CREATE_SQL_SQLITE) == 3
    assert columns(statements[0]) == columns(_CREATE_SQL_SQLITE[0]) == list(_EVENT_COLUMNS) + list(_CLEARED_COLUMNS)
    for mine, theirs in zip(_CREATE_SQL_SQLITE[1:], statements[1:]):
        assert " ".join(mine.split()) == " ".join(theirs.split())
    assert "ck_partner_agent_merchant_blocks_cleared_together" in statements[0]


def test_nothing_at_startup_creates_the_table():
    for path in ("db/schema_guard.py", "main.py", "db/sql_migrations.py"):
        assert TABLE not in (_REPO / path).read_text("utf-8"), path
    creators = sorted(
        str(p.relative_to(_REPO)) for p in _REPO.glob("**/*.py")
        if ".venv" not in p.parts and "node_modules" not in p.parts and p.relative_to(_REPO).parts[0] != "tests"
        and f"CREATE TABLE IF NOT EXISTS {TABLE}" in p.read_text("utf-8", errors="ignore"))
    assert creators == [], "only migration 295 (and this suite's SQLite copy) may create the table"


def test_the_migration_only_creates_and_its_down_only_drops():
    text = _MIGRATION.read_text("utf-8").upper()
    assert "CONCURRENTLY" not in text and "DROP " not in text and "ALTER " not in text
    assert [s.split()[0:2] for s in _migration_statements()] == [["CREATE", "TABLE"], ["CREATE", "UNIQUE"], ["CREATE", "INDEX"]]
    down = [line for line in _DOWN.read_text("utf-8").splitlines() if line and not line.startswith("--")]
    assert down == [
        "DROP INDEX IF EXISTS idx_partner_agent_merchant_blocks_open;",
        "DROP INDEX IF EXISTS uq_partner_agent_merchant_blocks_purchase;",
        f"DROP TABLE IF EXISTS {TABLE};",
    ]


def test_no_statement_in_the_application_can_delete_or_rewrite_an_event():
    """Rules 1, 2 and 2a, read off the source: the only UPDATE in the whole application that
    names this table is the clearance (once per dialect); it sets the four `cleared_*` columns
    and nothing else, on open rows among the listed ids, all or nothing. Nothing deletes from the
    table."""
    updates, deletes = [], []
    for path in _REPO.glob("**/*.py"):
        parts = path.relative_to(_REPO).parts
        if ".venv" in parts or "node_modules" in parts or parts[0] == "tests":
            continue
        text = path.read_text("utf-8", errors="ignore")
        updates += re.findall(rf"UPDATE\s+{TABLE}\b.*?(?=\"\"\")", text, flags=re.S)
        deletes += re.findall(rf"DELETE\s+FROM\s+{TABLE}\b", text)
    assert deletes == [] and len(updates) == 2, "one per dialect"
    for update in updates:
        set_clause = update[update.index("SET") + 3:update.index("WHERE")]
        assert sorted(re.findall(r"([a-z_]+)\s*=", set_clause)) == sorted(_CLEARED_COLUMNS)
        where = update[update.index("WHERE"):]
        assert "   AND cleared_at IS NULL\n   AND id IN (" in where, "open rows only, and only the listed ids"
        assert "AND listed.cleared_at IS NULL" in where and ") = CAST(:expected AS INTEGER)" in where


def test_nothing_reads_or_writes_the_record_on_a_request_path_yet():
    for folder in ("routes", "services"):
        for path in (_REPO / folder).rglob("*.py"):
            assert TABLE not in path.read_text("utf-8", errors="ignore"), path
