"""HTTP tests for the external citation read API (routes.agent_citation_v1)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.pivot_query_service as pqs
from routes import agent_citation_v1 as cite

_CK_A = "ck_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_CK_B = "ck_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_SIG_A = "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_SIG_ITEM = "sig_0123456789abcdef0123456789abcdef"


def _citable_rows():
    return [
        {
            "content_key": _CK_A,
            # Threaded by _fetch_citable_canonical_rows as
            # COALESCE(apv.pivota_signature_id, p.pivota_signature_id).
            "pivota_signature_id": _SIG_A,
            "product_title": "Anuko Nourishing Hair Butter",
            "product_description": "Shea butter treatment. For damaged hair.",
            "brand": "Anuko",
            "product_image_url": "https://img.example/anuko.jpg",
        },
        {
            # No sig minted → null attribution URL, never the ck form.
            "content_key": _CK_B,
            "pivota_signature_id": None,
            "product_title": "SKIN1004 Centella Ampoule",
            "product_description": "Centella ampoule.",
            "brand": "SKIN1004",
            "product_image_url": None,
        },
    ]


def _row(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "content_key": "ck_0123456789abcdef0123456789abcdef",
        "pivota_signature_id": _SIG_ITEM,
        "title": "Anuko Nourishing Hair Butter",
        "brand": "Anuko",
        "description": "A rich shea butter treatment for damaged hair. Mixed berry scent.",
        "bullet_points": ["Shea butter + green tea", "For damaged hair"],
        "usage_scenarios": ["Apply to damp hair"],
        "taxonomy_tags": ["haircare", "treatment"],
        "image_url": "https://img.example/anuko.jpg",
        "evidence_profile": {
            "claims": [
                {
                    "claim_text": "Nourishes damaged hair",
                    "source_type": "ingredient_mechanism",
                    "substantiation_status": "substantiated",
                }
            ],
            "review_state": "observed",
        },
        "required_disclaimers": [],
    }
    base.update(overrides)
    return base


class FakeDb:
    """Stub DB for the citation route.

    ``fetch_val`` MUST exist even though only the renderability check uses it.
    It did not, at first, and the consequence is worth recording: the route's
    check is deliberately fail-closed (`except Exception -> False`), so a missing
    method raised AttributeError, got swallowed, and every assertion about
    `url_renderable` passed while the predicate never ran once. A fail-closed
    guard and an incomplete stub silently agree on False.

    ``renderable`` also accepts an exception instance, so the fail-closed path
    can be tested on purpose rather than by accident.
    """

    def __init__(
        self,
        row: Optional[Dict[str, Any]],
        *,
        renderable: Any = True,
        elected_sig: Any = None,
    ) -> None:
        self._row = row
        self._renderable = renderable
        self._elected_sig = elected_sig
        self.fetch_val_calls: list[Dict[str, Any]] = []

    async def fetch_one(self, query: Any, params: Any = None) -> Optional[Dict[str, Any]]:
        return self._row

    async def fetch_val(self, query: Any, params: Any = None) -> Any:
        self.fetch_val_calls.append({"query": query, "params": params})
        # The route issues two DIFFERENT fetch_val queries; keying on the target
        # table keeps this stub from answering one with the other's value.
        is_election = "content_canonical_election" in str(query)
        value = self._elected_sig if is_election else self._renderable
        if isinstance(value, BaseException):
            raise value
        return value

    @property
    def renderable_calls(self) -> list[Dict[str, Any]]:
        return [
            c
            for c in self.fetch_val_calls
            if "content_canonical_election" not in str(c["query"])
        ]

    @property
    def election_calls(self) -> list[Dict[str, Any]]:
        return [
            c
            for c in self.fetch_val_calls
            if "content_canonical_election" in str(c["query"])
        ]


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch):
    def _make(
        row: Optional[Dict[str, Any]],
        *,
        renderable: Any = True,
        elected_sig: Any = None,
    ) -> TestClient:
        monkeypatch.setenv("INDEX_ELIGIBLE_READ", "1")
        db = FakeDb(row, renderable=renderable, elected_sig=elected_sig)
        monkeypatch.setattr(cite, "database", db)
        app = FastAPI()
        app.include_router(cite.router)
        client = TestClient(app)
        client.fake_db = db  # type: ignore[attr-defined]
        return client

    return _make


def test_citation_item_shape_and_invariants(client_for):
    res = client_for(_row()).get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
    assert res.status_code == 200
    body = res.json()
    assert body["content_key"] == "ck_0123456789abcdef0123456789abcdef"
    assert body["title"].startswith("Anuko")
    assert body["brand"] == "Anuko"
    # offer-free invariants (citation, not commerce)
    assert body["buyable"] is False
    assert body["offers"] is None
    assert body["catalog_track"] == "citation"
    # attribution — the moat. The cited URL is the SIG form: the content_key form
    # 500s at the gateway for every row (135/135 measured in prod 2026-07-26),
    # which made every citation unfollowable while still demanding attribution.
    assert body["attribution"]["source"] == "Pivota"
    assert (
        body["attribution"]["canonical_url"]
        == f"https://agent.pivota.cc/products/{_SIG_ITEM}"
    )
    assert body["attribution"]["cite_as"] == "Pivota — agent.pivota.cc"
    assert body["attribution"]["attribution_required"] is True
    # substantiation — claims present, coverage disclosed (not faked)
    assert len(body["substantiation"]["claims"]) >= 1
    assert body["substantiation"]["verify_coverage"] is None
    # one-line summary an agent can quote
    assert body["summary"] and body["summary"].endswith(".")
    # cacheable
    assert "max-age" in res.headers.get("cache-control", "")


def test_no_merchant_private_or_commerce_fields_leak(client_for):
    res = client_for(_row(primary_merchant_id="merch_secret", price_min=42)).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    )
    body = res.json()
    assert "merch_secret" not in str(body)
    assert "primary_merchant_id" not in body
    assert "price" not in body and "price_min" not in body
    assert body["offers"] is None


def test_404_when_row_missing(client_for):
    res = client_for(None).get("/agent/v1/citation/ck_ffffffffffffffffffffffffffffffff")
    assert res.status_code == 404


def test_rate_limit_returns_429_with_retry_after(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())

    async def deny(key: str, tier: str = "standard"):
        return False, {"limit": 1000, "remaining": 0, "reset": 9999999999}

    monkeypatch.setattr(cite._limiter, "check_limit", deny)
    res = client.get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
    assert res.status_code == 429
    assert res.headers.get("Retry-After")


def test_unknown_id_shape_is_404(client_for):
    # A value that isn't a content_key / sig / pg / ext resolves to no SQL → 404.
    res = client_for(_row()).get("/agent/v1/citation/not-a-real-id")
    assert res.status_code == 404


# ── attribution.canonical_url: the sig form, never the content_key form ──────
#
# The regression these pin is not hypothetical: shipping
# `PDP_URL_PREFIX + content_key` made attribution.canonical_url a hard 500 for
# EVERY row this endpoint serves (135/135 content_keys measured in prod
# 2026-07-26), including rows whose own sig-form PDP serves a real page — while
# the same response carried attribution_required: true.


def test_attribution_url_is_never_the_content_key_form(client_for):
    """The ck form 500s at the gateway. It must not appear, in any field."""
    ck = "ck_0123456789abcdef0123456789abcdef"
    body = client_for(_row()).get(f"/agent/v1/citation/{ck}").json()
    assert f"/products/{ck}" not in str(body)
    assert body["attribution"]["canonical_url"] == f"https://agent.pivota.cc/products/{_SIG_ITEM}"
    # content_key is still carried as an IDENTIFIER — it is only invalid as a URL.
    assert body["content_key"] == ck


def test_attribution_url_is_null_without_a_minted_sig(client_for):
    """No sig ⇒ no followable PDP. Null, not a ck URL that is certain to 500.

    attribution_required STAYS true and cite_as is unchanged: attribution is to
    the SOURCE, which an agent can honour by name without a deep link. Dropping
    the requirement here would hand away the moat on exactly the rows we cannot
    yet link to.
    """
    body = (
        client_for(_row(pivota_signature_id=None))
        .get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
        .json()
    )
    assert body["attribution"]["canonical_url"] is None
    assert body["attribution"]["attribution_required"] is True
    assert body["attribution"]["cite_as"] == "Pivota — agent.pivota.cc"
    assert "/products/" not in str(body)


# ── attribution.url_renderable: is the cited URL actually followable? ────────


def test_url_renderable_true_when_the_pdp_will_render(client_for):
    client = client_for(_row(), renderable=True)
    body = client.get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef").json()
    assert body["attribution"]["url_renderable"] is True
    # The predicate must actually have been consulted, keyed on the SAME sig the
    # URL is built from. Without this the fail-closed `except` can hide a
    # never-executed check (it did, before FakeDb grew fetch_val).
    calls = client.fake_db.fetch_val_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert calls[0]["params"] == {"sig": _SIG_ITEM}
    assert "catalog_products" in calls[0]["query"]
    assert "index_pipeline_state" in calls[0]["query"]


def test_url_renderable_false_when_the_pdp_will_not_render(client_for):
    """The honesty seam: content still served, link flagged unfollowable.

    879 of 5,887 live feed rows are in this state. The row must NOT be withheld —
    ADR-007 SLICE 1 exists to keep offer-free rows citable — so the URL stays and
    only the flag goes false.
    """
    client = client_for(_row(), renderable=False)
    body = client.get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef").json()
    assert body["attribution"]["url_renderable"] is False
    # …and the citation is still fully usable as a CITATION.
    assert body["attribution"]["canonical_url"] == f"https://agent.pivota.cc/products/{_SIG_ITEM}"
    assert body["attribution"]["attribution_required"] is True
    assert body["attribution"]["cite_as"] == "Pivota — agent.pivota.cc"
    assert body["title"] and body["description"]


def test_url_renderable_is_null_not_false_when_there_is_no_url(client_for):
    """No URL to characterise ⇒ null. False would imply we checked a link."""
    client = client_for(_row(pivota_signature_id=None), renderable=True)
    body = client.get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef").json()
    assert body["attribution"]["canonical_url"] is None
    assert body["attribution"]["url_renderable"] is None
    assert body["attribution"]["url_source"] is None
    # No sig ⇒ the renderability predicate is not worth a round trip…
    assert client.fake_db.renderable_calls == []  # type: ignore[attr-defined]
    # …but the ELECTION still is: a sig-less row can have an elected sibling that
    # renders, and that sibling's URL beats null. Here there is no election, so
    # it degrades to null.
    assert len(client.fake_db.election_calls) == 1  # type: ignore[attr-defined]


# ── Step 2: substitute the elected renderable sibling ────────────────────────
#
# 229 of the 245 route-broken content_keys have a renderable sibling (12/12
# sampled siblings served real 200s), so most dead citations can be given a live
# URL rather than just a false flag. INERT until content_canonical_election is
# seeded — it is empty in prod today — which is why every test here supplies the
# election explicitly.

_SIB = "sig_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_dead_own_pdp_cites_the_elected_sibling(client_for):
    client = client_for(_row(), renderable=False, elected_sig=_SIB)
    att = client.get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()["attribution"]
    assert att["canonical_url"] == f"https://agent.pivota.cc/products/{_SIB}"
    # Electability implies renderable, so the substituted URL is followable.
    assert att["url_renderable"] is True
    # …and the substitution is DISCLOSED, never silent.
    assert att["url_source"] == "elected_canonical"


def test_sig_less_row_also_gets_the_elected_sibling(client_for):
    """No own URL at all is exactly when a sibling's URL is most valuable."""
    client = client_for(_row(pivota_signature_id=None), elected_sig=_SIB)
    att = client.get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()["attribution"]
    assert att["canonical_url"] == f"https://agent.pivota.cc/products/{_SIB}"
    assert att["url_renderable"] is True
    assert att["url_source"] == "elected_canonical"


def test_renderable_row_keeps_its_own_url_and_skips_the_election(client_for):
    """Rung 1 must win, and must not pay for a lookup it cannot use."""
    client = client_for(_row(), renderable=True, elected_sig=_SIB)
    att = client.get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()["attribution"]
    assert att["canonical_url"] == f"https://agent.pivota.cc/products/{_SIG_ITEM}"
    assert att["url_source"] == "self"
    assert client.fake_db.election_calls == []  # type: ignore[attr-defined]


def test_unseeded_election_degrades_to_the_flagged_own_url(client_for):
    """Today's prod state: table empty ⇒ behaviour identical to Step 1."""
    client = client_for(_row(), renderable=False, elected_sig=None)
    att = client.get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()["attribution"]
    assert att["canonical_url"] == f"https://agent.pivota.cc/products/{_SIG_ITEM}"
    assert att["url_renderable"] is False
    assert att["url_source"] == "self"


def test_election_lookup_failure_degrades_rather_than_500s(client_for):
    client = client_for(_row(), renderable=False, elected_sig=RuntimeError("pg down"))
    res = client.get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
    assert res.status_code == 200
    att = res.json()["attribution"]
    assert att["url_renderable"] is False
    assert att["url_source"] == "self"


@pytest.mark.parametrize("junk", ["", "   ", "sig_", "ck_abc", "not-a-sig"])
def test_unusable_elected_sig_is_ignored(client_for, junk):
    """A stored winner that is not a usable sig must never become a URL."""
    client = client_for(_row(pivota_signature_id=None), elected_sig=junk)
    att = client.get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()["attribution"]
    assert att["canonical_url"] is None
    assert att["url_source"] is None


def test_url_renderable_fails_closed_when_the_check_errors(client_for):
    """A DB error must read as 'do not follow', never as followable."""
    client = client_for(_row(), renderable=RuntimeError("pg down"))
    body = client.get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef").json()
    assert body["attribution"]["url_renderable"] is False
    # The row is still served — a renderability check failure must not 500 a read.
    assert body["title"].startswith("Anuko")


def test_search_items_carry_url_renderable_from_the_recall_row(
    client_for, monkeypatch: pytest.MonkeyPatch
):
    """Search gets the flag inline from its own catalog_products lane."""
    client = client_for(_row())

    async def fake_fetch(*, query, merchant_id, limit):
        rows = _citable_rows()
        rows[0]["pdp_renderable"] = True
        rows[1]["pdp_renderable"] = False
        return rows

    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    monkeypatch.setattr(pqs, "_fetch_citable_canonical_rows", fake_fetch)
    items = client.get("/agent/v1/citation/search?q=hair&intent=inform").json()["items"]
    assert items[0]["attribution"]["url_renderable"] is True
    # Row B has no sig at all, so it has no URL to characterise → null, and the
    # row's own pdp_renderable=False must not be mistaken for "we checked a link".
    assert items[1]["attribution"]["canonical_url"] is None
    assert items[1]["attribution"]["url_renderable"] is None


@pytest.mark.parametrize("bad_sig", ["", "   ", "sig_", "ck_abc", "not-a-sig", None])
def test_attribution_url_null_for_unusable_sig_values(client_for, bad_sig):
    """A bare "sig_" is as dead as a ck URL (/products/sig_ errors the same way),
    and it would pass a naive startswith check — the same trap agent-ui's
    sitemap_lib.mjs documents with its `/^sig_.+/` (not `^sig_`) guard."""
    body = client_for(_row(pivota_signature_id=bad_sig)).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()
    assert body["attribution"]["canonical_url"] is None


# ── /search ─────────────────────────────────────────────────────────────────


def test_search_inform_returns_citation_items(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())

    async def fake_fetch(*, query, merchant_id, limit):
        return _citable_rows()

    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    monkeypatch.setattr(pqs, "_fetch_citable_canonical_rows", fake_fetch)
    res = client.get("/agent/v1/citation/search?q=hair+butter&intent=inform")
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "inform"
    assert body["count"] == 2
    item = body["items"][0]
    # same offer-free CitationItem shape as the single-item read
    assert item["buyable"] is False
    assert item["offers"] is None
    assert item["catalog_track"] == "citation"
    assert item["attribution"]["source"] == "Pivota"
    # Sig form, and the SAME URL the single-item read would emit for this
    # content_key — that agreement is why the recall query prefers
    # apv.pivota_signature_id over whichever product_key won the rank.
    assert item["attribution"]["canonical_url"] == f"https://agent.pivota.cc/products/{_SIG_A}"
    assert item["title"] == "Anuko Nourishing Hair Butter"
    assert item["brand"] == "Anuko"
    # recall rows are light → substantiation empty (full detail via single-item)
    assert item["substantiation"]["claims"] == []


def test_search_shop_intent_suppresses_citations(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    res = client.get("/agent/v1/citation/search?q=hair&intent=shop")
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "shop"
    assert body["items"] == []


def test_search_returns_empty_when_recall_flag_off(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: False)
    res = client.get("/agent/v1/citation/search?q=hair&intent=inform")
    assert res.status_code == 200
    assert res.json()["items"] == []


def test_search_empty_query_returns_empty(client_for):
    res = client_for(_row()).get("/agent/v1/citation/search?q=")
    assert res.status_code == 200
    assert res.json()["items"] == []


# ── B④-P1 attribution telemetry wiring ───────────────────────────────────────


@pytest.fixture
def capture_logs(monkeypatch: pytest.MonkeyPatch):
    """Capture the fields each handler hands to telemetry, bypassing the real
    fire-and-forget task (deterministic, no event-loop race)."""
    calls: list[Dict[str, Any]] = []
    monkeypatch.setattr(cite, "_spawn_log", lambda **f: calls.append(f))
    return calls


def test_single_read_hit_logs_telemetry(client_for, capture_logs):
    ck = "ck_0123456789abcdef0123456789abcdef"
    res = client_for(_row()).get(f"/agent/v1/citation/{ck}")
    assert res.status_code == 200
    assert len(capture_logs) == 1
    ev = capture_logs[0]
    assert ev["endpoint"] == "item"
    assert ev["status"] == cite.STATUS_HIT
    assert ev["requested_id"] == ck
    assert ev["content_key"] == ck


def test_single_read_miss_logs_telemetry(client_for, capture_logs):
    ck = "ck_ffffffffffffffffffffffffffffffff"
    res = client_for(None).get(f"/agent/v1/citation/{ck}")
    assert res.status_code == 404
    assert len(capture_logs) == 1
    ev = capture_logs[0]
    assert ev["endpoint"] == "item"
    assert ev["status"] == cite.STATUS_MISS
    assert ev["requested_id"] == ck
    assert ev["content_key"] is None


def test_agent_header_captured_in_telemetry(client_for, capture_logs):
    client_for(_row()).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef",
        headers={"X-Pivota-Agent": "openai-chatgpt/1.0"},
    )
    assert capture_logs[-1]["agent"] == "openai-chatgpt/1.0"


def test_search_hit_logs_result_count(client_for, capture_logs, monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*, query, merchant_id, limit):
        return _citable_rows()

    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    monkeypatch.setattr(pqs, "_fetch_citable_canonical_rows", fake_fetch)
    client_for(_row()).get("/agent/v1/citation/search?q=hair&intent=inform")
    ev = capture_logs[-1]
    assert ev["endpoint"] == "search"
    assert ev["status"] == cite.STATUS_HIT
    assert ev["result_count"] == 2
    assert ev["query"] == "hair"


def test_search_shop_logs_suppressed(client_for, capture_logs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    client_for(_row()).get("/agent/v1/citation/search?q=hair&intent=shop")
    assert capture_logs[-1]["status"] == cite.STATUS_SUPPRESSED


def test_search_recall_off_logs_disabled(client_for, capture_logs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: False)
    client_for(_row()).get("/agent/v1/citation/search?q=hair&intent=inform")
    assert capture_logs[-1]["status"] == cite.STATUS_DISABLED


def test_telemetry_disabled_by_default_spawns_no_write(client_for, monkeypatch: pytest.MonkeyPatch):
    # Real _spawn_log with the flag OFF must never reach log_citation_read.
    monkeypatch.delenv("CITATION_READ_TELEMETRY", raising=False)
    reached: list = []
    monkeypatch.setattr(cite, "log_citation_read", lambda **f: reached.append(f))
    res = client_for(_row()).get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
    assert res.status_code == 200
    assert reached == []


async def test_spawn_log_schedules_write_when_flag_on(monkeypatch: pytest.MonkeyPatch):
    # Flag ON: _spawn_log schedules the best-effort write coroutine (tested
    # directly to control the loop, avoiding a TestClient scheduling race).
    monkeypatch.setenv("CITATION_READ_TELEMETRY", "1")
    seen: list[Dict[str, Any]] = []

    async def fake_log(**fields):
        seen.append(fields)

    monkeypatch.setattr(cite, "log_citation_read", fake_log)
    cite._spawn_log(endpoint="item", status=cite.STATUS_HIT, content_key="ck_z")
    await asyncio.sleep(0)  # let the scheduled task run
    assert seen and seen[0]["endpoint"] == "item"
    assert seen[0]["content_key"] == "ck_z"


async def test_spawn_log_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CITATION_READ_TELEMETRY", raising=False)
    seen: list = []

    async def fake_log(**fields):
        seen.append(fields)

    monkeypatch.setattr(cite, "log_citation_read", fake_log)
    cite._spawn_log(endpoint="item", status=cite.STATUS_HIT)
    await asyncio.sleep(0)
    assert seen == []



def test_citation_item_emits_aggregate_rating_when_captured(client_for):
    # Migration 186 captured schema.org aggregateRating, but no external agent
    # surface projected it — Pivota's own diagnostic tells merchants to "expose
    # aggregateRating so agents can weigh social proof" while this endpoint
    # withheld it. A rating is content, not commerce, so the offer-free
    # invariants are unaffected.
    from decimal import Decimal

    row = _row(rating_value=Decimal("4.8"), rating_count=52)
    body = client_for(row).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()

    assert body["aggregate_rating"] == {"value": 4.8, "count": 52}
    assert body["buyable"] is False
    assert body["offers"] is None


def test_citation_item_aggregate_rating_null_when_uncaptured(client_for):
    # Never fabricated: NULL means "no review data on the source page".
    body = client_for(_row()).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()

    assert body["aggregate_rating"] is None


def test_citation_taxonomy_tags_json_strings_are_repaired(client_for):
    # Same double-encoding repair as the PDP surface: agents must never receive
    # a JSON-encoded string where the contract says array.
    row = _row(
        taxonomy_tags={
            "tags": '["haircare", "treatment"]',
            "use_case_tags": "[]",
            "category": "Treatment",
        }
    )
    body = client_for(row).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    ).json()

    tt = body["taxonomy_tags"]
    assert tt["tags"] == ["haircare", "treatment"]
    assert tt["use_case_tags"] == []
    assert tt["category"] == "Treatment"
