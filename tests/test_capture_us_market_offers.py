"""Pin the pure planning core of the US-market offer capture.

A wrong plan here writes a bogus USD price onto a live product or clobbers the
mirror's first-party offer row — both are served-surface corruption, so the id
namespace, the handle parser's refusal to guess, and the no-positive-price
refusal are each pinned.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.capture_us_market_offers import (  # noqa: E402
    OFFER_UPSERT_SQL,
    derive_us_offer_id,
    handle_from_url,
    plan_offer,
    select_capturable,
)
from services.external_offer_dual_write import (  # noqa: E402
    derive_mirror_offer_id,
)

_CAND = {
    "content_key": "ck-1",
    "product_key": "prod::external_seed::external_seed::slug-1",
    "merchant_id": "merch_obs_a",
    "canonical_url": "https://shop.example/products/slug-1",
    "source_domain": "shop.example",
}


def test_us_offer_id_never_collides_with_the_mirror_id() -> None:
    """Same product_key, two DIFFERENT offer rows: the sibling-US-offer design
    depends on the namespaces being disjoint, or the upsert would overwrite
    the mirror's home-market offer instead of adding one."""
    pk = _CAND["product_key"]
    assert derive_us_offer_id(pk) != derive_mirror_offer_id(pk)
    assert derive_us_offer_id(pk).startswith("offer:us_market:")


def test_handle_is_the_final_path_segment() -> None:
    assert handle_from_url("https://s.example/products/kojic-soap") == "kojic-soap"
    assert handle_from_url(
        "https://s.example/products/kojic-soap?variant=1#top") == "kojic-soap"
    assert handle_from_url("https://s.example/products/kojic-soap/") == "kojic-soap"


def test_handle_refuses_to_guess_on_non_product_urls() -> None:
    """A bare domain or a collections index must yield None (caller skips) —
    guessing a handle would price the WRONG product."""
    assert handle_from_url("https://s.example/") is None
    assert handle_from_url("https://s.example/products") is None
    assert handle_from_url(None) is None


def test_plan_offer_builds_usd_row_in_currency_units() -> None:
    row = plan_offer(_CAND, 2500, True)
    assert row is not None
    assert row["list_price"] == 25.0
    assert row["availability"] == "in_stock"
    assert row["offer_id"] == derive_us_offer_id(_CAND["product_key"])
    assert row["source_system"] == "us_market_capture"


def test_us_offer_shares_the_canonical_sku_row() -> None:
    """pivot_query_service INNER JOINs offers via sku_key; an invented
    suffix would hide the US offer from every sku-joined lane."""
    from services.external_offer_dual_write import derive_mirror_sku_key
    row = plan_offer(_CAND, 2500, True)
    assert row["sku_key"] == derive_mirror_sku_key(_CAND["product_key"])


def _cand(pk: str, url: str, dom: str):
    return {"content_key": f"ck-{pk}", "product_key": pk, "merchant_id": "m",
            "canonical_url": url, "source_domain": dom}


def test_select_capturable_rejects_domain_mismatch() -> None:
    """The handle comes from canonical_url but the fetch goes to
    source_domain — a mismatched pair would price the WRONG store's
    product (Path-C sibling-domain seed attribution)."""
    by_domain, skipped = select_capturable([
        _cand("pk1", "https://storea.com/products/serum", "storeb.com"),
    ])
    assert by_domain == {}
    assert skipped == [("pk1", "domain_mismatch")]


def test_select_capturable_accepts_www_and_matching_host() -> None:
    by_domain, skipped = select_capturable([
        _cand("pk1", "https://www.storea.com/products/serum", "storea.com"),
    ])
    assert list(by_domain) == ["storea.com"] and skipped == []


def test_select_capturable_rejects_multi_domain_products() -> None:
    """One product reachable via two domains would plan one offer_id twice
    and let sort order pick the surviving price — ambiguity is rejected."""
    by_domain, skipped = select_capturable([
        _cand("pk1", "https://storea.com/products/serum", "storea.com"),
        _cand("pk1", "https://storea.com/products/serum", "storeb.com"),
    ])
    assert by_domain == {}
    assert skipped == [("pk1", "ambiguous_domains")]


def test_plan_offer_refuses_non_positive_prices() -> None:
    """0 and None are 'we do not know the US price' — never a buyable quote."""
    assert plan_offer(_CAND, 0, True) is None
    assert plan_offer(_CAND, None, True) is None
    assert plan_offer(_CAND, -100, True) is None


def test_plan_offer_marks_unavailable_stock() -> None:
    row = plan_offer(_CAND, 2500, False)
    assert row is not None and row["availability"] == "out_of_stock"


def test_the_upsert_reads_the_seller_from_the_catalog_row_not_the_scan_snapshot():
    """THE TOCTOU THAT PRODUCED 12 ORPHANS (ADR-009).

    CANDIDATES_SQL is fetched once, then every storefront is probed over HTTP
    for minutes before any upsert runs. On 2026-08-14 the A9-4 flip re-keyed
    these products and cascaded catalog_offers during that window, and 12
    offers landed afterwards carrying the retired sentinel — orphans no cascade
    could reach, repairable only by scripts/dispose_sentinel_orphans.py because
    ON CONFLICT DO UPDATE did not touch merchant_id either.

    So the seller must be read from the catalog row IN the statement, on both
    the insert and the refresh path.
    """
    flat = " ".join(OFFER_UPSERT_SQL.split())
    assert "cp.merchant_id" in flat, "the insert must read the seller from catalog_products"
    # The bind is CAST on BOTH occurrences: it sits in the INSERT's column list
    # and in this predicate, and Postgres deduces one type per parameter per
    # statement — casting only one turns IndeterminateDatatype into
    # AmbiguousParameter rather than fixing it (the repo's PREPARE gate caught
    # exactly that here).
    assert "FROM catalog_products cp" in flat
    assert "WHERE cp.product_key = CAST(:product_key AS text)" in flat
    assert flat.count("CAST(:product_key AS text)") == 2
    assert "merchant_id = EXCLUDED.merchant_id" in flat, (
        "without this a row written before a re-key keeps the stale seller forever")
    assert ":merchant_id" not in flat, "the snapshot value must not be bound at all"


def test_plan_offer_does_not_carry_a_merchant_and_its_binds_match_the_statement():
    """Both sides of the contract, driven together.

    `databases`/SQLAlchemy text() rejects a params key the statement does not
    name AND a named bind with no value, so a drift in either direction is a
    hard runtime failure on every write — exactly the class that aborted the
    2026-08-07 canary batch. Asserting only one side would let the other rot.
    """
    import re

    row = plan_offer(_CAND, 2500, True)
    assert "merchant_id" not in row, (
        "the stale scan-time seller must not be passed; the statement reads it live")
    named = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", OFFER_UPSERT_SQL))
    assert named == set(row), (
        f"bind mismatch: SQL names {sorted(named)}, plan_offer gives {sorted(row)}")


def test_seed_ids_parse_commas_spaces_and_a_file(tmp_path) -> None:
    from scripts.capture_us_market_offers import parse_seed_ids

    assert parse_seed_ids(None) is None
    assert parse_seed_ids("eps_b, eps_a eps_b") == ["eps_a", "eps_b"]
    f = tmp_path / "ids.txt"
    f.write_text("eps_c\neps_a\n\n")
    assert parse_seed_ids(f"@{f}") == ["eps_a", "eps_c"]


def test_seed_ids_that_name_nothing_are_refused() -> None:
    """A run that selects no seed would print an empty plan that reads as 'nothing to capture'."""
    import pytest

    from scripts.capture_us_market_offers import parse_seed_ids

    for raw in ("", " , ", "\n"):
        with pytest.raises(ValueError):
            parse_seed_ids(raw)


# --- PR controller review of #2666: egress, shelf, and the capture/_drive paths -----------------

import asyncio  # noqa: E402

import pytest  # noqa: E402

import httpx  # noqa: E402

import scripts.capture_us_market_offers as cap  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def test_storefronts_are_never_fetched_off_the_crawl_subnet(monkeypatch) -> None:
    for value in (None, "default", "pivota-crawl "):
        if value is None:
            monkeypatch.delenv(cap.EGRESS_SUBNET_ENV, raising=False)
        else:
            monkeypatch.setenv(cap.EGRESS_SUBNET_ENV, value)
        with pytest.raises(SystemExit):
            cap.require_crawl_egress()
    monkeypatch.setenv(cap.EGRESS_SUBNET_ENV, "pivota-crawl")
    cap.require_crawl_egress()


def test_the_shelf_retires_projected_siblings_and_refuses_any_other() -> None:
    projected = ("external_product_seeds_mirror_v1", "variant_identity_backfill_v1")
    planned = [{"product_key": "pk_a", "sku_key": "pk_a::canonical", "offer_id": "us_a"},
               {"product_key": "pk_b", "sku_key": "pk_b::canonical", "offer_id": "us_b"},
               {"product_key": "pk_c", "sku_key": "pk_c::canonical", "offer_id": "us_c"}]
    siblings = [{"offer_id": "mirror_a", "sku_key": "pk_a::canonical", "source_system": projected[0]},
                {"offer_id": "mirror_b", "sku_key": "pk_b::canonical", "source_system": projected[0]},
                {"offer_id": "enrich_b", "sku_key": "pk_b::canonical", "source_system": "catalog_enrichment_agent_v1"}]
    keep, refused = cap.plan_shelf(planned, siblings, projected)
    assert [(r["product_key"], r["retire_offer_ids"]) for r in keep] == [("pk_a", ["mirror_a"]), ("pk_c", [])]
    assert refused == [("pk_b", "offers_refused_shelf_taken")]


def test_a_us_market_capture_row_is_never_the_seeds_listing() -> None:
    """Its destination_url is the seed's product page, so the URL rule alone would claim it, and a seed
    whose column says USD over a EUR page would rewrite the honest USD row (review of #2666)."""
    from services.external_offer_dual_write import NON_LISTING_OFFER_SOURCE_SYSTEMS, is_listing_offer

    seed = {"id": "eps_1", "destination_url": "https://shop.example/products/soap"}
    row = {"payload_destination_url": "https://shop.example/products/soap", "source_ref": None,
           "payload_seed_id": None}
    assert is_listing_offer({**row, "source_system": "catalog_enrichment_agent_v1"}, seed) is True
    assert is_listing_offer({**row, "source_system": cap.SOURCE_SYSTEM}, seed) is False
    assert cap.SOURCE_SYSTEM in NON_LISTING_OFFER_SOURCE_SYSTEMS


def _storefront(cart_currencies):
    """A Shopify Markets store whose /cart.js answers the given currencies in order (the last repeats)."""
    import httpx

    answers = list(cart_currencies)
    seen = []

    def handler(req):
        path = req.url.path
        seen.append(path)
        if path == "/cart.js":
            cur = answers.pop(0) if len(answers) > 1 else answers[0]
            return httpx.Response(200, json={"currency": cur})
        if path.startswith("/products/"):
            return httpx.Response(200, json={"price": 2300, "available": True})
        return httpx.Response(200, text="ok")
    return handler, seen


def _capture(monkeypatch, handler, n):
    import httpx

    async def no_pace(url):
        return None

    monkeypatch.setattr(cap, "_paced", no_pace)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **k))
    cands = [{**_CAND, "product_key": f"pk_{i}", "content_key": f"ck_{i}",
              "canonical_url": f"https://shop.example/products/h{i}"} for i in range(n)]
    return asyncio.run(cap.capture_domain("shop.example", cands))


def test_reads_since_the_last_proof_are_dropped_when_the_session_decays(monkeypatch) -> None:
    """M5: the re-check every SESSION_RECHECK_EVERY reads is what catches a session that decays and
    then recovers (USD -> GBP -> USD): the final proof passes, so only the periodic one drops the
    reads taken while it was GBP."""
    monkeypatch.setattr(cap, "SESSION_RECHECK_EVERY", 2)
    handler, _ = _storefront(["USD", "GBP", "USD"])  # proof, failed re-check, passing final proof
    result = _capture(monkeypatch, handler, 3)
    assert [r["product_key"] for r in result["planned"]] == ["pk_2"]
    assert sorted(result["skipped"]) == [("pk_0", "us_session_lost"), ("pk_1", "us_session_lost")]


def test_the_last_reads_are_kept_only_after_a_final_proof(monkeypatch) -> None:
    """M10: the final checkpoint commits the tail; without it nothing after the last re-check counts."""
    handler, seen = _storefront(["USD"])
    result = _capture(monkeypatch, handler, 3)
    assert [r["product_key"] for r in result["planned"]] == ["pk_0", "pk_1", "pk_2"]
    assert seen[-1] == "/cart.js"
    handler, _ = _storefront(["USD", "GBP"])
    assert _capture(monkeypatch, handler, 1)["planned"] == []


class _FakeDB:
    def __init__(self, candidates, outcomes=(), siblings=()):
        self.candidates, self.outcomes, self.siblings = candidates, list(outcomes), list(siblings)
        self.calls = []

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def fetch_all(self, sql, params=None):
        self.calls.append((sql, params))
        if sql is cap.SEED_MATCH_SQL:
            return self.outcomes
        if sql is cap.SHELF_SIBLINGS_SQL:
            return self.siblings
        if sql is cap.RETIRE_SUPERSEDED_SQL:
            return [{"offer_id": i} for i in params["retire_offer_ids"]]
        return self.candidates

    async def fetch_val(self, sql, params=None):
        self.calls.append((sql, params))
        return params.get("offer_id")

    def transaction(self):
        db = self

        class _Tx:
            async def __aenter__(self):
                db.calls.append(("BEGIN", None))

            async def __aexit__(self, *exc):
                db.calls.append(("COMMIT", None))
        return _Tx()


def _drive(monkeypatch, *, apply, seed_ids, siblings=()):
    monkeypatch.setenv(cap.EGRESS_SUBNET_ENV, "pivota-crawl")
    cand = {**_CAND, "source_domain": "shop.example"}
    db = _FakeDB([cand], outcomes=[{"id": "eps_1", "outcome": "matched"}, {"id": "eps_x", "outcome": "missing"}],
                 siblings=siblings)
    monkeypatch.setattr(cap, "database", db)

    async def fake_capture(domain, cands):
        rows = [dict(cap.plan_offer(c, 2300, True), content_key=c["content_key"]) for c in cands]
        return {"domain": domain, "us_market": True, "planned": rows, "skipped": []}

    async def fake_skus(planned, *, apply, batch_id):
        return planned, {}

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(cap, "capture_domain", fake_capture)
    monkeypatch.setattr(cap, "ensure_skus_for_planned", fake_skus)
    monkeypatch.setattr(cap, "write_writer_audit_log", nothing)
    import services.agent_pdp_view_assembler as pdp
    import services.index_pipeline_state_service as ips
    monkeypatch.setattr(pdp, "refresh_agent_pdp_view_for_content_key", nothing)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", nothing)
    rc = asyncio.run(cap._drive(apply, None, None, seed_ids))
    return rc, db


def test_seed_ids_drive_the_seed_query_not_the_cohort_scan(monkeypatch) -> None:
    """M11: --seed-ids must reach the statement; a _drive that ignored it would capture the
    no_us_offer cohort instead."""
    _, db = _drive(monkeypatch, apply=False, seed_ids=["eps_1", "eps_x"])
    sqls = [sql for sql, _ in db.calls]
    assert cap.CANDIDATES_SQL not in sqls
    assert (cap.SEED_CANDIDATES_SQL, {"seed_ids": ["eps_1", "eps_x"]}) in db.calls
    assert (cap.SEED_MATCH_SQL, {"seed_ids": ["eps_1", "eps_x"]}) in db.calls


def test_a_dry_run_reads_the_shelf_and_writes_nothing(monkeypatch, capsys) -> None:
    """M6: the dry run plans the shelf (so it shows what --apply retires) and never writes."""
    sib = [{"offer_id": "mirror_1", "sku_key": cap.derive_mirror_sku_key(_CAND["product_key"]),
            "source_system": "external_product_seeds_mirror_v1"}]
    _, db = _drive(monkeypatch, apply=False, seed_ids=["eps_1"], siblings=sib)
    sqls = [sql for sql, _ in db.calls]
    assert cap.SHELF_SIBLINGS_SQL in sqls
    assert cap.OFFER_UPSERT_SQL not in sqls and cap.RETIRE_SUPERSEDED_SQL not in sqls
    out = capsys.readouterr().out
    assert '"shelf_retire_planned": 1' in out and '"seed_ids_unmatched": {"missing": 1}' in out


def test_apply_retires_the_sibling_inside_the_upserts_transaction(monkeypatch, capsys) -> None:
    sib = [{"offer_id": "mirror_1", "sku_key": cap.derive_mirror_sku_key(_CAND["product_key"]),
            "source_system": "external_product_seeds_mirror_v1"}]
    _, db = _drive(monkeypatch, apply=True, seed_ids=["eps_1"], siblings=sib)
    seq = [sql if isinstance(sql, str) and sql in ("BEGIN", "COMMIT") else
           {cap.OFFER_UPSERT_SQL: "UPSERT", cap.RETIRE_SUPERSEDED_SQL: "RETIRE"}.get(sql)
           for sql, _ in db.calls]
    seq = [s for s in seq if s]
    assert seq == ["BEGIN", "UPSERT", "RETIRE", "COMMIT"]
    assert '"superseded_retired": 1' in capsys.readouterr().out
