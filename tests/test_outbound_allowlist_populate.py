"""The outbound allowlist populate job (services/outbound_allowlist_populate.py), on a throwaway database.

Fixtures use example.test hosts only. A populated entry must match what the mint checks
(outbound_links_service.is_destination_domain_allowed), the plan never writes, apply is insert-only and tagged with
its batch, rollback deletes only that batch, and a re-run changes nothing.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, List

import pytest

from services import outbound_allowlist_populate as core
from services.outbound_links_service import is_destination_domain_allowed

DDL = [
    """CREATE TABLE outbound_link_rules (id TEXT PRIMARY KEY, market TEXT NOT NULL, destination_url TEXT NOT NULL,
       status TEXT NOT NULL)""",
    """CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, market TEXT NOT NULL, canonical_url TEXT,
       destination_url TEXT, status TEXT NOT NULL DEFAULT 'active')""",
    """CREATE TABLE outbound_link_allowed_domains (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL,
       domain TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', notes TEXT, created_by TEXT,
       created_at TIMESTAMP, updated_at TIMESTAMP)""",
    "CREATE UNIQUE INDEX idx_allow_market_domain ON outbound_link_allowed_domains (market, domain)",
]

RULES = [
    ("r1", "US", "https://shop.store-one.test/p/1", "published"),
    ("r2", "US", "https://www.store-one.test/p/2", "published"),  # same registrable domain: one entry
    ("r3", "SG", "https://sg.store-two.test/c", "published"),  # already present for SG
    ("r4", "US", "https://draft.store-three.test/x", "draft"),  # not published: not a source
    ("r5", "USA", "https://store-four.test/x", "published"),  # unlisted key
    ("r6", "US", "http://store-five.test/x", "published"),  # non-https
]
SEEDS = [
    ("s1", "US", "https://brand-a.test/products/a", "https://brand-a.test/products/a?v=1", "active"),  # one row, one entry
    ("s2", "US", None, "https://checkout.brand-b.test/cart", "active"),
    ("s3", "US", "https://10.0.0.7/p", None, "active"),  # IP host
    ("s4", "US", "not a url", None, "active"),  # unparsable
    ("s5", "*", "https://brand-star.test/p", None, "active"),  # unlisted partition
    ("s6", "US", "https://brand-gone.test/p", None, "disabled"),  # not active: not a source
    ("s7", "ſg", "https://brand-lookalike.test/p", None, "active"),  # look-alike key: unlisted
    ("s8", "GB", "https://tenant.myshopify.com/p", None, "active"),  # shared platform: the tenant host
]


class Recording:
    """A databases.Database that records every statement, to show what the plan does not do."""

    def __init__(self, db):
        self.db, self.statements = db, []
        self.url = db.url

    def transaction(self):
        return self.db.transaction()

    async def fetch_all(self, q, v=None):
        self.statements.append(str(q)); return await self.db.fetch_all(q, v)

    async def fetch_one(self, q, v=None):
        self.statements.append(str(q)); return await self.db.fetch_one(q, v)

    async def execute(self, q, v=None):
        self.statements.append(str(q)); return await self.db.execute(q, v)


@pytest.fixture
def db():
    import databases

    path = tempfile.mktemp(suffix=".sqlite", dir=tempfile.mkdtemp())
    database = databases.Database(f"sqlite+aiosqlite:///{path}")

    async def setup():
        await database.connect()
        for stmt in DDL:
            await database.execute(stmt)
        for r in RULES:
            await database.execute("INSERT INTO outbound_link_rules VALUES (:a,:b,:c,:d)", dict(zip("abcd", r)))
        for s in SEEDS:
            await database.execute("INSERT INTO external_product_seeds VALUES (:a,:b,:c,:d,:e)", dict(zip("abcde", s)))
        await database.execute(
            "INSERT INTO outbound_link_allowed_domains (market, domain, status, created_by) VALUES ('SG','store-two.test','active','emp_1')")

    asyncio.run(setup())
    loop = asyncio.new_event_loop()
    yield database, loop
    loop.close()
    asyncio.run(database.disconnect())
    os.remove(path)


def run(db_fixture, coro_fn):
    database, _ = db_fixture
    return asyncio.run(coro_fn(database))


async def _rows(db) -> List[tuple]:
    return sorted((dict(r)["market"], dict(r)["domain"], dict(r)["status"], dict(r)["created_by"])
                  for r in await db.fetch_all("SELECT market, domain, status, created_by FROM outbound_link_allowed_domains"))


# --- normalisation: the entry is what the mint checks ------------------------------------------------------


@pytest.mark.parametrize("url, entry", [
    ("https://shop.example.test/p/1", "example.test"),
    ("https://www.shop.example.test/p", "example.test"),
    ("https://A.B.Example.Test:8443/x?y=1", "example.test"),
    ("https://a.b.example.co.uk/x", "example.co.uk"),
    ("https://tenant.myshopify.com/p", "tenant.myshopify.com"),
])
def test_an_entry_matches_the_mint_time_check_for_its_host_and_subdomains(url, entry) -> None:
    got, reason = core.derive_entry(url)
    assert (got, reason) == (entry, None)
    assert is_destination_domain_allowed(destination_url=url, allowed_domains=[got]) is True
    assert is_destination_domain_allowed(destination_url="https://unrelated.example-other.test/x", allowed_domains=[got]) is False


@pytest.mark.parametrize("url, reason", [
    (None, "unparsable_url"), ("", "unparsable_url"), ("not a url", "unparsable_url"), ("https://", "unparsable_url"),
    ("http://shop.example.test/x", "non_https"), ("ftp://shop.example.test/x", "non_https"),
    ("https://10.0.0.7/x", "ip_host"), ("https://[::1]/x", "ip_host"),
    ("https://localhost/x", "invalid_host"), ("https://xn--bcher-kva.example.test/x", None), ("https://bücher.example.test/x", "invalid_host"),
])
def test_each_skip_reason(url, reason) -> None:
    got, why = core.derive_entry(url)
    assert why == reason
    if reason is None:
        assert got


# --- plan: dedupe, scope, skips, and it never writes -----------------------------------------------------


def test_the_plan_dedupes_scopes_and_names_every_skip(db) -> None:
    plan = run(db, lambda d: core.read_plan(d, batch_id="olp-test-1"))
    assert [(e["market"], e["domain"], e["rules"], e["seeds"]) for e in plan["entries"]] == [
        ("GB", "tenant.myshopify.com", 0, 1),
        ("US", "brand-a.test", 0, 1),
        ("US", "brand-b.test", 0, 1),
        ("US", "store-one.test", 2, 0),
    ]
    skips = {(s["market"], s["reason"]): s["count"] for s in plan["skips"]}
    assert skips == {
        ("-", "unlisted_market"): 3,  # USA rule, '*' seed, look-alike seed
        ("SG", "already_present"): 1,
        ("US", "ip_host"): 1,
        ("US", "non_https"): 1,
        ("US", "unparsable_url"): 1,
    }
    assert plan["status"] == "disabled"
    assert plan["plan_sha256"] == core.verify_plan(plan)["plan_sha256"]


def test_the_plan_never_writes(db) -> None:
    database, _ = db

    async def go(d):
        rec = Recording(d)
        before = await _rows(d)
        plan = await core.read_plan(rec, batch_id="olp-test-1")
        assert await _rows(d) == before
        return plan, rec.statements

    plan, statements = asyncio.run(go(database))
    assert statements and all(s.lstrip().upper().startswith(("SELECT", "SET")) for s in statements), statements
    lines = core.plan_lines(plan)
    assert lines[0].startswith("OLP plan batch=olp-test-1 markets=2 domains=4 status=disabled plan_sha256=")
    assert "OLP market=US domains=3 from_rules=1 from_seeds=2" in lines
    assert "OLP domain market=US domain=store-one.test rules=2 seeds=0" in lines
    assert "OLP skip market=US reason=non_https count=1" in lines
    assert all(line.startswith("OLP ") for line in lines)
    assert not [line for line in lines if "://" in line or "@" in line], "no URL, no address in the report"


# --- apply / rollback / re-run --------------------------------------------------------------------------


def test_apply_is_insert_only_staged_and_tagged_and_rollback_deletes_only_its_batch(db) -> None:
    database, _ = db

    async def go(d):
        plan = await core.read_plan(d, batch_id="olp-test-1")
        receipt = await core.apply_plan(d, plan)
        after_apply = await _rows(d)
        # An unrelated row added after the apply (another writer) must survive the rollback.
        await d.execute("INSERT INTO outbound_link_allowed_domains (market, domain, status, created_by) VALUES ('JP','other.test','active','emp_2')")
        result = await core.rollback_batch(d, receipt)
        return receipt, after_apply, result, await _rows(d)

    receipt, after_apply, result, after_rollback = asyncio.run(go(database))
    tag = "allowlist_populate:olp-test-1"
    assert receipt["inserted"] == 4 and receipt["per_market"] == {"GB": 1, "US": 3}
    assert core.receipt_line(receipt).startswith("OLP receipt batch=olp-test-1 inserted=4 per_market=GB:1,US:3 receipt_sha256=")
    assert after_apply == [
        ("GB", "tenant.myshopify.com", "disabled", tag),
        ("SG", "store-two.test", "active", "emp_1"),
        ("US", "brand-a.test", "disabled", tag),
        ("US", "brand-b.test", "disabled", tag),
        ("US", "store-one.test", "disabled", tag),
    ], "the existing row is untouched; new rows are staged (disabled) and tagged"
    assert result["deleted"] == 4
    assert after_rollback == [
        ("JP", "other.test", "active", "emp_2"),
        ("SG", "store-two.test", "active", "emp_1"),
    ]


def test_a_rerun_is_idempotent(db) -> None:
    database, _ = db

    async def go(d):
        plan = await core.read_plan(d, batch_id="olp-test-1")
        await core.apply_plan(d, plan)
        once = await _rows(d)
        with pytest.raises(core.Refused, match="already present"):
            await core.apply_plan(d, plan)  # the same plan again: refused, nothing written
        replan = await core.read_plan(d, batch_id="olp-test-2")
        receipt = await core.apply_plan(d, replan)  # a fresh plan finds everything present: inserts nothing
        return once, replan, receipt, await _rows(d)

    once, replan, receipt, twice = asyncio.run(go(database))
    assert replan["entries"] == []
    assert {(s["market"], s["reason"]) for s in replan["skips"]} >= {("US", "already_present"), ("GB", "already_present")}
    assert receipt["inserted"] == 0
    assert twice == once


def test_apply_refuses_a_tampered_or_drifted_plan_and_writes_nothing(db) -> None:
    database, _ = db

    async def go(d):
        plan = await core.read_plan(d, batch_id="olp-test-1")
        tampered = {**plan, "entries": plan["entries"] + [{"market": "US", "domain": "store-nine.test", "rules": 0, "seeds": 0}]}
        with pytest.raises(core.Refused, match="its own sha"):
            await core.apply_plan(d, tampered)
        await d.execute("INSERT INTO outbound_link_allowed_domains (market, domain, status, created_by) VALUES ('US','brand-a.test','active','emp_3')")
        before = await _rows(d)
        with pytest.raises(core.Refused, match="already present"):
            await core.apply_plan(d, plan)
        return before, await _rows(d)

    before, after = asyncio.run(go(database))
    assert before == after


def test_rollback_refuses_when_the_batch_no_longer_matches_its_receipt(db) -> None:
    database, _ = db

    async def go(d):
        receipt = await core.apply_plan(d, await core.read_plan(d, batch_id="olp-test-1"))
        await d.execute("DELETE FROM outbound_link_allowed_domains WHERE domain = 'tenant.myshopify.com'")
        before = await _rows(d)
        with pytest.raises(core.Refused, match="no longer match"):
            await core.rollback_batch(d, receipt)
        forged = {**receipt, "inserted": 1}
        with pytest.raises(core.Refused):
            await core.rollback_batch(d, forged)
        return before, await _rows(d)

    before, after = asyncio.run(go(database))
    assert before == after


# --- the job: plan by default, writes only with the flag and a pinned approval ----------------------------


def test_the_job_refuses_outside_the_reviewed_production_job(monkeypatch, capsys) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    for name in ("CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION", "PIVOTA_ENV", job.WRITE_FLAG):
        monkeypatch.delenv(name, raising=False)
    assert asyncio.run(job.main([])) == 2
    assert asyncio.run(job.main(["apply"])) == 2
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(line.startswith("OLP refused mode=") for line in lines)


@pytest.mark.parametrize("mode, flag, ok", [("plan", None, True), ("plan", "1", False), ("apply", None, False),
                                            ("apply", "1", True), ("rollback", None, False), ("rollback", "1", True)])
def test_the_write_flag_is_set_for_apply_and_rollback_only(monkeypatch, tmp_path, mode, flag, ok) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    sha = "a" * 40
    image = tmp_path / ".image_commit_sha"
    image.write_text(sha)
    real_path = job.Path

    def fake_path(p):
        return image if str(p) == "/app/.image_commit_sha" else real_path(p)

    monkeypatch.setattr(job, "Path", fake_path)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("CLOUD_RUN_JOB", "reviewed-allowlist-populate-1")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "reviewed-allowlist-populate-1-abc")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("DB_AUTO_MIGRATE", "false")
    monkeypatch.setenv(job.RELEASE_COMMIT_ENV, sha)
    if flag is None:
        monkeypatch.delenv(job.WRITE_FLAG, raising=False)
    else:
        monkeypatch.setenv(job.WRITE_FLAG, flag)
    if ok:
        assert job.check_environment(mode) == sha
    else:
        with pytest.raises(core.Refused):
            job.check_environment(mode)


def test_apply_requires_an_approval_pinned_to_this_plan_and_release(monkeypatch, tmp_path) -> None:
    import json
    from hashlib import sha256

    from scripts.ops import outbound_allowlist_populate as job

    approval = {"mode": "apply", "plan_sha256": "p" * 64, "release_sha": "r" * 40, "go_reference": "peng go"}
    path = tmp_path / "approval.json"
    path.write_bytes(json.dumps(approval).encode())
    monkeypatch.setenv(job.ENV + "APPROVAL_PATH", str(path))
    monkeypatch.setenv(job.ENV + "APPROVAL_SHA256", sha256(path.read_bytes()).hexdigest())
    assert job.check_approval("apply", release="r" * 40, pinned_key="plan_sha256", pinned_sha="p" * 64)["go_reference"]
    for kwargs in ({"release": "x" * 40}, {"pinned_sha": "q" * 64}):
        args = {"release": "r" * 40, "pinned_key": "plan_sha256", "pinned_sha": "p" * 64, **kwargs}
        with pytest.raises(core.Refused):
            job.check_approval("apply", **args)
    with pytest.raises(core.Refused):
        job.check_approval("rollback", release="r" * 40, pinned_key="plan_sha256", pinned_sha="p" * 64)
    monkeypatch.setenv(job.ENV + "APPROVAL_SHA256", "0" * 64)
    with pytest.raises(core.Refused, match="SHA-256 pin"):
        job.check_approval("apply", release="r" * 40, pinned_key="plan_sha256", pinned_sha="p" * 64)


def test_the_plan_doc_line_round_trips(db) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    plan = run(db, lambda d: core.read_plan(d, batch_id="olp-test-1"))
    line = job.doc_line("plan_doc", plan)
    assert line.startswith("OLP plan_doc ")
    assert job.parse_doc_line("noise\n" + line + "\n", "plan_doc") == plan
