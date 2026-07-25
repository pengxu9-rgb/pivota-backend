"""
HTTP-level tests for the canonical PDP resolver
(routes.pivota_canonical_routes).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _sitemap_row_eligible(r: Dict[str, Any]) -> bool:
    """Mirror routes.pivota_canonical_routes list eligibility, flag-aware.

    content_key always required; canonical sig required ONLY when the sitemap
    is not widened; serving_eligible OR (widened AND index_eligible).
    """
    widen = (os.getenv("INDEX_ELIGIBLE_SITEMAP") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not r.get("content_key"):
        return False
    # Suppressed rows are withdrawn from serving and never advertised.
    if r.get("suppressed_at") is not None:
        return False
    index_elig = r.get("index_eligible") is True
    # Merchant gate (mirrors the INNER/LEFT join). merchant_indexable=None models
    # a row with NO catalog_merchants entry (store-less brand): allowed only when
    # widened + index_eligible; a present-but-hidden retail merchant stays out.
    if r.get("merchant_indexable") is None:
        if not (widen and index_elig):
            return False
    else:
        if r.get("merchant_indexable") is not True:
            return False
        # ADR-009 amendment: observed (unclaimed) sellers serve; only
        # disabled/inactive merchants are excluded.
        if r.get("merchant_status", "active") not in ("active", "observed"):
            return False
    has_sig = str(r.get("pivota_signature_id") or "").startswith("sig_")
    serving = r.get("serving_eligible") is True
    # identity: sig-bearing, OR an index_eligible citation row when widened
    identity_ok = has_sig or (widen and index_elig)
    eligible = serving or (widen and index_elig)
    return identity_ok and eligible


class FakeDb:
    """Minimal in-memory DB stub. Parses the compiled SQL loosely to
    decide which canned response to return — enough for the resolver's
    two queries (single-sig SELECT + paginated list)."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.compiled_sql: List[str] = []

    async def fetch_one(self, query):
        try:
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            return None
        self.compiled_sql.append(sql)
        import re
        m = re.search(r"pivota_signature_id\s*=\s*'([^']+)'", sql)
        if not m:
            return None
        sig = m.group(1)
        for r in self._rows:
            if (
                r.get("pivota_signature_id") == sig
                and r.get("content_key")
                and r.get("suppressed_at") is None
                and r.get("serving_eligible") is True
                and r.get("merchant_indexable", True) is True
                and r.get("merchant_status", "active") in ("active", "observed")
            ):
                return r
        return None

    def _eligible_sorted(self) -> List[Dict[str, Any]]:
        """Rows passing the (flag-aware) sitemap gate, in the endpoint's
        ORDER BY: (content_changed_at DESC, sig ASC NULLS LAST,
        content_key ASC, product_key ASC)."""
        rows = [r for r in self._rows if _sitemap_row_eligible(r)]
        rows.sort(
            key=lambda r: (
                r["pivota_signature_id"] is None,  # NULLS LAST
                r["pivota_signature_id"] or "",
                r["content_key"],
                r["product_key"],
            )
        )
        rows.sort(key=lambda r: r["content_changed_at"], reverse=True)
        return rows

    async def fetch_all(self, query):
        # Used by the list endpoint; return rows passing the (flag-aware)
        # sitemap gate in sort order, honoring the compiled LIMIT/OFFSET
        # and (if present) the keyset seek predicate.
        try:
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            return []
        self.compiled_sql.append(sql)
        # Crude limit/offset parse.
        import re
        m_lim = re.search(r"LIMIT\s+(\d+)", sql, re.I)
        m_off = re.search(r"OFFSET\s+(\d+)", sql, re.I)
        lim = int(m_lim.group(1)) if m_lim else 200
        off = int(m_off.group(1)) if m_off else 0
        rows = self._eligible_sorted()

        # Keyset seek: the route compiles `content_changed_at < '<ts>'
        # OR (... pivota_signature_id > '<sig>') OR (... content_key >
        # '<ck>') OR (... product_key > '<pk>') OR (... sig IS NULL)`.
        # Extract the literals and apply the same "strictly after cursor
        # position" filter.
        m_ts = re.search(r"content_changed_at\s*<\s*'([^']+)'", sql)
        if m_ts:
            ts = datetime.fromisoformat(m_ts.group(1))
            sig = re.search(r"pivota_signature_id\s*>\s*'([^']+)'", sql).group(1)
            ck = re.search(r"content_key\s*>\s*'([^']+)'", sql).group(1)
            pk = re.search(r"product_key\s*>\s*'([^']+)'", sql).group(1)

            def _after_cursor(r: Dict[str, Any]) -> bool:
                if r["content_changed_at"] != ts:
                    return r["content_changed_at"] < ts
                if r["pivota_signature_id"] is None:
                    # NULLS LAST: always past a non-null cursor sig.
                    return True
                if r["pivota_signature_id"] != sig:
                    return r["pivota_signature_id"] > sig
                if r["content_key"] != ck:
                    return r["content_key"] > ck
                return r["product_key"] > pk

            rows = [r for r in rows if _after_cursor(r)]

        return rows[off : off + lim]

    async def fetch_val(self, query):
        try:
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        except Exception:
            sql = ""
        self.compiled_sql.append(sql)
        # COUNT — total of rows passing the (flag-aware) sitemap eligibility gate.
        return len([r for r in self._rows if _sitemap_row_eligible(r)])


class SlowDb:
    async def fetch_one(self, query):
        await asyncio.sleep(0.1)
        return None

    async def fetch_val(self, query):
        await asyncio.sleep(0.1)
        return 0

    async def fetch_all(self, query):
        await asyncio.sleep(0.1)
        return []


def _row(sig_suffix: str, **overrides) -> Dict[str, Any]:
    base = {
        "product_key": f"prod::merch_a::shopify::{sig_suffix}",
        "merchant_id": "merch_a",
        "merchant_indexable": True,
        "merchant_status": "active",
        "platform": "shopify",
        "source_product_id": sig_suffix,
        "title": f"Product {sig_suffix}",
        "brand": "Test Brand",
        "product_type": "face mask",
        "canonical_url": f"https://example.com/p/{sig_suffix}",
        "image_url": f"https://example.com/img/{sig_suffix}.jpg",
        "product_payload": {"id": sig_suffix, "handle": sig_suffix},
        "pivota_signature_id": f"sig_{sig_suffix}",
        "pivota_canonical_url": f"https://agent.pivota.cc/products/sig_{sig_suffix}",
        "content_key": f"ck_{sig_suffix}",
        # Migration 181: the ONE elected sig for this content_key. Defaults to
        # SELF, which is the shape of the 4,054 content_keys carrying a single
        # candidate; override it to model a member of a duplicate group.
        "canonical_sig_id": f"sig_{sig_suffix}",
        "serving_eligible": True,
        # Row-grain PDP renderability (approved + live_read_enabled identity
        # listing). The feed exposes it; it does not filter on it.
        "renderable": True,
        "suppressed_at": None,
        "index_eligible": False,
        "blocker_code": None,
        "blocker_detail": None,
        "content_quality_score": 91.0,
        "quality_scored_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 7, tzinfo=timezone.utc),
        "content_changed_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    from routes import pivota_canonical_routes as pcr

    rows = [
        _row("abc"),
        # Eligible but its PDP would serve the generic shell (no approved
        # live-read identity listing) — the advertised-but-dead shape. The
        # feed still lists it, carrying renderable=False for the consumer.
        _row("def", brand="Other Brand", renderable=False),
        _row("xyz", title="No-image SKU", image_url=None, serving_eligible=False, blocker_code="no_image"),
        _row("noimg", title="Eligible No-image SKU", image_url=None),
        # one row without a sig to confirm the list endpoint filters it out
        {
            **_row("nosig"),
            "pivota_signature_id": None,
            "pivota_canonical_url": None,
        },
        # Otherwise-eligible row whose merchant is gated to non-indexable
        # (catalog_merchants.indexable = FALSE). Must be excluded from
        # both the list endpoint and the single-sig resolver.
        _row("hidden", merchant_id="merch_test", merchant_indexable=False),
        # Otherwise-eligible row whose merchant is inactive
        # (catalog_merchants.status != 'active'). Same exclusion contract.
        _row("inactive", merchant_id="merch_off", merchant_status="inactive"),
        # Store-less brand-authored row: offer-free citation, no minted sig AND
        # NO catalog_merchants row (merchant_indexable=None models the missing
        # LEFT-JOIN row). Excluded when strict; included (keyed on content_key)
        # when INDEX_ELIGIBLE_SITEMAP widens the gate.
        {
            **_row("storeless"),
            "pivota_signature_id": None,
            "pivota_canonical_url": None,
            "serving_eligible": False,
            "index_eligible": True,
            "merchant_indexable": None,
        },
        # A PRESENT-but-hidden retail merchant (indexable=False) that is also
        # index_eligible must STILL be excluded when widened — only MISSING
        # merchant rows are allowed through, not explicitly-hidden ones.
        {
            **_row("hidden_index", merchant_id="merch_hide2"),
            "serving_eligible": False,
            "index_eligible": True,
            "merchant_indexable": False,
        },
        # Suppressed row (catalog_products.suppressed_at set, e.g. the
        # demo_retired sweep): withdrawn from serving — excluded from the
        # list AND 404 on the resolver even though serving_eligible is TRUE.
        _row("supp", suppressed_at=datetime(2026, 7, 1, tzinfo=timezone.utc)),
    ]
    monkeypatch.setattr(pcr, "database", FakeDb(rows))

    app = FastAPI()
    app.include_router(pcr.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /api/canonical/products/{sig_id}
# ---------------------------------------------------------------------------


def test_get_canonical_pdp_returns_product_for_existing_sig(env):
    client = env
    res = client.get("/api/canonical/products/sig_abc")
    assert res.status_code == 200
    body = res.json()
    p = body["product"]
    assert p["title"] == "Product abc"
    assert p["brand"] == "Test Brand"
    assert p["canonical_url"] == "https://agent.pivota.cc/products/sig_abc"
    assert p["merchant_canonical_url"] == "https://example.com/p/abc"
    assert p["platform"] == "shopify"


def test_get_canonical_pdp_404_for_unknown_sig(env):
    client = env
    res = client.get("/api/canonical/products/sig_doesnotexist")
    assert res.status_code == 404
    assert res.json()["detail"]["sig_id"] == "sig_doesnotexist"


def test_get_canonical_pdp_400_for_malformed_sig(env):
    client = env
    res = client.get("/api/canonical/products/not_a_sig")
    assert res.status_code == 400
    assert "sig_" in res.json()["detail"]


def test_get_canonical_pdp_handles_missing_image_gracefully_for_eligible_rows(env):
    client = env
    res = client.get("/api/canonical/products/sig_noimg")
    assert res.status_code == 200
    p = res.json()["product"]
    assert p["title"] == "Eligible No-image SKU"
    assert p["image_url"] is None
    assert p["main_image_url"] is None


def test_get_canonical_pdp_404_for_blocked_sig(env):
    client = env
    res = client.get("/api/canonical/products/sig_xyz")
    assert res.status_code == 404
    assert res.json()["detail"]["sig_id"] == "sig_xyz"


def test_get_canonical_pdp_returns_404_for_non_indexable_merchant(env):
    client = env
    res = client.get("/api/canonical/products/sig_hidden")
    assert res.status_code == 404
    assert res.json()["detail"]["sig_id"] == "sig_hidden"


# ---------------------------------------------------------------------------
# /api/canonical/products (list)
# ---------------------------------------------------------------------------


def test_list_canonical_pdps_returns_only_serving_eligible_rows_with_sig(env):
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200
    body = res.json()
    # abc/def/noimg are serving eligible; xyz has a sig but is blocked.
    assert body["total"] == 3
    assert body["has_more"] is False
    assert len(body["items"]) == 3
    sigs = [item["sig_id"] for item in body["items"]]
    assert all(s.startswith("sig_") for s in sigs)
    assert "sig_xyz" not in sigs
    assert "sig_nosig" not in sigs
    for item in body["items"]:
        assert item["serving_eligible"] is True
        assert item["content_key"].startswith("ck_")
        assert item["blocker_code"] is None
        assert item["quality_scored_at"] == "2026-05-07T00:00:00+00:00"
        assert item["last_modified"] == "2026-05-01T00:00:00+00:00"


def test_list_canonical_pdps_excludes_storeless_when_sitemap_strict(
    env, monkeypatch: pytest.MonkeyPatch
):
    # Flag OFF (default): the offer-free brand-authored row (no sig) is NOT in
    # the sitemap — prior behavior preserved.
    monkeypatch.delenv("INDEX_ELIGIBLE_SITEMAP", raising=False)
    body = env.get("/api/canonical/products").json()
    assert body["total"] == 3
    assert "ck_storeless" not in [i["content_key"] for i in body["items"]]


def test_list_canonical_pdps_includes_storeless_when_sitemap_widened(
    env, monkeypatch: pytest.MonkeyPatch
):
    # Flag ON: the store-less brand-authored row (index_eligible, null sig) is
    # included, keyed on content_key; a serving row without a sig still is not.
    monkeypatch.setenv("INDEX_ELIGIBLE_SITEMAP", "1")
    res = env.get("/api/canonical/products")
    assert res.status_code == 200
    body = res.json()
    # 3 serving+sig rows + the 1 offer-free citation row
    assert body["total"] == 4
    by_ck = {i["content_key"]: i for i in body["items"]}
    assert "ck_nosig" not in by_ck  # serving but sig-less → still excluded
    # present-but-hidden retail merchant stays out even though index_eligible
    assert "ck_hidden_index" not in by_ck
    storeless = by_ck["ck_storeless"]
    assert storeless["sig_id"] is None
    assert storeless["serving_eligible"] is False
    assert storeless["index_eligible"] is True
    assert storeless["canonical_url"] == "https://agent.pivota.cc/products/ck_storeless"
    # serving rows now also carry the index_eligible flag (False) for the UI gate
    assert by_ck["ck_abc"]["index_eligible"] is False


def test_list_canonical_pdps_excludes_non_indexable_merchant(env):
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200

    body = res.json()
    sigs = [item["sig_id"] for item in body["items"]]
    assert "sig_hidden" not in sigs
    assert body["total"] == 3


def test_list_canonical_pdps_excludes_inactive_merchant(env):
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200

    body = res.json()
    sigs = [item["sig_id"] for item in body["items"]]
    assert "sig_inactive" not in sigs
    assert body["total"] == 3


def test_list_canonical_pdps_carries_renderable_without_filtering(env):
    # Option (a) of the dead-PDP fix: the feed EXPOSES renderability so the
    # sitemap generator can filter, but the endpoint itself does NOT shrink —
    # existing consumers keep seeing every serving_eligible row.
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200
    body = res.json()
    by_sig = {i["sig_id"]: i for i in body["items"]}
    assert by_sig["sig_abc"]["renderable"] is True
    assert by_sig["sig_def"]["renderable"] is False  # listed, flagged dead
    assert body["total"] == 3


def test_suppressed_row_excluded_from_list_and_resolver(env):
    client = env
    body = client.get("/api/canonical/products").json()
    assert "sig_supp" not in [i["sig_id"] for i in body["items"]]
    assert body["total"] == 3  # suppressed row does not count as advertised
    res = client.get("/api/canonical/products/sig_supp")
    assert res.status_code == 404


def test_list_canonical_pdps_last_modified_uses_content_changed_at(env):
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200

    by_sig = {item["sig_id"]: item for item in res.json()["items"]}
    assert by_sig["sig_abc"]["last_modified"] == "2026-05-01T00:00:00+00:00"


def test_list_canonical_pdps_pagination_bounds(env):
    client = env
    res = client.get("/api/canonical/products?limit=2&offset=0")
    body = res.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["has_more"] is True
    assert body["next_cursor"]

    res2 = client.get("/api/canonical/products?limit=2&offset=2")
    body2 = res2.json()
    assert len(body2["items"]) == 1
    # COUNT(*) runs on the first page only; later pages return total=null
    # and consumers page on has_more (or items.length >= limit).
    assert body2["total"] is None
    assert body2["has_more"] is False
    assert body2["next_cursor"] is None


def test_list_canonical_pdps_counts_only_on_first_page(env):
    client = env
    from routes import pivota_canonical_routes as pcr

    client.get("/api/canonical/products?limit=2&offset=0")
    client.get("/api/canonical/products?limit=2&offset=2")

    count_queries = [s for s in pcr.database.compiled_sql if "count(" in s.lower()]
    assert len(count_queries) == 1


def test_list_canonical_pdps_cursor_pagination_walks_all_rows(env):
    client = env
    res = client.get("/api/canonical/products?limit=2")
    body = res.json()
    assert body["total"] == 3
    assert body["has_more"] is True
    cursor = body["next_cursor"]
    assert cursor

    res2 = client.get(f"/api/canonical/products?limit=2&cursor={cursor}")
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["total"] is None
    assert body2["has_more"] is False
    assert body2["next_cursor"] is None

    # Cursor mode must not COUNT and must not OFFSET-scan.
    from routes import pivota_canonical_routes as pcr

    cursor_sql = pcr.database.compiled_sql[-1]
    assert "count(" not in cursor_sql.lower()
    assert "OFFSET" not in cursor_sql.upper()
    assert "content_changed_at <" in cursor_sql

    # Both pages together cover every eligible sig exactly once.
    sigs = [i["sig_id"] for i in body["items"]] + [i["sig_id"] for i in body2["items"]]
    assert sorted(sigs) == ["sig_abc", "sig_def", "sig_noimg"]
    assert len(set(sigs)) == 3


def test_list_canonical_pdps_cursor_seeks_across_timestamp_boundary(env, monkeypatch):
    """Rows with distinct content_changed_at values page correctly:
    newest first, and the cursor resumes on the older-timestamp side."""
    from routes import pivota_canonical_routes as pcr

    rows = [
        _row("old", content_changed_at=datetime(2026, 4, 1, tzinfo=timezone.utc)),
        _row("new", content_changed_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
        _row("mid", content_changed_at=datetime(2026, 5, 1, tzinfo=timezone.utc)),
    ]
    monkeypatch.setattr(pcr, "database", FakeDb(rows))
    app = FastAPI()
    app.include_router(pcr.router)
    client = TestClient(app)

    res = client.get("/api/canonical/products?limit=1")
    body = res.json()
    assert [i["sig_id"] for i in body["items"]] == ["sig_new"]
    assert body["has_more"] is True

    res2 = client.get(f"/api/canonical/products?limit=1&cursor={body['next_cursor']}")
    body2 = res2.json()
    assert [i["sig_id"] for i in body2["items"]] == ["sig_mid"]
    assert body2["has_more"] is True

    res3 = client.get(f"/api/canonical/products?limit=1&cursor={body2['next_cursor']}")
    body3 = res3.json()
    assert [i["sig_id"] for i in body3["items"]] == ["sig_old"]
    assert body3["has_more"] is False
    assert body3["next_cursor"] is None


def test_list_canonical_pdps_rejects_cursor_with_offset(env):
    client = env
    res = client.get("/api/canonical/products?limit=2")
    cursor = res.json()["next_cursor"]

    res2 = client.get(f"/api/canonical/products?offset=2&cursor={cursor}")
    assert res2.status_code == 400
    assert "not both" in res2.json()["detail"]


def test_list_canonical_pdps_rejects_malformed_cursor(env):
    client = env
    for bad in ["garbage", "eyJ2IjoyfQ", "aGVsbG8"]:  # junk, wrong version, non-JSON-object
        res = client.get(f"/api/canonical/products?cursor={bad}")
        assert res.status_code == 400, bad
        assert "cursor" in res.json()["detail"]


def test_list_canonical_pdps_uses_index_pipeline_state_join(env):
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200

    from routes import pivota_canonical_routes as pcr

    sql = "\n".join(pcr.database.compiled_sql)
    assert "JOIN index_pipeline_state" in sql
    assert "JOIN catalog_merchants" in sql
    assert "serving_eligible IS true" in sql
    assert "catalog_merchants.indexable IS true" in sql
    # ADR-009 amendment: gate = "not disabled" (active OR observed);
    # a plain 'active'-only equality would darken observed sellers.
    assert "catalog_merchants.status IN" in sql
    assert "catalog_merchants.status = 'active'" not in sql
    assert "ORDER BY catalog_products.content_changed_at DESC" in sql
    assert "catalog_products.pivota_signature_id ASC" in sql
    assert "catalog_products.content_key ASC" in sql
    assert "catalog_products.product_key ASC" in sql
    # Renderability is a CONTENT-ROUTE question, not an identity one. The
    # identity join this used to assert on was measured wrong in BOTH
    # directions on 2026-07-25 (rows with no listing at all render 200; rows
    # with a perfect listing 500) — see services/pdp_renderability. Semantics
    # now live in tests/test_pdp_renderability.py; here we only pin that the
    # feed asks the shared question and no longer asks the identity one.
    assert "pdp_identity_listing" not in sql
    assert "live_read_enabled" not in sql
    assert "external_product_seeds" in sql
    # Suppressed rows are withdrawn from the advertised feed.
    assert "catalog_products.suppressed_at IS NULL" in sql


def test_renderable_excludes_rows_whose_external_seed_is_not_active(env):
    """The second way a PDP fails to render — and the cause of the 500s.

    get_pdp_v2 runs an external-seed status precheck BEFORE identity
    resolution and hard-404s (reason=external_seed_not_active) on any seed row
    whose status is not 'active'. Those rows pass every other gate
    (serving_eligible, priced offers, agent_pdp_view, approved + live_read
    identity listing), so `renderable` reported True and the sitemap kept
    advertising them. After pivota-agent-ui#269 made canonical PDPs
    static/ISR, each one served a hard HTTP 500 instead of a thin 200 shell —
    127 of the 1,901 live sitemap URLs, measured 2026-07-25.
    """
    client = env
    res = client.get("/api/canonical/products")
    assert res.status_code == 200

    from routes import pivota_canonical_routes as pcr

    sql = " ".join(" ".join(pcr.database.compiled_sql).split())
    assert "external_product_seeds" in sql
    # The seed subquery must be CORRELATED: its FROM names only
    # external_product_seeds, with catalog_products supplied by the outer
    # query. If catalog_products appeared in that FROM the subquery would be a
    # cartesian product and `renderable` would collapse into a global constant
    # instead of a per-row answer. Whitespace is normalized above so this does
    # not depend on SQLAlchemy's line breaks.
    assert "FROM external_product_seeds WHERE catalog_products" in sql


# ---------------------------------------------------------------------------
# The executable semantics for `renderable` moved to tests/test_pdp_renderability.py
# when the predicate moved to services/pdp_renderability — it is now shared with
# the `public_not_renderable` invariant and the trust policy, and all three
# twins are pinned against one row matrix there.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# ADR-008 SLICE 1: by-signature READ vs sitemap LISTING flag separation
# ---------------------------------------------------------------------------


def _client_for_sql(monkeypatch: pytest.MonkeyPatch):
    from routes import pivota_canonical_routes as pcr

    db = FakeDb([_row("abc")])
    monkeypatch.setattr(pcr, "database", db)
    app = FastAPI()
    app.include_router(pcr.router)
    return TestClient(app), pcr, db


def test_sitemap_not_widened_by_index_eligible_read_alone(monkeypatch: pytest.MonkeyPatch):
    """INDEX_ELIGIBLE_READ widens the by-signature PDP read but MUST NOT widen
    the public /products sitemap listing — that needs its own flag."""
    monkeypatch.setenv("INDEX_ELIGIBLE_READ", "true")
    monkeypatch.delenv("INDEX_ELIGIBLE_SITEMAP", raising=False)
    client, pcr, db = _client_for_sql(monkeypatch)

    # by-signature read: widened (carries index_eligible)
    db.compiled_sql.clear()
    client.get("/api/canonical/products/sig_abc")
    read_sql = "\n".join(db.compiled_sql)
    assert "index_pipeline_state.index_eligible" in read_sql

    # sitemap listing: NOT widened (serving-only)
    db.compiled_sql.clear()
    res = client.get("/api/canonical/products")
    assert res.status_code == 200
    list_sql = "\n".join(db.compiled_sql)
    assert "index_pipeline_state.index_eligible" not in list_sql
    assert "serving_eligible IS true" in list_sql


def test_sitemap_widened_only_by_index_eligible_sitemap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INDEX_ELIGIBLE_READ", raising=False)
    monkeypatch.setenv("INDEX_ELIGIBLE_SITEMAP", "true")
    client, pcr, db = _client_for_sql(monkeypatch)

    # sitemap listing: widened
    db.compiled_sql.clear()
    res = client.get("/api/canonical/products")
    assert res.status_code == 200
    list_sql = "\n".join(db.compiled_sql)
    assert "index_pipeline_state.index_eligible" in list_sql

    # by-signature read: NOT widened (its flag is off)
    db.compiled_sql.clear()
    client.get("/api/canonical/products/sig_abc")
    read_sql = "\n".join(db.compiled_sql)
    assert "index_pipeline_state.index_eligible" not in read_sql


def test_both_flags_off_is_serving_only_byte_identical(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INDEX_ELIGIBLE_READ", raising=False)
    monkeypatch.delenv("INDEX_ELIGIBLE_SITEMAP", raising=False)
    client, pcr, db = _client_for_sql(monkeypatch)

    db.compiled_sql.clear()
    client.get("/api/canonical/products/sig_abc")
    client.get("/api/canonical/products")
    sql = "\n".join(db.compiled_sql)
    assert "index_pipeline_state.index_eligible" not in sql
    assert "serving_eligible IS true" in sql


def test_list_canonical_pdps_rejects_oversized_limit(env):
    """Cap at 1000 to keep response sizes sane."""
    client = env
    res = client.get("/api/canonical/products?limit=10000")
    assert res.status_code == 422


def test_get_canonical_pdp_times_out_slow_database(monkeypatch: pytest.MonkeyPatch):
    from routes import pivota_canonical_routes as pcr

    monkeypatch.setattr(pcr, "database", SlowDb())
    monkeypatch.setattr(pcr, "CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS", 0.01)

    app = FastAPI()
    app.include_router(pcr.router)
    client = TestClient(app)

    res = client.get("/api/canonical/products/sig_abc")

    assert res.status_code == 504
    assert res.json()["detail"]["operation"] == "product_by_signature"


def test_list_canonical_pdps_times_out_slow_database(monkeypatch: pytest.MonkeyPatch):
    from routes import pivota_canonical_routes as pcr

    monkeypatch.setattr(pcr, "database", SlowDb())
    monkeypatch.setattr(pcr, "CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS", 0.01)

    app = FastAPI()
    app.include_router(pcr.router)
    client = TestClient(app)

    res = client.get("/api/canonical/products")

    assert res.status_code == 504
    assert res.json()["detail"]["operation"] == "product_signature_count"
