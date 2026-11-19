"""The outbound allowlist populate job (services/outbound_allowlist_populate.py), on a throwaway sqlite database.

Fixtures use .test hosts only. A staged batch is invisible to the real allowlist reader; an entry matches what the
mint checks; the plan never writes; apply is insert-only and tagged with its batch; rollback deletes only that batch
and only while it is still staged; a re-run changes nothing. (The Postgres-only guards are in the _postgres twin.)
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any, List

import pytest

from services import outbound_allowlist_populate as core
from services.outbound_links_service import is_destination_domain_allowed

LOCAL = {"require_postgres": False}  # sqlite here; production refuses anything but Postgres (tested below)

DDL = [
    """CREATE TABLE outbound_link_rules (id TEXT PRIMARY KEY, market TEXT NOT NULL, destination_url TEXT NOT NULL,
       status TEXT NOT NULL)""",
    """CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, market TEXT NOT NULL, canonical_url TEXT,
       destination_url TEXT, domain TEXT, seed_data TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active')""",
    """CREATE TABLE outbound_link_allowed_domains (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL,
       domain TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', notes TEXT, created_by TEXT,
       created_at TIMESTAMP, updated_at TIMESTAMP)""",
    "CREATE UNIQUE INDEX idx_allow_market_domain ON outbound_link_allowed_domains (market, domain)",
]

RULES = [
    ("r1", "US", "https://store-one.test/p/1", "published"),
    ("r2", "US", "https://www.store-one.test/p/2", "published"),  # www + registrable: one entry, widened
    ("r3", "SG", "https://sg.store-two.test/c", "published"),  # already present for SG (exact host)
    ("r4", "US", "https://draft.store-three.test/x", "draft"),  # not published: not a source
    ("r5", "USA", "https://store-four.test/x", "published"),  # unlisted key
    ("r6", "US", "http://store-five.test/x", "published"),  # non-https
    ("r7", "NZ", "https://store-six.test/x", "published"),  # NZ is listed but not a pricing market
]


def _sd(**kw: Any) -> str:
    return json.dumps(kw)


SEEDS = [
    # one row, two URLs on one host: one entry, counted once
    ("s1", "US", "https://brand-a.test/products/a", "https://brand-a.test/products/a?v=1", None, _sd(), "active"),
    # a deep subdomain keeps its exact host
    ("s2", "US", None, "https://checkout.brand-b.test/cart", None, _sd(), "active"),
    ("s3", "US", "https://10.0.0.7/p", None, None, _sd(), "active"),  # IP host
    ("s4", "US", "not a url", None, None, _sd(), "active"),  # unparsable
    ("s5", "*", "https://brand-star.test/p", None, None, _sd(), "active"),  # unlisted partition
    ("s6", "US", "https://brand-gone.test/p", None, None, _sd(), "disabled"),  # not active: not a source
    ("s7", "ſg", "https://brand-lookalike.test/p", None, None, _sd(), "active"),  # look-alike key: unlisted
    ("s8", "GB", "https://tenant.myshopify.com/p", None, None, _sd(), "active"),  # platform tenant: its own host
    # URLs only in seed_data / its snapshot (the mints' fallbacks), and the cart-permalink host in `domain`
    ("s9", "US", None, None, None, _sd(destination_url="https://brand-c.test/p"), "active"),
    ("s10", "US", None, None, None, _sd(snapshot={"canonical_url": "https://brand-d.test/p"}), "active"),
    ("s11", "US", "https://brand-e.test/p", None, "brand-e-shop.myshopify.com", _sd(), "active"),
    ("s12", "US", None, None, None, _sd(domain="brand-f-shop.myshopify.com"), "active"),
    ("s13", "US", None, None, None, _sd(), "active"),  # nothing at all: no_url
    ("s14", "US", "https://co.uk/p", None, None, _sd(), "active"),  # a bare public suffix: never an entry
    # a snapshot stored as a JSON STRING (the mint parses it with _ensure_json_obj)
    ("s15", "US", None, None, None, _sd(snapshot=json.dumps({"destination_url": "https://brand-g.test/p"})), "active"),
]


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
            await database.execute("INSERT INTO external_product_seeds VALUES (:a,:b,:c,:d,:e,:f,:g)", dict(zip("abcdefg", s)))
        await database.execute(
            "INSERT INTO outbound_link_allowed_domains (market, domain, status, created_by) VALUES ('SG','sg.store-two.test','active','emp_1')")

    asyncio.run(setup())
    yield database
    asyncio.run(database.disconnect())
    os.remove(path)


async def _rows(d) -> List[tuple]:
    return sorted((dict(r)["market"], dict(r)["domain"], dict(r)["status"], dict(r)["created_by"])
                  for r in await d.fetch_all("SELECT market, domain, status, created_by FROM outbound_link_allowed_domains"))


class Recording:
    def __init__(self, d):
        self.d, self.statements, self.url = d, [], d.url

    def transaction(self):
        return self.d.transaction()

    async def fetch_all(self, q, v=None):
        self.statements.append(str(q)); return await self.d.fetch_all(q, v)

    async def fetch_one(self, q, v=None):
        self.statements.append(str(q)); return await self.d.fetch_one(q, v)

    async def execute(self, q, v=None):
        self.statements.append(str(q)); return await self.d.execute(q, v)


# --- the entry rule: matches the mint, keeps the exact host when in doubt, never a public suffix -----------


@pytest.mark.parametrize("url, entry, widened", [
    ("https://store-one.test/p/1", "store-one.test", False),
    ("https://www.store-one.test/p", "store-one.test", True),
    ("https://shop.store-one.test/p", "shop.store-one.test", False),  # a deeper subdomain: its exact host
    ("https://A.B.Store-One.Test:8443/x?y=1", "a.b.store-one.test", False),
    ("https://brand.example.co.uk/x", "brand.example.co.uk", False),
    ("https://example.co.uk/x", "example.co.uk", False),
    ("https://tenant.myshopify.com/p", "tenant.myshopify.com", False),
])
def test_an_entry_matches_the_mint_check_and_keeps_the_exact_host_unless_it_is_the_registrable(url, entry, widened) -> None:
    got, host, reason = core.derive(url)
    assert (got, reason) == (entry, None)
    assert (host != got) is widened
    assert is_destination_domain_allowed(destination_url=url, allowed_domains=[got]) is True
    assert is_destination_domain_allowed(destination_url="https://unrelated.other-store.test/x", allowed_domains=[got]) is False


@pytest.mark.parametrize("url", ["https://co.uk/", "https://com.au/x", "https://myshopify.com/", "https://www.myshopify.com/",
                                 "https://shopify.com/p", "https://www.co.uk/"])
def test_a_bare_public_or_platform_suffix_is_never_an_entry(url) -> None:
    assert core.derive_entry(url) == (None, "public_suffix")


@pytest.mark.parametrize("url, reason", [
    (None, "unparsable_url"), ("", "unparsable_url"), ("not a url", "unparsable_url"), ("https://", "unparsable_url"),
    ("http://shop.store-one.test/x", "non_https"), ("ftp://shop.store-one.test/x", "non_https"),
    ("https://10.0.0.7/x", "ip_host"), ("https://[::1]/x", "ip_host"),
    ("https://shop.store-one.test./x", "trailing_dot_host"), ("https://bücher.store-one.test/x", "idn_host"),
    ("https://localhost/x", "invalid_host"), ("https://xn--bcher-kva.store-one.test/x", None),
])
def test_each_skip_reason(url, reason) -> None:
    assert core.derive_entry(url)[1] == reason


def test_redirector_marks_for_the_review() -> None:
    from services.outbound_warm_handoff import AFFILIATE_HOST_SUFFIXES

    assert core.redirector_marks("https://store-one.test/go/abc") == {"path_go"}
    assert core.redirector_marks("https://store-one.test/x?url=https%3A%2F%2Fother.test%2F") == {"query_url"}
    assert core.redirector_marks("https://store-one.test/x?u=http://other.test/") == {"query_u"}
    assert core.redirector_marks(f"https://{AFFILIATE_HOST_SUFFIXES[0]}/c/1") >= {"affiliate_host"}
    assert core.redirector_marks("https://store-one.test/products/rocket?ref=home") == set()


# --- plan ---------------------------------------------------------------------------------------------


def test_the_plan_covers_the_seed_fallbacks_and_cart_hosts_and_names_every_skip(db) -> None:
    plan = asyncio.run(core.read_plan(db, batch_id="olp-test-1", **LOCAL))
    assert [(e["market"], e["domain"], e["rules"], e["seeds"], e["hosts"], e["marks"]) for e in plan["entries"]] == [
        ("GB", "tenant.myshopify.com", 0, 1, 1, ["apex"]),  # a curated platform's tenant is its own registrable
        ("US", "brand-a.test", 0, 1, 1, ["apex"]),
        ("US", "brand-c.test", 0, 1, 1, ["apex"]),  # seed_data.destination_url
        ("US", "brand-d.test", 0, 1, 1, ["apex"]),  # seed_data.snapshot.canonical_url
        ("US", "brand-e-shop.myshopify.com", 0, 1, 1, ["apex"]),  # the cart host in `domain`
        ("US", "brand-e.test", 0, 1, 1, ["apex"]),
        ("US", "brand-f-shop.myshopify.com", 0, 1, 1, ["apex"]),  # the cart host in seed_data.domain
        ("US", "brand-g.test", 0, 1, 1, ["apex"]),  # a snapshot stored as a JSON string
        ("US", "checkout.brand-b.test", 0, 1, 1, []),  # an exact subdomain: admits only its own subtree
        ("US", "store-one.test", 2, 0, 2, ["apex", "widened"]),
    ]
    skips = {(s["market"], s["reason"]): s["count"] for s in plan["skips"]}
    assert skips == {
        # every unlisted key on its own line (NZ is listed but not a pricing market; a non-ASCII key is masked)
        ("*", "unlisted_market"): 1, ("NZ", "unlisted_market"): 1, ("USA", "unlisted_market"): 1, ("?g", "unlisted_market"): 1,
        ("SG", "already_present"): 1,
        ("US", "ip_host"): 1, ("US", "non_https"): 1, ("US", "unparsable_url"): 1, ("US", "no_url"): 1,
        ("US", "public_suffix"): 1,
    }
    assert plan["status"] == "disabled" and plan["plan_sha256"] == core.verify_plan(plan)["plan_sha256"]


def test_marks_widened_redirector_and_multi_market_reach_the_reviewed_line() -> None:
    rules = [{"market": m, "destination_url": "https://www.shared.test/go/1"} for m in ("US", "SG", "GB")]
    rules.append({"market": "US", "destination_url": "https://deals.test/x?url=https://other.test/"})
    plan = core.build_plan(batch_id="olp-marks", rules=rules, seeds=[], present=[])
    lines = core.plan_lines(plan)
    assert "OLP domain market=US domain=shared.test rules=1 seeds=0 hosts=1 marks=apex,multi_market,path_go,redirector_suspect,widened" in lines
    assert "OLP domain market=US domain=deals.test rules=1 seeds=0 hosts=1 marks=apex,query_url,redirector_suspect" in lines
    assert "OLP market=US domains=2 from_rules=2 from_seeds=0 widened=1 redirector_suspect=2" in lines


def test_the_plan_never_writes(db) -> None:
    async def go():
        rec = Recording(db)
        before = await _rows(db)
        plan = await core.read_plan(rec, batch_id="olp-test-1", **LOCAL)
        assert await _rows(db) == before
        return plan, rec.statements

    plan, statements = asyncio.run(go())
    assert statements and all(s.lstrip().upper().startswith(("SELECT", "SET")) for s in statements), statements
    lines = core.plan_lines(plan)
    assert lines[0].startswith("OLP plan batch=olp-test-1 markets=2 domains=10 status=disabled plan_sha256=")
    assert all(line.startswith("OLP ") for line in lines)
    assert not [line for line in lines if "://" in line or "@" in line], "no URL, no address in the report"


def test_production_refuses_a_non_postgres_database(db) -> None:
    with pytest.raises(core.Refused, match="Postgres only"):
        asyncio.run(core.read_plan(db, batch_id="olp-test-1"))
    plan = asyncio.run(core.read_plan(db, batch_id="olp-test-1", **LOCAL))
    with pytest.raises(core.Refused, match="Postgres only"):
        asyncio.run(core.apply_plan(db, plan, lists_sha256=None))
    assert asyncio.run(_rows(db)) == [("SG", "sg.store-two.test", "active", "emp_1")]


# --- the staged rows are inert: the real reader never sees them ------------------------------------------


def test_an_applied_batch_is_invisible_to_the_allowlist_reader_and_the_mint_check(monkeypatch, db) -> None:
    import services.outbound_links_service as links

    monkeypatch.setattr(links, "database", db)
    cache, inflight = links._ALLOWED_DOMAIN_CACHE, links._ALLOWED_DOMAIN_INFLIGHT
    saved = (dict(cache), dict(inflight))
    cache.clear(); inflight.clear()
    try:
        async def go():
            await core.apply_plan(db, await core.read_plan(db, batch_id="olp-test-1", **LOCAL), **LOCAL, lists_sha256=None)
            staged = await links._fetch_allowed_domains_for_market("US")
            read = await links.get_allowed_domains_for_market(market="US")
            # Control: one ACTIVE row is seen at once (that is what enforcing will do).
            await db.execute("INSERT INTO outbound_link_allowed_domains (market, domain, status, created_by) VALUES ('JP','store-jp.test','active','emp_9')")
            return staged, read, await links._fetch_allowed_domains_for_market("JP")

        staged, read, control = asyncio.run(go())
    finally:
        cache.clear(); cache.update(saved[0]); inflight.clear(); inflight.update(saved[1])
    assert staged == [] and read == []
    assert is_destination_domain_allowed(destination_url="https://anything.other.test/x", allowed_domains=read) is True
    assert control == ["store-jp.test"]


# --- apply / rollback / re-run ----------------------------------------------------------------------------


def test_apply_is_insert_only_staged_and_tagged_and_rollback_deletes_only_its_batch(db) -> None:
    async def go():
        receipt = await core.apply_plan(db, await core.read_plan(db, batch_id="olp-test-1", **LOCAL), **LOCAL, lists_sha256=None)
        after_apply = await _rows(db)
        await db.execute("INSERT INTO outbound_link_allowed_domains (market, domain, status, created_by) VALUES ('JP','other.test','active','emp_2')")
        return receipt, after_apply, await core.rollback_batch(db, receipt, **LOCAL), await _rows(db)

    receipt, after_apply, result, after_rollback = asyncio.run(go())
    tag = "allowlist_populate:olp-test-1"
    assert receipt["inserted"] == 10 and receipt["per_market"] == {"GB": 1, "US": 9}
    assert core.receipt_line(receipt).startswith("OLP receipt batch=olp-test-1 inserted=10 per_market=GB:1,US:9 receipt_sha256=")
    assert ("SG", "sg.store-two.test", "active", "emp_1") in after_apply
    assert all(r[2:] == ("disabled", tag) for r in after_apply if r[1] != "sg.store-two.test")
    assert result["deleted"] == 10
    assert after_rollback == [("JP", "other.test", "active", "emp_2"), ("SG", "sg.store-two.test", "active", "emp_1")]


@pytest.mark.parametrize("edit", [
    "UPDATE outbound_link_allowed_domains SET status = 'active' WHERE domain = 'brand-a.test'",
    "UPDATE outbound_link_allowed_domains SET notes = 'edited' WHERE domain = 'brand-a.test'",
])
def test_rollback_refuses_once_a_batch_row_is_no_longer_staged(db, edit) -> None:
    async def go():
        receipt = await core.apply_plan(db, await core.read_plan(db, batch_id="olp-test-1", **LOCAL), **LOCAL, lists_sha256=None)
        await db.execute(edit)
        before = await _rows(db)
        with pytest.raises(core.Refused, match="no longer staged"):
            await core.rollback_batch(db, receipt, **LOCAL)
        return before, await _rows(db)

    before, after = asyncio.run(go())
    assert before == after, "nothing deleted"


def test_a_rerun_is_idempotent(db) -> None:
    async def go():
        plan = await core.read_plan(db, batch_id="olp-test-1", **LOCAL)
        await core.apply_plan(db, plan, **LOCAL, lists_sha256=None)
        once = await _rows(db)
        with pytest.raises(core.Refused, match="already present"):
            await core.apply_plan(db, plan, **LOCAL, lists_sha256=None)
        replan = await core.read_plan(db, batch_id="olp-test-2", **LOCAL)
        receipt = await core.apply_plan(db, replan, **LOCAL, lists_sha256=None)
        return once, replan, receipt, await _rows(db)

    once, replan, receipt, twice = asyncio.run(go())
    assert replan["entries"] == [] and receipt["inserted"] == 0 and twice == once


def test_a_batch_id_is_applied_once_even_with_new_pairs(db) -> None:
    async def go():
        await core.apply_plan(db, await core.read_plan(db, batch_id="olp-test-1", **LOCAL), **LOCAL, lists_sha256=None)
        await db.execute("INSERT INTO outbound_link_rules VALUES ('r9','US','https://store-new.test/p','published')")
        again = await core.read_plan(db, batch_id="olp-test-1", **LOCAL)
        assert [e["domain"] for e in again["entries"]] == ["store-new.test"]
        before = await _rows(db)
        with pytest.raises(core.Refused, match="already has rows"):
            await core.apply_plan(db, again, **LOCAL, lists_sha256=None)
        return before, await _rows(db)

    before, after = asyncio.run(go())
    assert before == after


def test_apply_refuses_a_tampered_or_drifted_plan_and_writes_nothing(db) -> None:
    async def go():
        plan = await core.read_plan(db, batch_id="olp-test-1", **LOCAL)
        tampered = {**plan, "entries": plan["entries"] + [{"market": "US", "domain": "store-nine.test", "rules": 0, "seeds": 0, "hosts": 1, "marks": []}]}
        with pytest.raises(core.Refused, match="its own sha"):
            await core.apply_plan(db, tampered, **LOCAL, lists_sha256=None)
        await db.execute("INSERT INTO outbound_link_allowed_domains (market, domain, status, created_by) VALUES ('US','brand-a.test','active','emp_3')")
        before = await _rows(db)
        with pytest.raises(core.Refused, match="already present"):
            await core.apply_plan(db, plan, **LOCAL, lists_sha256=None)
        return before, await _rows(db)

    before, after = asyncio.run(go())
    assert before == after


def test_rollback_refuses_when_the_batch_no_longer_matches_its_receipt(db) -> None:
    async def go():
        receipt = await core.apply_plan(db, await core.read_plan(db, batch_id="olp-test-1", **LOCAL), **LOCAL, lists_sha256=None)
        await db.execute("DELETE FROM outbound_link_allowed_domains WHERE domain = 'tenant.myshopify.com'")
        before = await _rows(db)
        with pytest.raises(core.Refused, match="no longer match"):
            await core.rollback_batch(db, receipt, **LOCAL)
        with pytest.raises(core.Refused):
            await core.rollback_batch(db, {**receipt, "inserted": 1}, **LOCAL)
        return before, await _rows(db)

    before, after = asyncio.run(go())
    assert before == after


# --- documents: chunked under the log-entry limit, reassembled exactly ------------------------------------


def test_a_large_plan_doc_is_chunked_and_reassembles() -> None:
    rules = [{"market": "US", "destination_url": f"https://store-{i:05d}-with-a-long-name.test/p"} for i in range(4000)]
    plan = core.build_plan(batch_id="olp-big", rules=rules, seeds=[], present=[])
    lines = core.doc_lines("plan_doc", plan)
    assert len(lines) > 1 and all(len(line.encode()) < 200_000 for line in lines)
    assert all(line.startswith(f"OLP plan_doc {i + 1}/{len(lines)} ") for i, line in enumerate(lines))
    shuffled = "\n".join(["noise"] + list(reversed(lines)))
    assert core.parse_doc_lines(shuffled, "plan_doc") == plan
    with pytest.raises(core.Refused, match="incomplete"):
        core.parse_doc_lines("\n".join(lines[1:]), "plan_doc")


# --- the job: plan by default, writes only with the flag and a pinned approval, refusals as one line --------


def test_every_refusal_is_one_olp_line_never_a_traceback(monkeypatch, capsys) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    for name in ("CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION", "PIVOTA_ENV", job.WRITE_FLAG):
        monkeypatch.delenv(name, raising=False)
    assert asyncio.run(job.main([])) == 2
    assert asyncio.run(job.main(["apply"])) == 2

    def boom(mode):
        raise ValueError("database said no\nsecond line")

    monkeypatch.setattr(job, "check_environment", boom)
    assert asyncio.run(job.main(["rollback"])) == 2
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3 and all(line.startswith("OLP refused mode=") for line in lines)
    assert lines[2] == "OLP refused mode=rollback reason=ValueError detail=database said no second line"


@pytest.mark.parametrize("mode, flag, ok", [("plan", None, True), ("plan", "1", False), ("apply", None, False),
                                            ("apply", "1", True), ("rollback", None, False), ("rollback", "1", True)])
def test_the_write_flag_is_set_for_apply_and_rollback_only(monkeypatch, tmp_path, mode, flag, ok) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    sha = "a" * 40
    image = tmp_path / ".image_commit_sha"
    image.write_text(sha)
    real_path = job.Path
    monkeypatch.setattr(job, "Path", lambda p: image if str(p) == "/app/.image_commit_sha" else real_path(p))
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


# --- v2 review P3s: apex marks, wider redirector marks, string snapshots, timing ------------------------


@pytest.mark.parametrize("url, apex", [
    ("https://platform-host.test/store-a", True),  # an uncurated platform's apex: admits every tenant, marked
    ("https://store-a.platform-host.test/p", False),  # its tenant, exact host: admits only its own subtree
    ("https://www.store-one.test/p", True),
    ("https://shop.store-one.test/p", False),
    ("https://tenant.myshopify.com/p", True),  # a curated platform tenant is its own registrable
])
def test_an_apex_entry_is_marked_for_the_review(url, apex) -> None:
    plan = core.build_plan(batch_id="olp-apex", rules=[{"market": "US", "destination_url": url}], seeds=[], present=[])
    assert ("apex" in plan["entries"][0]["marks"]) is apex
    line = [x for x in core.plan_lines(plan) if x.startswith("OLP domain ")][0]
    assert ("apex" in line.split("marks=", 1)[1].split(",")) is apex


@pytest.mark.parametrize("url, mark", [
    ("https://a.test/x?ued=https%3A%2F%2Fb.test%2F", "query_ued"),
    ("https://a.test/x?dl=https://b.test/", "query_dl"),
    ("https://a.test/x?deeplink=https://b.test/", "query_deeplink"),
    ("https://a.test/login?next=https://b.test/", "query_next"),
    ("https://a.test/x?continue=//b.test/p", "query_continue"),
    ("https://a.test/x?return_url=https://b.test/", "query_return_url"),
    ("https://a.test/x?lp=https://b.test/", "query_lp"),
    ("https://a.test/x?adurl=https://b.test/", "query_adurl"),
    ("https://a.test/x?href=https://b.test/", "query_href"),
    ("https://a.test/search?q=https://b.test/p", "query_q"),
    ("https://a.test/x?url=b.test/p", "query_url"),  # scheme-less
    ("https://a.test/x?url=aHR0cHM6Ly9iLnRlc3QvcA", "query_url"),  # base64 of a URL
    ("https://a.test/x?url=aHR0cHM6Ly9iLnRlc3QvcA==", "query_url"),
    ("https://a.test/out/https://b.test/p", "path_url"),  # a URL in the path
    ("https://a.test/go/https%3A%2F%2Fb.test%2Fp", "path_url"),
    ("https://a.test/redirect.php?id=1", "path_redirect"),
    ("https://a.test/click.php", "path_click"),
    ("https://a.test/en/redirect/123", "path_redirect"),  # a head in the second segment
    ("https://a.test/c/abc123/", "path_c_id"),
    ("https://a.test/t/xyz789", "path_t_id"),
    ("https://a.test/p#url=https://b.test/", "fragment_url"),
    ("https://a.test/p#https://b.test/", "fragment_url"),
    # the private host lists (a .test fixture here; the real lists are never in the repository)
    ("https://short.test/3abcde", "shortener_host"),
    ("https://go.tiny-link.test/somebody", "shortener_host"),
    ("https://www.aff-net.test/click-1-2", "affiliate_host"),
    ("https://click.aff-two.test/svt?id=1", "affiliate_host"),
])
def test_redirector_marks_table(url, mark, fixture_lists) -> None:
    assert mark in core.redirector_marks(url, fixture_lists)
    plan = core.build_plan(batch_id="olp-redir", rules=[{"market": "US", "destination_url": url}], seeds=[], present=[],
                           lists=fixture_lists)
    if plan["entries"]:
        assert "redirector_suspect" in plan["entries"][0]["marks"]


# --- the private redirector host lists: loaded at run time, pinned, counts only, bound into the plan and apply ---

LISTS_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "outbound_redirector_lists.test.json")


def _lists_env(path, pin=None) -> dict:
    from hashlib import sha256

    if pin is None:
        pin = sha256(open(path, "rb").read() if os.path.isfile(path) else b"").hexdigest()
    return {core.REDIRECTOR_LISTS_PATH_ENV: str(path), core.REDIRECTOR_LISTS_SHA256_ENV: pin}


@pytest.fixture
def fixture_lists() -> dict:
    return core.load_redirector_lists(_lists_env(LISTS_FIXTURE))


def test_the_lists_load_pinned_and_print_counts_only(fixture_lists) -> None:
    from hashlib import sha256

    assert fixture_lists["sha256"] == sha256(open(LISTS_FIXTURE, "rb").read()).hexdigest()
    assert fixture_lists["shorteners"] == ("short.test", "tiny-link.test")
    line = core.lists_line(fixture_lists)
    assert line == f"OLP lists sha256={fixture_lists['sha256']} shorteners=2 affiliate=2"
    assert ".test" not in line, "never a host"


@pytest.mark.parametrize("env", [
    {}, {core.REDIRECTOR_LISTS_PATH_ENV: ""}, {core.REDIRECTOR_LISTS_PATH_ENV: "  "},
    {core.REDIRECTOR_LISTS_PATH_ENV: "", core.REDIRECTOR_LISTS_SHA256_ENV: ""},
])
def test_unset_lists_are_absent_no_list_marks_and_no_failure(env) -> None:
    lists = core.load_redirector_lists(env)
    assert lists["sha256"] is None and core.lists_line(lists) == "OLP lists=absent"
    assert core.redirector_marks("https://short.test/3abcde", lists) == set()
    assert core.redirector_marks("https://www.aff-net.test/click-1-2", lists) == set()
    plan = core.build_plan(batch_id="olp-redir", rules=[{"market": "US", "destination_url": "https://short.test/3abcde"}],
                           seeds=[], present=[], lists=lists)
    assert plan["redirector_lists_sha256"] is None and "shortener_host" not in plan["entries"][0]["marks"]


@pytest.mark.parametrize("case", ["set_missing_file", "set_empty_file", "pin_without_path", "pin_blank_path"])
def test_configured_lists_that_are_not_there_refuse_lists_missing(case, tmp_path) -> None:
    (tmp_path / "empty.json").write_bytes(b"")
    env = {
        "set_missing_file": _lists_env(tmp_path / "nope.json", pin="0" * 64),
        "set_empty_file": _lists_env(tmp_path / "empty.json"),
        "pin_without_path": {core.REDIRECTOR_LISTS_SHA256_ENV: "0" * 64},
        "pin_blank_path": {core.REDIRECTOR_LISTS_PATH_ENV: " ", core.REDIRECTOR_LISTS_SHA256_ENV: "0" * 64},
    }[case]
    with pytest.raises(core.Refused) as refused:
        core.load_redirector_lists(env)
    assert refused.value.reason == "lists_missing"


@pytest.mark.parametrize("mode", ["plan", "apply"])
def test_the_job_refuses_lists_missing_and_writes_nothing(mode, db, monkeypatch, capsys, tmp_path) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    monkeypatch.setenv(core.REDIRECTOR_LISTS_PATH_ENV, str(tmp_path / "unmounted.json"))
    monkeypatch.setenv(core.REDIRECTOR_LISTS_SHA256_ENV, "0" * 64)
    monkeypatch.setenv(job.ENV + "BATCH_ID", "olp-test-1")
    monkeypatch.setattr(job, "read_pinned", lambda name: json.dumps({"plan_sha256": "p" * 64}).encode())
    before = asyncio.run(_rows(db))
    with pytest.raises(core.Refused) as refused:
        asyncio.run(job.run(mode, db, release="a" * 40))
    assert refused.value.reason == "lists_missing"
    assert asyncio.run(_rows(db)) == before and capsys.readouterr().out == ""


def test_lists_that_miss_their_pin_refuse(tmp_path) -> None:
    for pin in ("0" * 64, "", "not-a-sha"):
        with pytest.raises(core.Refused) as refused:
            core.load_redirector_lists(_lists_env(LISTS_FIXTURE, pin=pin))
        assert refused.value.reason == "lists_sha_mismatch"


@pytest.mark.parametrize("body, why", [
    (b"not json", "not JSON"), (b"[]", "schema"), (b'{"version": 2, "shorteners": [], "affiliate_networks": []}', "schema"),
    (b'{"version": 1, "shorteners": []}', "schema"),
    (b'{"version": 1, "shorteners": ["Short.test"], "affiliate_networks": []}', "schema"),
    (b'{"version": 1, "shorteners": ["short.test", "short.test"], "affiliate_networks": []}', "schema"),
    (b'{"version": 1, "shorteners": ["https://short.test/"], "affiliate_networks": []}', "schema"),
    (b'{"version": 1, "version": 1, "shorteners": [], "affiliate_networks": []}', "not JSON"),
    (b'{"version": true, "shorteners": [], "affiliate_networks": []}', "schema"),
    (b'{"version": 1.0, "shorteners": [], "affiliate_networks": []}', "schema"),
    (b'{"version": "1", "shorteners": [], "affiliate_networks": []}', "schema"),
    (b'\xef\xbb\xbf{"version": 1, "shorteners": [], "affiliate_networks": []}', "byte-order mark"),  # UTF-8 BOM
    ('{"version": 1, "shorteners": [], "affiliate_networks": []}'.encode("utf-16"), "byte-order mark"),  # UTF-16 + BOM
    ('{"version": 1, "shorteners": [], "affiliate_networks": []}'.encode("utf-16-le"), "NUL"),  # UTF-16, no BOM
    (b'{"version": 1, "shorteners": ["sh\xc3ort.test"], "affiliate_networks": []}', "not UTF-8"),
    (b'{"version": 1, "shorteners": [], "affiliate_networks": []}\xff', "not UTF-8"),
])
def test_lists_that_are_not_the_schema_refuse(body, why, tmp_path) -> None:
    path = tmp_path / "lists.json"
    path.write_bytes(body)
    with pytest.raises(core.Refused) as refused:
        core.load_redirector_lists(_lists_env(path))
    assert refused.value.reason == "lists_invalid" and why in str(refused.value)


def test_the_plan_names_its_lists_and_apply_refuses_other_lists(db, fixture_lists) -> None:
    async def go():
        plan = await core.read_plan(db, batch_id="olp-test-1", lists=fixture_lists, **LOCAL)
        assert plan["redirector_lists_sha256"] == fixture_lists["sha256"]
        assert core.verify_plan(plan) is plan
        for other in (None, "0" * 64):
            with pytest.raises(core.Refused) as refused:
                await core.apply_plan(db, plan, lists_sha256=other, **LOCAL)
            assert refused.value.reason == "lists_sha_mismatch"
        assert len(await _rows(db)) == 1, "nothing written"
        unnamed = {k: v for k, v in plan.items() if k not in ("redirector_lists_sha256", "plan_sha256")}
        unnamed["plan_sha256"] = core.digest(unnamed)
        with pytest.raises(core.Refused, match="redirector lists"):
            core.verify_plan(unnamed)
        receipt = await core.apply_plan(db, plan, lists_sha256=fixture_lists["sha256"], **LOCAL)
        assert receipt["inserted"] == len(plan["entries"])

    asyncio.run(go())


def test_the_apply_approval_binds_the_plans_lists(monkeypatch, tmp_path) -> None:
    from hashlib import sha256

    from scripts.ops import outbound_allowlist_populate as job

    def approve(lists_sha):
        approval = {"mode": "apply", "plan_sha256": "p" * 64, "lists_sha256": lists_sha, "release_sha": "r" * 40,
                    "go_reference": "go ref"}
        path = tmp_path / "approval.json"
        path.write_bytes(json.dumps(approval).encode())
        monkeypatch.setenv(job.ENV + "APPROVAL_PATH", str(path))
        monkeypatch.setenv(job.ENV + "APPROVAL_SHA256", sha256(path.read_bytes()).hexdigest())

    args = {"release": "r" * 40, "pinned_key": "plan_sha256", "pinned_sha": "p" * 64}
    approve("l" * 64)
    assert job.check_approval("apply", **args, extra={"lists_sha256": "l" * 64})
    for other in (None, "m" * 64):
        with pytest.raises(core.Refused):
            job.check_approval("apply", **args, extra={"lists_sha256": other})
    approve(None)
    assert job.check_approval("apply", **args, extra={"lists_sha256": None})
    with pytest.raises(core.Refused):
        job.check_approval("apply", **args)  # an approval naming lists never passes without the plan's lists


def test_the_job_refuses_a_lists_sha_mismatch_as_one_line(db, monkeypatch, capsys) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    for key, value in _lists_env(LISTS_FIXTURE, pin="0" * 64).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(job, "check_environment", lambda mode: "a" * 40)
    monkeypatch.setattr(job, "check_gate", lambda now: {})
    import scripts.ops.reviewed_seed_batch as seed_batch

    async def no_pins(conn):
        return None

    monkeypatch.setattr(seed_batch, "runtime_pins", lambda: no_pins)
    import db.database as database_module

    monkeypatch.setattr(database_module, "database", db)
    monkeypatch.setattr(db, "connect", _noop)
    monkeypatch.setattr(db, "disconnect", _noop)
    assert asyncio.run(job.main(["plan"])) == 2
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("OLP refused mode=plan reason=lists_sha_mismatch ")


async def _noop(*args, **kwargs):
    return None


@pytest.mark.parametrize("url", [
    "https://store-one.test/products/rocket-shoe?ref=home",
    "https://store-one.test/collections/sale?page=2",
    "https://store-one.test/search?q=red+shoes",  # a search term, not a URL
    "https://store-one.test/c/",  # a bare /c/ with no id
    "https://store-one.test/products/clicker",
])
def test_ordinary_store_urls_get_no_redirector_mark(url) -> None:
    assert core.redirector_marks(url) == set()


@pytest.mark.parametrize("raw, parsed", [
    ('{"canonical_url": "https://a.test/p"}', {"canonical_url": "https://a.test/p"}),
    ('"{\\"canonical_url\\": \\"https://a.test/p\\"}"', {"canonical_url": "https://a.test/p"}),  # string-encoded
    ({"destination_url": "https://a.test/p"}, {"destination_url": "https://a.test/p"}),
    ('"not json"', {}), ("null", {}), (None, {}), ("[1, 2]", {}), ("{bad", {}),
])
def test_a_snapshot_is_read_as_the_mint_reads_it(raw, parsed) -> None:
    from routes.agent_api import _ensure_json_obj as mint_ensure_json_obj

    assert core.seed_snapshot(raw) == parsed
    decoded = raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
    assert core.seed_snapshot(raw) == mint_ensure_json_obj(decoded), "the mint's own parser agrees"


def test_the_plan_prints_a_timing_line(db) -> None:
    timing: dict = {}
    asyncio.run(core.read_plan(db, batch_id="olp-test-1", timing=timing, **LOCAL))
    published = sum(1 for r in RULES if r[3] == "published")
    active = sum(1 for r in SEEDS if r[6] == "active")
    assert (timing["rules"], timing["seeds"], timing["present"]) == (published, active, 1)
    line = core.timing_line(timing)
    assert line.startswith("OLP timing ms=") and f"rules={published} seeds={active} present=1" in line


def test_the_job_plan_run_prints_the_timing_line_first(db, monkeypatch, capsys) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    real_read_plan = core.read_plan

    async def local_read_plan(conn, **kwargs):
        return await real_read_plan(conn, **kwargs, **LOCAL)

    monkeypatch.setattr(job.core, "read_plan", local_read_plan)
    monkeypatch.setenv(job.ENV + "BATCH_ID", "olp-test-1")
    for key, value in _lists_env(LISTS_FIXTURE).items():
        monkeypatch.setenv(key, value)
    assert asyncio.run(job.run("plan", db, release="a" * 40)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("OLP timing ms=") and " read_ms=" in lines[0] and " build_ms=" in lines[0]
    assert lines[1].startswith("OLP lists sha256=") and lines[1].endswith(" shorteners=2 affiliate=2")
    doc = core.parse_doc_lines("\n".join(lines), "plan_doc")
    assert doc["redirector_lists_sha256"] == lines[1].split("sha256=")[1].split()[0]
    assert any(line.startswith("OLP plan_doc 1/") for line in lines)


# --- P3-2 / P3-3: one no-follow open, a regular file under the cap, absent-at-plan vs present-at-apply, the job's
# --- approval binding


def test_a_directory_a_symlink_or_a_fifo_is_refused(tmp_path) -> None:
    import shutil

    real = tmp_path / "real.json"
    shutil.copyfile(LISTS_FIXTURE, real)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "gone.json")
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)  # no writer: a blocking open would hang; a non-regular file is refused before any read
    assert core.load_redirector_lists(_lists_env(real))["sha256"]
    for path in (tmp_path, link, dangling, fifo):
        with pytest.raises(core.Refused) as refused:
            core.load_redirector_lists(_lists_env(path, pin=_lists_env(real)[core.REDIRECTOR_LISTS_SHA256_ENV]))
        assert refused.value.reason == "lists_invalid", path


def test_the_lists_file_size_cap(tmp_path) -> None:
    body = b'{"version": 1, "shorteners": ["short.test"], "affiliate_networks": []}'
    path = tmp_path / "lists.json"
    path.write_bytes(body + b" " * (core.REDIRECTOR_LISTS_LIMIT - len(body)))
    assert core.load_redirector_lists(_lists_env(path))["shorteners"] == ("short.test",)
    path.write_bytes(body + b" " * (core.REDIRECTOR_LISTS_LIMIT + 1 - len(body)))
    with pytest.raises(core.Refused) as refused:
        core.load_redirector_lists(_lists_env(path))
    assert refused.value.reason == "lists_invalid" and "too large" in str(refused.value)


def test_lists_absent_at_plan_and_present_at_apply_refuse(db, fixture_lists) -> None:
    async def go():
        plan = await core.read_plan(db, batch_id="olp-test-1", **LOCAL)
        assert plan["redirector_lists_sha256"] is None
        with pytest.raises(core.Refused) as refused:
            await core.apply_plan(db, plan, lists_sha256=fixture_lists["sha256"], **LOCAL)
        assert refused.value.reason == "lists_sha_mismatch"
        assert len(await _rows(db)) == 1, "nothing written"

    asyncio.run(go())


def _job_apply_files(monkeypatch, tmp_path, job, plan, approval) -> None:
    from hashlib import sha256

    for name, doc in (("PLAN", plan), ("APPROVAL", approval)):
        path = tmp_path / f"{name.lower()}.json"
        path.write_bytes(json.dumps(doc).encode())
        monkeypatch.setenv(job.ENV + name + "_PATH", str(path))
        monkeypatch.setenv(job.ENV + name + "_SHA256", sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize("case, ok", [
    ("same_lists", True), ("approval_names_other_lists", False), ("approval_names_no_lists", False),
    ("approval_omits_lists", False), ("absent_at_plan_present_at_apply", False), ("lists_changed_after_plan", False),
])
def test_the_job_apply_binds_the_plans_lists(case, ok, db, monkeypatch, capsys, tmp_path, fixture_lists) -> None:
    from scripts.ops import outbound_allowlist_populate as job

    release = "a" * 40
    plan_lists = dict(core.ABSENT_LISTS) if case == "absent_at_plan_present_at_apply" else fixture_lists
    plan = asyncio.run(core.read_plan(db, batch_id="olp-test-1", lists=plan_lists, **LOCAL))
    approval = {"mode": "apply", "plan_sha256": plan["plan_sha256"], "lists_sha256": plan["redirector_lists_sha256"],
                "release_sha": release, "go_reference": "go ref"}
    if case == "approval_names_other_lists":
        approval["lists_sha256"] = "0" * 64
    if case == "approval_names_no_lists":
        approval["lists_sha256"] = None
    if case == "approval_omits_lists":
        del approval["lists_sha256"]
    _job_apply_files(monkeypatch, tmp_path, job, plan, approval)
    lists_path = tmp_path / "lists.json"
    lists_path.write_bytes(open(LISTS_FIXTURE, "rb").read())
    if case == "lists_changed_after_plan":
        lists_path.write_bytes(b'{"version": 1, "shorteners": ["other-short.test"], "affiliate_networks": []}')
    for key, value in _lists_env(lists_path).items():
        monkeypatch.setenv(key, value)
    real_apply = core.apply_plan

    async def local_apply(conn, plan_doc, **kwargs):
        return await real_apply(conn, plan_doc, **kwargs, **LOCAL)

    monkeypatch.setattr(job.core, "apply_plan", local_apply)
    before = asyncio.run(_rows(db))
    if ok:
        assert asyncio.run(job.run("apply", db, release=release)) == 0
        assert len(asyncio.run(_rows(db))) == len(before) + len(plan["entries"])
        assert any(line.startswith("OLP receipt ") for line in capsys.readouterr().out.splitlines())
        return
    with pytest.raises(core.Refused):
        asyncio.run(job.run("apply", db, release=release))
    assert asyncio.run(_rows(db)) == before, "nothing written"
