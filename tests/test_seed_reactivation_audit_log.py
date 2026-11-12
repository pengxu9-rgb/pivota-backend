"""Every operator tool that sets a seed back to `active` outside the draft-approval rule logs it (owner 2026-11-08).

The tools stay ops-only and ungated (docs/runbooks/seed_reactivation_outside_approval.md). What this pins:
- each reactivation site logs one `external_seed_reactivated_outside_approval` line per seed it reactivates, with the
  seed id, the tool and the previous status, and nothing that names a person;
- a logging failure changes nothing the tool does: the same statements, the same return value.

The fakes are the ones each tool's own tests use. All names are placeholders.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import seed_reactivation_audit as audit  # noqa: E402

FIELDS = {"severity", "message", "event", "seed_id", "tool", "previous_status", "row_confirmed"}


class _Recorder:
    def __init__(self, fail: bool = False):
        self.lines: List[Dict[str, Any]] = []
        self.fail = fail

    def warning(self, message, *args, **kwargs):
        if self.fail:
            raise RuntimeError("log sink on fire")
        self.lines.append(json.loads(message))


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(audit, "_logger", rec)
    return rec


@pytest.fixture
def failing_logger(monkeypatch):
    rec = _Recorder(fail=True)
    monkeypatch.setattr(audit, "_logger", rec)
    return rec


def _lines_for(rec, tool):
    out = [line for line in rec.lines if line["tool"] == tool]
    for line in out:
        assert set(line) == FIELDS and line["event"] == audit.EVENT
    return out


# ---------------------------------------------------------------- the helper


def test_one_line_per_seed_with_only_the_audit_fields(recorder):
    audit.log_seed_reactivated_outside_approval(["seed_a", "seed_b"], tool="tool_x", previous_status="inactive",
                                                row_confirmed=False)
    base = {"severity": "WARNING", "message": audit.EVENT, "event": audit.EVENT, "tool": "tool_x",
            "previous_status": "inactive", "row_confirmed": False}
    assert recorder.lines == [dict(base, seed_id="seed_a"), dict(base, seed_id="seed_b")]
    audit.log_seed_reactivated_outside_approval([], tool="tool_x")
    audit.log_seed_reactivated_outside_approval(None, tool="tool_x")
    assert len(recorder.lines) == 2


def test_the_helper_never_raises(failing_logger):
    audit.log_seed_reactivated_outside_approval(["seed_a"], tool="tool_x")

    class _Unlistable:
        def __iter__(self):
            raise TypeError("not a list")

    audit.log_seed_reactivated_outside_approval(_Unlistable(), tool="tool_x")


def test_the_line_is_one_bare_json_object_on_stdout_with_a_severity(capsys):
    """A bare JSON line is jsonPayload in Cloud Logging, at the severity it names (review P3); a `[ts] LEVEL - ...`
    line is textPayload at DEFAULT, which a severity or jsonPayload filter misses."""
    audit.log_seed_reactivated_outside_approval(["seed_a"], tool="tool_x")
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("{")
    line = json.loads(out[0])
    assert line["severity"] == "WARNING" and line["event"] == audit.EVENT and line["seed_id"] == "seed_a"
    assert audit._logger.propagate is False, "never a second, text copy through another handler"


# ---------------------------------------------------------------- withdraw_catalog_rows --revert


def _withdraw_revert(monkeypatch):
    import scripts.withdraw_catalog_rows as withdraw
    from tests.test_withdraw_catalog_rows import _FakeDB, _ours, _patch

    db = _FakeDB({"ck_promo": True})
    _patch(monkeypatch, db)
    out = asyncio.run(withdraw._revert([_ours(seed_ids=("seed_a", "seed_b"))]))
    return withdraw, db, out


def test_withdraw_revert_logs_each_reactivated_seed(monkeypatch, recorder):
    withdraw, _, _ = _withdraw_revert(monkeypatch)
    lines = _lines_for(recorder, withdraw.SCRIPT_NAME)
    assert [(l["seed_id"], l["previous_status"], l["row_confirmed"]) for l in lines] == [
        ("seed_a", "inactive", False), ("seed_b", "inactive", False)]


def test_withdraw_revert_is_unchanged_when_logging_fails(monkeypatch, recorder):
    _, db_ok, out_ok = _withdraw_revert(monkeypatch)
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    _, db_fail, out_fail = _withdraw_revert(monkeypatch)
    assert db_fail.executed == db_ok.executed and out_fail == out_ok


# ---------------------------------------------------------------- remediate_unpublished_crawl_rows --revert


def _remediate_revert(monkeypatch, prior="active"):
    import scripts.remediate_unpublished_crawl_rows as remediate
    from tests.test_remediate_unpublished_crawl_rows import _FakeDB, _patch, _row

    db = _FakeDB({"ck_1": True})
    _patch(monkeypatch, db)
    row = _row("seed_r", "https://shop-a.example/products/a")
    row["suppression_metadata"] = {"script": remediate.SCRIPT_NAME, "prior_seed_status": prior}
    out = asyncio.run(remediate._revert([row]))
    return remediate, db, out


def test_remediate_revert_logs_the_reactivated_seed_and_only_that(monkeypatch, recorder):
    remediate, _, _ = _remediate_revert(monkeypatch)
    (line,) = _lines_for(recorder, remediate.SCRIPT_NAME)
    assert (line["seed_id"], line["previous_status"], line["row_confirmed"]) == ("seed_r", "inactive", False)
    recorder.lines.clear()
    _remediate_revert(monkeypatch, prior="inactive")
    assert recorder.lines == [], "a seed the revert leaves dead is not logged as reactivated"


def test_remediate_revert_is_unchanged_when_logging_fails(monkeypatch, recorder):
    _, db_ok, out_ok = _remediate_revert(monkeypatch)
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    _, db_fail, out_fail = _remediate_revert(monkeypatch)
    assert db_fail.executed == db_ok.executed and out_fail == out_ok


# ---------------------------------------------------------------- repair_same_title_listings (its revert)


class _RepairDB:
    def __init__(self):
        self.calls: List[Any] = []

    async def fetch_all(self, query, values=None):
        self.calls.append((" ".join(query.split()), dict(values or {})))
        return [{"id": (values or {}).get("id")}]


def _repair_reverse():
    import scripts.repair_same_title_listings as repair

    db = _RepairDB()
    manifest = {"run_id": "run_1", "groups": [{
        "product_key": "pk_1", "repoint": None, "restore": [], "suppress": [], "suppress_prior_metadata": {},
        "deactivate": ["seed_s"], "deactivate_prior_updated_at": {"seed_s": "2026-11-01T00:00:00+00:00"},
    }]}
    out = asyncio.run(repair._reverse(db, manifest))
    return db, out


def test_repair_same_title_revert_logs_the_reactivated_seed(recorder):
    _, out = _repair_reverse()
    (line,) = _lines_for(recorder, "repair_same_title_listings")
    assert (line["seed_id"], line["previous_status"], line["row_confirmed"]) == ("seed_s", "inactive", True)
    assert out == {"reactivated": 1}


def test_repair_same_title_revert_is_unchanged_when_logging_fails(monkeypatch, recorder):
    db_ok, out_ok = _repair_reverse()
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    db_fail, out_fail = _repair_reverse()
    assert db_fail.calls == db_ok.calls and out_fail == out_ok


# ---------------------------------------------------------------- identity_resolution revert_run / revert_proposal


class _Conn:
    """An asyncpg-shaped connection answering each statement of the two reverts by identity."""

    def __init__(self, mod):
        self.mod = mod
        self.calls: List[Any] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        m = self.mod
        if sql is m.RUN_EVENTS_SQL:
            return [{"detail": json.dumps({"deactivated_seed_ids": ["seed_i1", "seed_i2"]}), "proposal_id": None}]
        if sql is m.REACTIVATE_SEEDS_SQL:
            return [{"id": s} for s in args[0] if s != "seed_i2"]  # seed_i2 was no longer inactive
        if sql in (m.PROPOSAL_SUPPRESSED_ROWS_SQL, m.REVERT_PROPOSAL_ROWS_SQL):
            return [{"product_key": "pk_dup"}]
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        m = self.mod
        if sql is m.PROPOSAL_FOR_REVERT_SQL:
            return {"status": "applied", "run_id": "run_a", "kind": "suppress_dup"}
        if sql is m.PROPOSAL_APPLIED_EVENT_SQL:
            return {"detail": {"suppressed": 1, "deactivated_seed_ids": ["seed_p"]}}
        return None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"


def _identity(which):
    from services import identity_resolution as mod

    conn = _Conn(mod)
    if which == "run":
        out = asyncio.run(mod.revert_run(conn, "run_a"))
    else:
        out = asyncio.run(mod.revert_proposal(conn, "prop_1", dry_run=False, revert_run_id="rv_1"))
    return conn, out


@pytest.mark.parametrize("which,tool,expected", [
    ("run", "identity_resolution.revert_run", ["seed_i1"]),
    ("proposal", "identity_resolution.revert_proposal", ["seed_p"]),
])
def test_identity_reverts_log_exactly_the_seeds_they_reactivated(recorder, which, tool, expected):
    _, out = _identity(which)
    lines = _lines_for(recorder, tool)
    assert [l["seed_id"] for l in lines] == expected == out["reactivated_seeds"]
    assert all(l["previous_status"] == "inactive" and l["row_confirmed"] for l in lines)


@pytest.mark.parametrize("which", ["run", "proposal"])
def test_identity_reverts_are_unchanged_when_logging_fails(monkeypatch, recorder, which):
    conn_ok, out_ok = _identity(which)
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    conn_fail, out_fail = _identity(which)
    assert conn_fail.calls == conn_ok.calls and out_fail == out_ok


# ---------------------------------------------------------------- the lane-4 mirror-seed deactivation revert


#: The lane-4 mirror-seed deactivation script, found by its pattern (its file name names a store).
_LANE4_PATH = next((Path(__file__).resolve().parents[1] / "scripts").glob("deactivate_lane4_*_mirror_seeds.py"))
_LANE4_REL = f"scripts/{_LANE4_PATH.name}"


class _Lane4DB:
    def __init__(self):
        self.calls: List[Any] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch_one(self, sql, values=None):
        self.calls.append((sql, dict(values or {})))
        return {"id": values["id"]}


def _lane4_revert(prior_status="active"):
    import importlib

    lane4 = importlib.import_module(f"scripts.{_LANE4_PATH.stem}")

    db = _Lane4DB()
    out = asyncio.run(lane4.revert(db, {"run_id": "run_l", "seeds": [
        {"id": "seed_l1", "attached_product_key": "pk_l", "prior_status": prior_status}]}))
    return db, out


def test_the_lane4_revert_logs_a_seed_it_put_back_live(recorder):
    _lane4_revert()
    (line,) = _lines_for(recorder, "lane4_mirror_seed_deactivation")
    assert (line["seed_id"], line["previous_status"], line["row_confirmed"]) == ("seed_l1", "inactive", True)
    recorder.lines.clear()
    _lane4_revert(prior_status="disabled")
    assert recorder.lines == [], "a seed restored to a status that is not live is not a reactivation"


def test_the_lane4_revert_is_unchanged_when_logging_fails(monkeypatch, recorder):
    db_ok, out_ok = _lane4_revert()
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    db_fail, out_fail = _lane4_revert()
    assert db_fail.calls == db_ok.calls and out_fail == out_ok


# ---------------------------------------------------------------- retire_superseded_brand_keys revert


from tests.test_retire_superseded_brand_keys_trust_refresh import RevertDB, _manifest, trust  # noqa: E402,F401


async def test_the_retire_revert_logs_a_seed_it_put_back_live(monkeypatch, recorder, trust):  # noqa: F811
    from scripts import retire_superseded_brand_keys as tool

    monkeypatch.setattr(tool, "database", RevertDB(ours={"k_ours"}))
    await tool.revert_manifest(_manifest(["k_ours"]))
    (line,) = _lines_for(recorder, "retire_superseded_brand_keys")
    assert (line["seed_id"], line["previous_status"], line["row_confirmed"]) == ("s1", "unknown", False)
    recorder.lines.clear()
    monkeypatch.setattr(tool, "database", RevertDB(ours=()))
    await tool.revert_manifest(_manifest(["k_ours"]))
    assert recorder.lines == [], "no row restored: its seed cannot have come back"


async def test_the_retire_revert_is_unchanged_when_logging_fails(monkeypatch, recorder, trust, capsys):  # noqa: F811
    from scripts import retire_superseded_brand_keys as tool

    db_ok = RevertDB(ours={"k_ours"})
    monkeypatch.setattr(tool, "database", db_ok)
    await tool.revert_manifest(_manifest(["k_ours"]))
    out_ok = capsys.readouterr().out
    monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    db_fail = RevertDB(ours={"k_ours"})
    monkeypatch.setattr(tool, "database", db_fail)
    await tool.revert_manifest(_manifest(["k_ours"]))
    assert db_fail.events == db_ok.events and capsys.readouterr().out == out_ok


# ---------------------------------------------------------------- the re-ingest upserts


class _StatusDB:
    """Answers the pre-upsert status read with `prior`, records every write."""

    is_connected = True

    def __init__(self, prior: Dict[str, str], fail_read: bool = False):
        self.prior, self.fail_read = prior, fail_read
        self.calls: List[Any] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch_all(self, sql, values=None):
        if "SELECT id, status FROM external_product_seeds" in sql:
            if self.fail_read:
                raise RuntimeError("read failed")
            return [{"id": v, "status": self.prior[v]} for v in (values or {}).values() if v in self.prior]
        return []

    async def fetch_one(self, sql, values=None):
        self.calls.append(("fetch_one", sql, dict(values or {})))
        return None

    async def fetch_val(self, sql, values=None):
        return None

    async def execute(self, sql, values=None):
        self.calls.append(("execute", sql, dict(values or {})))


def _onboard(monkeypatch, db):
    import scripts.onboard_external_brand_from_crawl as onboard

    monkeypatch.setattr(onboard, "database", db)
    asyncio.run(onboard._upsert_seed(
        {"external_product_id": "ext_onb_1", "title": "Placeholder serum", "price_amount": 20.0, "market": "US",
         "price_currency": "USD"}, seller_ref=None, seed_kind=None))


@pytest.mark.parametrize("prior,logged", [("inactive", True), ("disabled", True), ("active", False), (None, False)])
def test_the_crawl_onboarding_upsert_logs_a_revived_seed(monkeypatch, recorder, prior, logged):
    import scripts.onboard_external_brand_from_crawl as onboard

    seed_id = onboard._seed_id("ext_onb_1")
    _onboard(monkeypatch, _StatusDB({seed_id: prior} if prior else {}))
    lines = _lines_for(recorder, "onboard_external_brand_from_crawl")
    assert [(x["seed_id"], x["previous_status"]) for x in lines] == ([(seed_id, prior)] if logged else [])


@pytest.mark.parametrize("failure", ["read", "log", "no_read_api"])
def test_the_crawl_onboarding_upsert_is_unchanged_when_the_audit_fails(monkeypatch, recorder, failure):
    import scripts.onboard_external_brand_from_crawl as onboard

    seed_id = onboard._seed_id("ext_onb_1")
    db_ok = _StatusDB({seed_id: "inactive"})
    _onboard(monkeypatch, db_ok)
    if failure == "log":
        monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
        db_fail = _StatusDB({seed_id: "inactive"})
    elif failure == "read":
        db_fail = _StatusDB({seed_id: "inactive"}, fail_read=True)
    else:
        from tests.test_onboard_external_brand_seed_data_merge import _FakeDB

        db_fail = _FakeDB()  # no fetch_all, no transaction: the read fails and is swallowed
    _onboard(monkeypatch, db_fail)
    # The same statements with the same parameters (the seed_data carries the write's own timestamp, so it is
    # compared without that one value).
    def shape(calls):
        out = []
        for c in calls:
            sql, values = (c[1], c[2]) if len(c) == 3 else (c[0], c[1])
            out.append((" ".join(sql.split()), sorted(k for k in values if k != "data"),
                        {k: v for k, v in values.items() if k != "data"}))
        return out

    assert shape(db_fail.calls) == shape(db_ok.calls)


def _enrichment(monkeypatch, db, *, batched):
    import services.catalog_enrichment_agent.apply as apply
    from tests.test_external_seed_canonical_writers import SIBLING, _plan_seed

    written: List[Dict[str, Any]] = []

    async def bulk_upsert(database, sql, rows, label=None, **_k):
        if sql is apply._SEED_UPSERT_SQL:
            written.extend(dict(r) for r in rows)
        return len(rows), 0, []

    monkeypatch.setattr(apply, "_derive_seed_seller_for_plan_row", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(apply, "bulk_upsert", bulk_upsert)
    monkeypatch.setattr(apply, "write_writer_audit_log", AsyncMock(return_value=None))
    plan = {"seeds": [_plan_seed(SIBLING)]}
    if batched:
        out = asyncio.run(apply._apply_ingest_plan_batched(plan, batch_label="t", database=db))
    else:
        out = asyncio.run(apply._apply_ingest_plan(plan, batch_label="t", db=db))
        written.extend(c[2] for c in db.calls if c[0] == "execute" and c[1] is apply._SEED_UPSERT_SQL)
    return written, out


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("prior,logged", [("inactive", True), ("active", False), (None, False)])
def test_the_enrichment_upsert_logs_a_planned_active_over_a_seed_taken_down(monkeypatch, recorder, batched, prior,
                                                                           logged):
    from tests.test_external_seed_canonical_writers import SIBLING, _plan_seed

    seed_id = _plan_seed(SIBLING)["id"]
    _enrichment(monkeypatch, _StatusDB({seed_id: prior} if prior else {}), batched=batched)
    lines = _lines_for(recorder, "catalog_enrichment_agent")
    assert [(x["seed_id"], x["previous_status"]) for x in lines] == ([(seed_id, prior)] if logged else [])


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("failure", ["read", "log"])
def test_the_enrichment_upsert_is_unchanged_when_the_audit_fails(monkeypatch, recorder, batched, failure):
    from tests.test_external_seed_canonical_writers import SIBLING, _plan_seed

    seed_id = _plan_seed(SIBLING)["id"]
    written_ok, out_ok = _enrichment(monkeypatch, _StatusDB({seed_id: "inactive"}), batched=batched)
    if failure == "log":
        monkeypatch.setattr(audit, "_logger", _Recorder(fail=True))
    db = _StatusDB({seed_id: "inactive"}, fail_read=failure == "read")
    written_fail, out_fail = _enrichment(monkeypatch, db, batched=batched)
    assert written_fail == written_ok and out_fail == out_ok


# ---------------------------------------------------------------- every status write that can make a seed live


#: Every SQL status assignment on external_product_seeds that can set `active` (a literal 'active', a bound
#: `:status`, `EXCLUDED.status`: anything but a literal status that is not live), by file, with what covers it (the
#: reactivation audit, or the staff publish step) and how many such assignments the file has. A new one fails here.
_MAY_ACTIVATE_SITES = {
    "routes/employee_products.py": ("_publish_seed(", 1),  # the approve's publish UPDATE, run by _publish_seed
    "services/catalog_enrichment_agent/apply.py": ("log_upsert_revivals(", 1),
    "services/identity_resolution.py": ("log_seed_reactivated_outside_approval(", 1),
    _LANE4_REL: ("log_seed_reactivated_outside_approval(", 1),
    "scripts/onboard_external_brand_from_crawl.py": ("log_upsert_revivals(", 2),  # the SET, and its SQL comment
    "scripts/remediate_unpublished_crawl_rows.py": ("log_seed_reactivated_outside_approval(", 1),
    "scripts/repair_same_title_listings.py": ("log_seed_reactivated_outside_approval(", 2),  # forward + revert SQL
    "scripts/retire_superseded_brand_keys.py": ("log_seed_reactivated_outside_approval(", 1),
    "scripts/withdraw_catalog_rows.py": ("log_seed_reactivated_outside_approval(", 1),
}
_NOT_LIVE = re.compile(r"^'(inactive|disabled|draft|paused|retired|deleted)'$", re.I)
_SEED_STATEMENT = re.compile(
    r"(UPDATE\s+(?:public\.)?external_product_seeds\b(?:\s+(?:AS\s+)?\w+)?\s+SET\b.*?(?:\bWHERE\b|\"\"\"|'''|\bRETURNING\b)"
    r"|INSERT\s+INTO\s+(?:public\.)?external_product_seeds\b.*?(?:\"\"\"|'''))",
    re.S | re.I,
)


def _may_activate_assignments(text: str) -> List[str]:
    """Each status assignment in an UPDATE ... SET, or an INSERT's ON CONFLICT ... DO UPDATE SET, on the seeds table,
    whatever its position, alias, schema prefix or case, that does not set a literal non-live status."""
    out = []
    for m in _SEED_STATEMENT.finditer(text):
        body = m.group(0)
        if body.upper().startswith("INSERT"):
            if "ON CONFLICT" not in body.upper():
                continue
            body = body[body.upper().index("ON CONFLICT"):]
        else:
            body = re.split(r"\bWHERE\b", body, flags=re.I)[0]
        for a in re.finditer(r"\b(?:\w+\.)?status\s*=\s*([^,\n)]+)", body, re.I):
            value = a.group(1).strip()
            if not _NOT_LIVE.match(value.split()[0] if value else ""):
                out.append(value)
    return out


@pytest.mark.parametrize("sql,flagged", [
    ("UPDATE external_product_seeds SET status = 'active' WHERE id = :id", True),
    ("UPDATE external_product_seeds SET updated_at = NOW(), status='active' WHERE id = :id", True),
    ("UPDATE external_product_seeds SET status = :status WHERE id = :id", True),
    ("UPDATE public.external_product_seeds AS s SET s.status = 'active' WHERE s.id = :id", True),
    ("update external_product_seeds set status = 'active' where id = :id", True),
    ("INSERT INTO external_product_seeds (id) VALUES (:id) ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status'''",
     True),
    ("UPDATE external_product_seeds SET status = 'inactive' WHERE id = :id", False),
    ("UPDATE external_product_seeds SET status = 'disabled', notes = :n WHERE id = :id", False),
])
def test_the_ratchet_reads_every_shape_the_review_listed(sql, flagged):
    assert bool(_may_activate_assignments(sql)) is flagged


def test_every_status_write_that_can_make_a_seed_live_is_logged_or_is_the_publish_step():
    """Widened (review P3): any position, a bound or EXCLUDED value, an ON CONFLICT arm, an alias or schema prefix,
    over routes/ too. SQL assembled in Python (the PATCH and CSV set lists) is the staff publish step's own code and
    is pinned by its tests; an ORM write would not be seen here."""
    repo = Path(__file__).resolve().parents[1]
    found = {}
    for folder in ("routes", "services", "scripts", "jobs", "db"):
        for path in sorted((repo / folder).rglob("*.py")):
            values = _may_activate_assignments(path.read_text(errors="replace"))
            if values:
                found[path.relative_to(repo).as_posix()] = values
    assert set(found) == set(_MAY_ACTIVATE_SITES), {k: v for k, v in found.items() if k not in _MAY_ACTIVATE_SITES}
    for rel, (helper, statements) in _MAY_ACTIVATE_SITES.items():
        assert len(found[rel]) == statements, (rel, found[rel])
        assert helper in (repo / rel).read_text(), f"{rel} sets a seed live without {helper}"


def test_every_operator_reactivation_site_calls_the_audit_helper():
    """Each reactivation the tools do logs: the reverts once per statement, the re-ingest upserts after each write."""
    repo = Path(__file__).resolve().parents[1]
    calls = {
        "scripts/withdraw_catalog_rows.py": 1, "scripts/remediate_unpublished_crawl_rows.py": 1,
        "scripts/repair_same_title_listings.py": 1, "services/identity_resolution.py": 2,
        _LANE4_REL: 1, "scripts/retire_superseded_brand_keys.py": 1,
    }
    for rel, n in calls.items():
        assert (repo / rel).read_text().count("log_seed_reactivated_outside_approval(") == n, rel
    upserts = {"scripts/onboard_external_brand_from_crawl.py": 1, "services/catalog_enrichment_agent/apply.py": 2}
    for rel, n in upserts.items():
        assert (repo / rel).read_text().count("log_upsert_revivals(") == n, rel
