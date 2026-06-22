"""Phase 2c web-evidence collector: domain classification + candidate building are
pure; the merge/collect paths read-merge-write so they never clobber merchant
claims, and discovered claims stay UNVERIFIED (merchant confirms → served)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import db.product_evidence as pe
import services.web_evidence_collector as wec


# --- classify_web_source -----------------------------------------------------

def test_classify_editorial_review_social_unknown() -> None:
    assert wec.classify_web_source("https://www.vogue.com/article/best-serums") == ("editorial_press", "b")
    assert wec.classify_web_source("https://www.sephora.com/product/x/reviews") == ("third_party_review", "b")
    assert wec.classify_web_source("https://www.reddit.com/r/SkincareAddiction/abc")[0] == "social_mention"
    assert wec.classify_web_source("https://randomblog.example/post")[0] == "web_mention"
    assert wec.classify_web_source("not a url")[0] == "web_mention"


def test_classify_brand_owned_skipped() -> None:
    # Brand token in host, or the merchant's own host -> brand_owned (self-reference).
    assert wec.classify_web_source("https://glowco.com/products/serum", brand="GlowCo")[0] == "brand_owned"
    assert wec.classify_web_source("https://shop.glowco.com/p", merchant_host="glowco.com")[0] == "brand_owned"


def test_classify_brand_stopwords_do_not_drop_editorial() -> None:
    # "The Ordinary": the stopword "the" must NOT mark thecut.com as brand-owned.
    assert wec.classify_web_source("https://www.thecut.com/best", brand="The Ordinary")[0] == "editorial_press"


def test_classify_merchant_host_with_scheme_normalizes() -> None:
    # A merchant_host passed WITH a scheme must still exclude the merchant's own host.
    assert wec.classify_web_source("https://shop.glowco.com/p", merchant_host="https://glowco.com")[0] == "brand_owned"


# --- build_web_evidence_candidates -------------------------------------------

def test_build_candidates_keeps_press_review_drops_social_brand() -> None:
    results = [
        {"title": "The 10 Best Vitamin C Serums", "url": "https://www.allure.com/best-serums", "snippet": "..."},
        {"title": "Customer reviews", "url": "https://www.sephora.com/p/serum/reviews", "snippet": "..."},
        {"title": "Reddit thread", "url": "https://www.reddit.com/r/x/abc", "snippet": "..."},
        {"title": "Our product page", "url": "https://glowco.com/serum", "snippet": "..."},
    ]
    out = wec.build_web_evidence_candidates(results, brand="GlowCo")
    assert [c["source_type"] for c in out] == ["editorial_press", "third_party_review"]
    assert all(c["substantiation_status"] == "unverified" for c in out)
    assert out[0]["source_ref"] == "https://www.allure.com/best-serums"
    assert out[0]["discovered_via"] == "web_crawl"


def test_build_candidates_dedupes_by_host_caps_and_skips_blank() -> None:
    results = [
        {"title": "A", "url": "https://www.allure.com/a", "snippet": ""},
        {"title": "B", "url": "https://www.allure.com/b", "snippet": ""},  # same host -> dropped
        {"title": "", "url": "https://www.elle.com/x", "snippet": ""},      # blank title -> dropped
        {"title": "C", "url": "", "snippet": ""},                          # blank url -> dropped
    ]
    out = wec.build_web_evidence_candidates(results, max_claims=6)
    assert len(out) == 1 and out[0]["claim_text"] == "A"


# --- _merge_candidates_into_evidence (fake DB) -------------------------------

class _FakeDB:
    def __init__(self, row=None):
        self._row = row
        self.executed = []

    async def fetch_one(self, *a, **k):
        return self._row

    async def execute(self, sql, values):
        self.executed.append((sql, values))


async def _noop_ensure() -> None:
    return None


async def test_merge_preserves_merchant_claims_and_dedupes(monkeypatch) -> None:
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop_ensure)
    # Existing row: one merchant lab claim + one already-seen web citation.
    existing_row = {
        "product_key": "pk1",
        "merchant_id": "m1",
        "claims": [
            {"claim_text": "Clinically tested", "source_type": "merchant_lab_report",
             "source_ref": "art_1", "substantiation_status": "substantiated"},
            {"claim_text": "Old press", "source_type": "editorial_press",
             "source_ref": "https://vogue.com/x", "substantiation_status": "unverified"},
        ],
        "review_state": "observed",
        "required_disclaimers": None,
        "updated_at": None,
    }
    db = _FakeDB(row=existing_row)
    candidates = [
        {"claim_text": "Old press", "source_ref": "https://vogue.com/x", "source_type": "editorial_press",
         "substantiation_status": "unverified"},  # dup by ref -> skipped
        {"claim_text": "New feature", "source_ref": "https://allure.com/y", "source_type": "editorial_press",
         "substantiation_status": "unverified"},  # new -> added
    ]
    added = await wec._merge_candidates_into_evidence("pk1", "m1", candidates, db=db)
    assert added == 1
    _, values = db.executed[0]
    written = json.loads(values["claims"])
    # merchant claim preserved, no dup, one new
    assert len(written) == 3
    assert any(c["source_type"] == "merchant_lab_report" for c in written)
    assert sum(1 for c in written if c.get("source_ref") == "https://allure.com/y") == 1


async def test_merge_preserves_merchant_id_and_disclaimers_when_caller_unknown(monkeypatch) -> None:
    # A crawl/job that doesn't know the merchant passes merchant_id=None. The merge
    # must NOT null out the stored merchant_id (indexed scoping column) or disclaimers
    # (upsert is a full-row replace). Prefer the existing non-null merchant_id.
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop_ensure)
    existing_row = {
        "product_key": "pk1",
        "merchant_id": "m_stored",
        "claims": [{"claim_text": "Existing", "source_ref": "u0"}],
        "review_state": "reviewed",
        "required_disclaimers": [{"code": "fda_dshea_supplement", "text": "…"}],
        "updated_at": None,
    }
    db = _FakeDB(row=existing_row)
    added = await wec._merge_candidates_into_evidence(
        "pk1", None,  # caller doesn't know the merchant
        [{"claim_text": "New", "source_ref": "https://allure.com/y", "source_type": "editorial_press"}],
        db=db,
    )
    assert added == 1
    _, values = db.executed[0]
    assert values["mid"] == "m_stored"              # NOT clobbered to None
    assert values["rs"] == "reviewed"               # review_state preserved
    assert json.loads(values["disc"]) == [{"code": "fda_dshea_supplement", "text": "…"}]  # disclaimers preserved


async def test_merge_no_new_does_not_write(monkeypatch) -> None:
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop_ensure)
    db = _FakeDB(row={"claims": [{"claim_text": "X", "source_ref": "u1"}], "review_state": "observed"})
    added = await wec._merge_candidates_into_evidence(
        "pk1", "m1", [{"claim_text": "X", "source_ref": "u1", "source_type": "editorial_press"}], db=db
    )
    assert added == 0
    assert db.executed == []  # nothing new -> no upsert


# --- collect_web_evidence_for_product (injected search) ----------------------

async def test_collect_stores_candidates(monkeypatch) -> None:
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop_ensure)

    async def _fake_search(query):
        assert "GlowCo" in query and "review" in query
        return ([{"title": "Best serums", "url": "https://www.byrdie.com/best", "snippet": ""}], "ok")

    db = _FakeDB(row=None)  # no existing evidence
    out = await wec.collect_web_evidence_for_product(
        "pk1", "m1", brand="GlowCo", title="Bright Serum", db=db, search=_fake_search
    )
    assert out["discovered"] == 1
    assert out["candidates_added"] == 1
    assert out["status"] == "stored"


async def test_collect_no_key_is_safe_noop() -> None:
    async def _no_key(query):
        return ([], "no_key")
    out = await wec.collect_web_evidence_for_product(
        "pk1", "m1", brand="GlowCo", title="X", search=_no_key
    )
    assert out["candidates_added"] == 0
    assert out["status"] == "no_key"


async def test_collect_requires_identity_and_survives_search_error() -> None:
    out = await wec.collect_web_evidence_for_product("pk1", "m1")  # no brand/title
    assert out["status"] == "skipped"

    async def _boom(query):
        raise RuntimeError("serpapi down")
    out2 = await wec.collect_web_evidence_for_product(
        "pk1", "m1", brand="GlowCo", title="X", search=_boom
    )
    assert out2["status"] == "search_error"
