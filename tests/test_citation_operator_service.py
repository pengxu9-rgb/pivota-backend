"""Unit tests for the provider-aware citation operator.

Pure logic + the provider-aware scan with a mocked probe. No DB, no live LLM.
(`asyncio_mode = auto` in pytest.ini runs the async tests automatically.)
"""
from __future__ import annotations

from datetime import datetime, timezone

import services.citation_operator_service as cos
import services.citation_dashboard_service as dash


# --------------------------------------------------------------------------
# classify_channel — community must beat the registry's reddit='video' mislabel
# --------------------------------------------------------------------------

def test_classify_channel_reddit_is_community_not_video() -> None:
    assert cos.classify_channel("reddit.com") == cos.CHANNEL_COMMUNITY
    assert cos.classify_channel("https://www.reddit.com/r/SkincareAddiction/x") == cos.CHANNEL_COMMUNITY
    assert cos.classify_channel("r/Biohackers") == cos.CHANNEL_COMMUNITY


def test_classify_channel_buckets() -> None:
    assert cos.classify_channel("pubmed.ncbi.nlm.nih.gov") == cos.CHANNEL_SCIENTIFIC
    assert cos.classify_channel("sciencedirect.com") == cos.CHANNEL_SCIENTIFIC
    assert cos.classify_channel("oliveyoung.com") == cos.CHANNEL_RETAILER
    assert cos.classify_channel("yesstyle.com") == cos.CHANNEL_RETAILER
    assert cos.classify_channel("youtube.com") == cos.CHANNEL_VIDEO
    assert cos.classify_channel("webmd.com") == cos.CHANNEL_REVIEW
    assert cos.classify_channel("vogue.com") == cos.CHANNEL_EDITORIAL
    assert cos.classify_channel("whowhatwear.com") == cos.CHANNEL_EDITORIAL
    assert cos.classify_channel("") == cos.CHANNEL_OTHER
    assert cos.classify_channel("some-random-blog-xyz.net") == cos.CHANNEL_OTHER


# --------------------------------------------------------------------------
# Lane-aware revisit cadence (from the re-index latency research)
# --------------------------------------------------------------------------

def test_recheck_cadence_lanes() -> None:
    assert cos.recheck_cadence_days("chatgpt", cos.CHANNEL_COMMUNITY) == (3, 7, 14)
    assert cos.recheck_cadence_days("gemini", cos.CHANNEL_COMMUNITY) == (14, 28, 45, 60)
    assert cos.recheck_cadence_days("gemini", cos.CHANNEL_RETAILER) == (14, 28, 45, 60)
    assert cos.recheck_cadence_days("chatgpt", cos.CHANNEL_SCIENTIFIC) == (7, 21, 42, 60)
    assert cos.recheck_cadence_days(None, None) == (7, 21, 42, 60)


def test_next_check_advances_then_gives_up() -> None:
    posted = datetime(2026, 6, 9, tzinfo=timezone.utc)
    # default lane (7, 21, 42, 60): after check #1, next is posted+21d; past last window, give up.
    n1 = cos._next_check_at(provider="chatgpt", channel="scientific", posted_at=posted, check_count=1)
    assert n1 == datetime(2026, 6, 30, tzinfo=timezone.utc)  # +21
    assert cos._next_check_at(provider="chatgpt", channel="scientific", posted_at=posted, check_count=4) is None
    # community/chatgpt lane (3, 7, 14): tighter loop; give up after 3 windows.
    nc = cos._next_check_at(provider="chatgpt", channel="community", posted_at=posted, check_count=1)
    assert nc == datetime(2026, 6, 16, tzinfo=timezone.utc)  # +7
    assert cos._next_check_at(provider="chatgpt", channel="community", posted_at=posted, check_count=3) is None


# --------------------------------------------------------------------------
# community_threads + build_targets + target_key idempotency
# --------------------------------------------------------------------------

def _chatgpt_runs():
    return [
        {"grounding_sources": [
            {"uri": "https://www.reddit.com/r/SkincareAddiction/comments/abc", "title": "reddit.com"},
            {"uri": "https://pubmed.ncbi.nlm.nih.gov/123", "title": "pubmed.ncbi.nlm.nih.gov"},
        ]},
        {"grounding_sources": [
            {"uri": "https://www.reddit.com/r/30PlusSkinCare/comments/def", "title": "reddit.com"},
            {"uri": "https://www.webmd.com/reviews", "title": "webmd.com"},
        ]},
    ]


def test_community_threads_extracts_subreddits() -> None:
    threads = cos.community_threads(_chatgpt_runs())
    assert any("SkincareAddiction" == v for v in threads.values())
    assert any("30PlusSkinCare" == v for v in threads.values())
    assert all("reddit.com" in k for k in threads)


def test_build_targets_classifies_and_enriches_community() -> None:
    from collections import Counter
    competitors = Counter({"reddit.com": 2, "pubmed.ncbi.nlm.nih.gov": 1, "webmd.com": 1})
    targets = cos.build_targets(
        provider="chatgpt", competitors=competitors, raw_runs=_chatgpt_runs(),
        merchant_id="m1", sku_key="sku1", question_text="does collagen work",
    )
    by_host = {t["source_host"]: t for t in targets}
    assert by_host["reddit.com"]["channel"] == cos.CHANNEL_COMMUNITY
    assert by_host["reddit.com"]["subreddit"] in ("SkincareAddiction", "30PlusSkinCare")
    assert by_host["reddit.com"]["engagement_jsonb"]["subreddits"]
    assert by_host["pubmed.ncbi.nlm.nih.gov"]["channel"] == cos.CHANNEL_SCIENTIFIC
    assert by_host["webmd.com"]["channel"] == cos.CHANNEL_REVIEW


def test_target_key_is_stable_and_distinguishing() -> None:
    k1 = cos._target_key("m1", "chatgpt", "reddit.com", "sku1", "q")
    k2 = cos._target_key("m1", "chatgpt", "reddit.com", "sku1", "q")
    k3 = cos._target_key("m1", "gemini", "reddit.com", "sku1", "q")
    assert k1 == k2          # idempotent
    assert k1 != k3          # provider distinguishes


def test_channel_mix_and_evaluate_outcome() -> None:
    targets = [{"channel": "community"}, {"channel": "community"}, {"channel": "scientific"}]
    assert cos.channel_mix(targets) == {"community": 2, "scientific": 1}
    assert cos.evaluate_outcome(0) is False
    assert cos.evaluate_outcome(2) is True


# --------------------------------------------------------------------------
# run_citation_scan — provider-aware, mocked probe, no persistence
# --------------------------------------------------------------------------

def _patch_scan_billing(monkeypatch, *, paid=True, meter_result=None):
    """Mock the paid-tier gate + metering so scan tests don't touch the balance DB."""
    async def fake_paid(merchant_id, **kw):
        return paid
    async def fake_meter(merchant_id, op, *, provider, units, idempotency_key, **kw):
        return meter_result or {"billing_mode": "metered", "credits": 1}
    monkeypatch.setattr(cos.ccs, "merchant_is_paid_tier", fake_paid)
    monkeypatch.setattr(cos.ccs, "meter_agent_workflow", fake_meter)


async def test_run_citation_scan_is_provider_aware(monkeypatch) -> None:
    def _fake_result(provider):
        if provider == "chatgpt":
            return {"raw_runs": _chatgpt_runs()}
        return {"raw_runs": [
            {"grounding_sources": [
                {"uri": "https://global.oliveyoung.com/x", "title": "oliveyoung.com"},
                {"uri": "https://youtube.com/watch?v=1", "title": "youtube.com"},
            ]},
        ]}

    async def fake_probe(*, provider, **kwargs):
        return _fake_result(provider)

    monkeypatch.setattr(cos, "_probe", fake_probe)
    _patch_scan_billing(monkeypatch, paid=True)

    result = await cos.run_citation_scan(
        merchant_id="m1", merchant_brand="BB Lab", merchant_host="bblab.shop",
        queries=["does collagen work", "is collagen worth it"],
        providers=("gemini", "chatgpt"), persist=False,
    )
    assert result["billing_mode"] == "metered"
    summary = result["providers"]

    # ChatGPT surfaces community + scientific + review; Gemini does NOT cite community.
    cg = summary["chatgpt"]["channel_mix"]
    gm = summary["gemini"]["channel_mix"]
    assert cg.get("community", 0) >= 1
    assert cg.get("scientific", 0) >= 1
    assert "community" not in gm
    assert gm.get("retailer", 0) >= 1 and gm.get("video", 0) >= 1
    # Each provider carries its own billing line.
    assert summary["chatgpt"]["billing"]["billing_mode"] == "metered"

    # The community target carries a real subreddit for the operator to act on.
    community = [t for t in summary["chatgpt"]["targets"] if t["channel"] == "community"]
    assert community and community[0]["subreddit"] in ("SkincareAddiction", "30PlusSkinCare")


async def test_run_citation_scan_free_tier_gated_no_probe(monkeypatch) -> None:
    probed = {"called": False}
    async def fake_probe(*, provider, **kwargs):
        probed["called"] = True
        return {"raw_runs": []}
    monkeypatch.setattr(cos, "_probe", fake_probe)
    _patch_scan_billing(monkeypatch, paid=False)

    result = await cos.run_citation_scan(
        merchant_id="m1", queries=["q"], providers=("chatgpt",), persist=False)
    assert result["billing_mode"] == "preview_only"
    assert result["reason"] == "not_paid_tier"
    assert result["providers"] == {}
    assert probed["called"] is False   # free tier spends NO COGS


def test_build_targets_handles_gemini_titles_not_just_hosts() -> None:
    """Gemini returns human TITLES ('Olive Young Global'), not hosts. We must not
    fabricate a host from a title, and must not stamp Reddit subreddits onto a
    non-Reddit community target. (Regression for the normalize_host + enrichment bugs.)"""
    from collections import Counter
    competitors = Counter({"Olive Young Global": 2, "Sephora": 1, "Quora": 1, "reddit.com": 2})
    targets = {t["source_label"]: t for t in cos.build_targets(
        provider="gemini", competitors=competitors, raw_runs=_chatgpt_runs(),
        merchant_id="m1", question_text="q",
    )}
    # Title labels -> retailer channel, NO fabricated host.
    assert targets["Olive Young Global"]["channel"] == cos.CHANNEL_RETAILER
    assert targets["Olive Young Global"]["source_host"] is None
    assert targets["Sephora"]["channel"] == cos.CHANNEL_RETAILER
    assert targets["Sephora"]["source_host"] is None
    # Quora is community but must NOT inherit Reddit's subreddits.
    assert targets["Quora"]["channel"] == cos.CHANNEL_COMMUNITY
    assert targets["Quora"]["subreddit"] is None
    assert targets["Quora"]["engagement_jsonb"] is None
    # Only the genuine reddit.com host target gets a real host + subreddit.
    assert targets["reddit.com"]["source_host"] == "reddit.com"
    assert targets["reddit.com"]["subreddit"] in ("SkincareAddiction", "30PlusSkinCare")


# --------------------------------------------------------------------------
# DB-backed spine (record_merchant_action / recheck_action) via a fake database.
# Catches the `Record.get()` / row-as-dict class of bug the pure tests missed.
# --------------------------------------------------------------------------

class _FakeDB:
    """Minimal stand-in for db.database.database driving the operator loop."""
    def __init__(self):
        self.actions = {}     # action_id -> row dict
        self.targets = {}     # target_id -> row dict
        self.outcomes = []
        self.probe_cache = {}  # cache_key -> result dict
        self.scan_runs = []    # citation_scan_runs snapshot rows

    async def fetch_one(self, query, values=None):
        q, v = " ".join(query.split()), values or {}
        if "FROM citation_probe_cache" in q:
            hit = self.probe_cache.get(v.get("k"))
            return {"result_jsonb": hit} if hit is not None else None
        if "WHERE idempotency_key" in q:
            for a in self.actions.values():
                if a.get("idempotency_key") == v.get("k"):
                    return dict(a)
            return None
        if "SELECT provider, channel FROM citation_targets" in q:
            t = self.targets.get(v.get("tid"))
            return {"provider": t["provider"], "channel": t["channel"]} if t else None
        if "JOIN citation_targets" in q:
            a = self.actions.get(v.get("aid"))
            if not a:
                return None
            t = self.targets.get(a["target_id"])
            return {"provider": t["provider"], "channel": t["channel"]} if t else None
        if "FROM citation_actions WHERE action_id" in q:
            a = self.actions.get(v.get("aid"))
            return dict(a) if a else None
        if "FROM citation_targets WHERE target_id" in q:
            t = self.targets.get(v.get("tid"))
            return dict(t) if t else None
        return None

    async def fetch_all(self, query, values=None):
        return []

    async def execute(self, query, values=None):
        import json as _json
        q, v = " ".join(query.split()), values or {}
        if "INSERT INTO citation_probe_cache" in q:
            self.probe_cache[v.get("k")] = _json.loads(v.get("r"))
            return
        if "INSERT INTO citation_scan_runs" in q:
            self.scan_runs.append(dict(v))
            return
        if "INSERT INTO citation_actions" in q:
            row = dict(v)
            row.setdefault("check_count", 0)
            if not any(a.get("idempotency_key") == row.get("idempotency_key")
                       for a in self.actions.values()):  # ON CONFLICT DO NOTHING
                self.actions[row["action_id"]] = row
        elif "INSERT INTO citation_action_outcomes" in q:
            self.outcomes.append(dict(v))
        elif "UPDATE citation_actions" in q and "status='measuring'" in q:  # mark_action_posted
            a = self.actions.get(v.get("aid"))
            if a:
                a.update(status="measuring", posted_url=v.get("url"),
                         posted_at=v.get("now"), next_check_at=v.get("next"))
        elif "UPDATE citation_actions" in q:  # recheck advance
            a = self.actions.get(v.get("aid"))
            if a:
                a.update(status=v.get("status"), check_count=v.get("cc"),
                         next_check_at=v.get("next"))
        # UPDATE citation_targets ... -> ignore


async def _no_charge_meter(merchant_id, op, *, provider, units, idempotency_key, **kw):
    return {"billing_mode": "preview_only", "credits": 0}


async def test_action_and_recheck_loop_confirms_when_brand_cited(monkeypatch) -> None:
    db = _FakeDB()
    db.targets["t1"] = {"target_id": "t1", "merchant_id": "m1", "provider": "chatgpt",
                        "channel": "community", "source_host": "reddit.com"}
    monkeypatch.setattr(cos, "database", db)
    monkeypatch.setattr(cos.ccs, "meter_agent_workflow", _no_charge_meter)
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)

    # Merchant posts -> action enters 'measuring' with the COMMUNITY lane's first window (+3d).
    action_id = await cos.record_merchant_action(
        target_id="t1", merchant_id="m1", action_type="reddit_reply",
        posted_url="https://www.reddit.com/r/SkincareAddiction/comments/abc/xyz", now=now)
    a = db.actions[action_id]
    assert a["status"] == "measuring"
    assert a["next_check_at"] == datetime(2026, 6, 12, tzinfo=timezone.utc)  # +3 (community/chatgpt)

    # Re-probe shows the brand now cited -> confirmed, no further checks.
    async def cited_probe(*, provider, **kw):
        return {"raw_runs": [{"grounding_sources": [
            {"uri": "https://www.reddit.com/r/SkincareAddiction/comments/abc", "title": "reddit.com"},
            {"uri": "https://bblab.shop", "title": "BB Lab Official"},
        ]}]}
    monkeypatch.setattr(cos, "_probe", cited_probe)

    out = await cos.recheck_action(action_id=action_id, merchant_id="m1",
                                   queries=["is collagen worth it"],
                                   merchant_brand="BB Lab", merchant_host="bblab.shop")
    assert out["brand_cited"] is True
    assert out["source_cited"] is True            # reddit.com still cited at recheck
    assert out["status"] == "confirmed"
    assert db.actions[action_id]["status"] == "confirmed"
    assert len(db.outcomes) == 1


async def test_recheck_loop_advances_then_no_effect(monkeypatch) -> None:
    db = _FakeDB()
    db.targets["t1"] = {"target_id": "t1", "merchant_id": "m1", "provider": "chatgpt",
                        "channel": "community", "source_host": "reddit.com"}
    monkeypatch.setattr(cos, "database", db)
    monkeypatch.setattr(cos.ccs, "meter_agent_workflow", _no_charge_meter)
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    action_id = await cos.record_merchant_action(
        target_id="t1", merchant_id="m1", action_type="reddit_reply",
        posted_url="https://reddit.com/r/x/y", now=now)

    async def uncited_probe(*, provider, **kw):
        return {"raw_runs": [{"grounding_sources": [
            {"uri": "https://www.reddit.com/r/Other/z", "title": "reddit.com"}]}]}
    monkeypatch.setattr(cos, "_probe", uncited_probe)

    # community/chatgpt lane has 3 windows -> 3 rechecks then no_effect.
    statuses = []
    for _ in range(3):
        out = await cos.recheck_action(action_id=action_id, merchant_id="m1", queries=["q"],
                                       merchant_brand="BB Lab", merchant_host="bblab.shop")
        statuses.append(out["status"])
    assert statuses == ["measuring", "measuring", "no_effect"]
    assert db.actions[action_id]["check_count"] == 3
    assert len(db.outcomes) == 3


async def test_run_citation_scan_one_provider_failure_isolated(monkeypatch) -> None:
    async def fake_probe(*, provider, **kwargs):
        if provider == "gemini":
            raise RuntimeError("upstream 500")
        return {"raw_runs": _chatgpt_runs()}

    monkeypatch.setattr(cos, "_probe", fake_probe)
    _patch_scan_billing(monkeypatch, paid=True)
    result = await cos.run_citation_scan(
        merchant_id="m1", merchant_brand="BB Lab", merchant_host="bblab.shop",
        queries=["does collagen work"], providers=("gemini", "chatgpt"), persist=False,
    )
    summary = result["providers"]
    assert "error" in summary["gemini"]          # failure captured, not raised
    assert summary["chatgpt"]["channel_mix"].get("community", 0) >= 1  # other provider unaffected


async def test_run_due_rechecks_uses_persisted_brand_and_queries(monkeypatch) -> None:
    async def fake_due(*, now=None, limit=50):
        return [{"action_id": "a1", "merchant_id": "m1", "target_id": "t1"}]
    async def fake_get_target(*, target_id, merchant_id):
        return {"target_id": "t1", "merchant_id": "m1",
                "question_text": "is it worth it; does it work",
                "merchant_brand": "BB Lab", "merchant_host": "bblab.shop"}
    rc = {}
    async def fake_recheck(**kw):
        rc.update(kw)
        return {"action_id": "a1", "status": "confirmed"}
    monkeypatch.setattr(cos, "due_for_recheck", fake_due)
    monkeypatch.setattr(cos, "get_target", fake_get_target)
    monkeypatch.setattr(cos, "recheck_action", fake_recheck)

    out = await cos.run_due_rechecks(limit=10)
    assert out["processed"] == 1
    # Recovered queries + persisted brand/host drive the citation detection.
    assert rc["queries"] == ["is it worth it", "does it work"]
    assert rc["merchant_brand"] == "BB Lab" and rc["merchant_host"] == "bblab.shop"
    assert rc["merchant_id"] == "m1"


async def test_recheck_action_meters_the_probe(monkeypatch) -> None:
    db = _FakeDB()
    db.targets["t1"] = {"target_id": "t1", "merchant_id": "m1", "provider": "chatgpt",
                        "channel": "community", "source_host": "reddit.com"}
    monkeypatch.setattr(cos, "database", db)
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    action_id = await cos.record_merchant_action(
        target_id="t1", merchant_id="m1", action_type="reddit_reply",
        posted_url="https://reddit.com/r/x/y", now=now)

    async def probe(*, provider, **kw):
        return {"raw_runs": [{"grounding_sources": [
            {"uri": "https://reddit.com/r/x", "title": "reddit.com"}]}]}
    monkeypatch.setattr(cos, "_probe", probe)
    metered = {}
    async def fake_meter(merchant_id, op, *, provider, units, idempotency_key, **kw):
        metered.update(op=op, units=units, key=idempotency_key)
        return {"billing_mode": "metered", "credits": 1}
    monkeypatch.setattr(cos.ccs, "meter_agent_workflow", fake_meter)

    await cos.recheck_action(action_id=action_id, merchant_id="m1", queries=["q1", "q2"],
                             merchant_brand="BB Lab", merchant_host="bblab.shop")
    # The revisit loop is metered; keyed on (action, check_count) for once-per-window billing.
    assert metered["op"] == cos.SCAN_OPERATION_TYPE
    assert metered["units"] == 2
    assert metered["key"] == f"citation_recheck:{action_id}:0"


async def test_probe_caches_scan_results_avoiding_repeat_cogs(monkeypatch) -> None:
    db = _FakeDB()
    monkeypatch.setattr(cos, "database", db)
    calls = {"n": 0}
    async def fake_upstream(**kw):
        calls["n"] += 1
        return {"raw_runs": [{"grounding_sources": [
            {"uri": "https://reddit.com/r/x", "title": "reddit.com"}]}]}
    monkeypatch.setattr(cos.agent_center_llm_client, "probe", fake_upstream)

    kw = dict(provider="chatgpt", queries=["is it worth it"], merchant_id="m1")
    r1 = await cos._probe(**kw)
    r2 = await cos._probe(**kw)        # identical scan -> served from cache
    assert calls["n"] == 1             # upstream (COGS) hit exactly ONCE
    assert r1["raw_runs"] == r2["raw_runs"]
    assert r1["_from_cache"] is False and r2["_from_cache"] is True  # miss then hit


async def test_recheck_probe_bypasses_cache(monkeypatch) -> None:
    db = _FakeDB()
    monkeypatch.setattr(cos, "database", db)
    calls = {"n": 0}
    async def fake_upstream(**kw):
        calls["n"] += 1
        return {"raw_runs": [{"grounding_sources": [
            {"uri": "https://reddit.com/r/x", "title": "reddit.com"}]}]}
    monkeypatch.setattr(cos.agent_center_llm_client, "probe", fake_upstream)

    kw = dict(provider="chatgpt", queries=["q"], merchant_id="m1")
    await cos._probe(**kw)                      # primes the cache
    await cos._probe(**kw, use_cache=False)     # recheck path must probe FRESH
    assert calls["n"] == 2                       # both hit upstream (no stale recheck)


async def test_scan_does_not_charge_on_cache_hit(monkeypatch) -> None:
    async def cached_probe(*, provider, **kw):
        return {"raw_runs": _chatgpt_runs(), "_from_cache": True}  # served from cache
    monkeypatch.setattr(cos, "_probe", cached_probe)
    meter_calls = {"n": 0}
    async def fake_paid(merchant_id, **kw):
        return True
    async def fake_meter(*a, **k):
        meter_calls["n"] += 1
        return {"billing_mode": "metered", "credits": 1}
    monkeypatch.setattr(cos.ccs, "merchant_is_paid_tier", fake_paid)
    monkeypatch.setattr(cos.ccs, "meter_agent_workflow", fake_meter)

    result = await cos.run_citation_scan(
        merchant_id="m1", merchant_brand="BB Lab", merchant_host="bblab.shop",
        queries=["q"], providers=("chatgpt",), persist=False)
    # Cache hit = no COGS = NOT charged (closes the midnight-boundary double-bill).
    assert result["providers"]["chatgpt"]["billing"]["billing_mode"] == "cached"
    assert meter_calls["n"] == 0


async def test_probe_cache_write_failure_still_returns_result(monkeypatch) -> None:
    class _RaisingCacheDB:
        async def fetch_one(self, q, v=None):
            return None                         # always a miss
        async def execute(self, q, v=None):
            raise RuntimeError("cache write boom")
        async def fetch_all(self, q, v=None):
            return []
    monkeypatch.setattr(cos, "database", _RaisingCacheDB())
    calls = {"n": 0}
    async def fake_upstream(**kw):
        calls["n"] += 1
        return {"raw_runs": [{"grounding_sources": [
            {"uri": "https://reddit.com/r/x", "title": "reddit.com"}]}]}
    monkeypatch.setattr(cos.agent_center_llm_client, "probe", fake_upstream)

    out = await cos._probe(provider="chatgpt", queries=["q"], merchant_id="m1")
    assert out["raw_runs"]                       # live result survives a cache-write failure
    assert calls["n"] == 1


async def test_run_due_rechecks_respects_query_budget(monkeypatch) -> None:
    actions = [{"action_id": f"a{i}", "merchant_id": "m1", "target_id": "t1"} for i in range(3)]
    async def fake_due(*, now=None, limit=50):
        return actions
    async def fake_get_target(*, target_id, merchant_id):
        return {"question_text": "q1; q2; q3", "merchant_brand": "B", "merchant_host": "h"}
    done = []
    async def fake_recheck(**kw):
        done.append(kw["action_id"]); return {"status": "measuring"}
    monkeypatch.setattr(cos, "due_for_recheck", fake_due)
    monkeypatch.setattr(cos, "get_target", fake_get_target)
    monkeypatch.setattr(cos, "recheck_action", fake_recheck)

    # 3 queries/action, budget 5 -> only the first action fits (next would hit 6 > 5).
    out = await cos.run_due_rechecks(limit=10, query_budget=5)
    assert out["budget_exhausted"] is True
    assert out["processed"] == 1 and done == ["a0"]
    assert out["queries_spent"] == 3


# --------------------------------------------------------------------------
# Dashboard: snapshot persistence + read aggregation
# --------------------------------------------------------------------------

async def test_scan_snapshots_fresh_probe_not_cached(monkeypatch) -> None:
    db = _FakeDB()
    monkeypatch.setattr(cos, "database", db)
    _patch_scan_billing(monkeypatch, paid=True)

    async def fresh_probe(*, provider, **kw):
        return {"raw_runs": _chatgpt_runs(), "_from_cache": False}
    monkeypatch.setattr(cos, "_probe", fresh_probe)
    await cos.run_citation_scan(
        merchant_id="m1", merchant_brand="BB Lab", merchant_host="bblab.shop",
        queries=["q"], providers=("chatgpt",), persist=True)
    assert len(db.scan_runs) == 1               # fresh scan -> one snapshot
    assert db.scan_runs[0]["p"] == "chatgpt"

    db.scan_runs.clear()
    async def cached_probe(*, provider, **kw):
        return {"raw_runs": _chatgpt_runs(), "_from_cache": True}
    monkeypatch.setattr(cos, "_probe", cached_probe)
    await cos.run_citation_scan(
        merchant_id="m1", merchant_brand="BB Lab", merchant_host="bblab.shop",
        queries=["q"], providers=("chatgpt",), persist=True)
    assert db.scan_runs == []                   # cached scan -> no duplicate snapshot


async def test_scan_no_snapshot_on_empty_fresh_probe(monkeypatch) -> None:
    """A transient-empty fresh probe must NOT write a bogus 0% snapshot that would
    become the latest headline / pollute the trend."""
    db = _FakeDB()
    monkeypatch.setattr(cos, "database", db)
    _patch_scan_billing(monkeypatch, paid=True)
    async def empty_probe(*, provider, **kw):
        return {"raw_runs": [], "_from_cache": False}
    monkeypatch.setattr(cos, "_probe", empty_probe)
    await cos.run_citation_scan(
        merchant_id="m1", queries=["q"], providers=("chatgpt",), persist=True)
    assert db.scan_runs == []


async def test_dashboard_reads_are_merchant_scoped(monkeypatch) -> None:
    rows = [
        {"provider": "chatgpt", "merchant_id": "m1", "runs": 3, "merchant_cited_runs": 1,
         "visibility_score": 33, "channel_mix_jsonb": {}, "gap_count": 2, "created_at": "t"},
        {"provider": "chatgpt", "merchant_id": "m2", "runs": 3, "merchant_cited_runs": 3,
         "visibility_score": 100, "channel_mix_jsonb": {}, "gap_count": 0, "created_at": "t"},
    ]
    monkeypatch.setattr(dash, "database", _DashDB(overview=rows))
    out = await dash.overview(merchant_id="m1")
    # Only the caller's row comes back — never the other merchant's.
    assert list(out["providers"]) == ["chatgpt"]
    assert out["providers"]["chatgpt"]["visibility_score"] == 33


def test_visibility_score() -> None:
    assert cos.visibility_score(0, 0) == 0
    assert cos.visibility_score(3, 0) == 0
    assert cos.visibility_score(3, 3) == 100
    assert cos.visibility_score(3, 1) == 33


class _DashDB:
    def __init__(self, *, overview=None, trend=None, funnel=None, confirmed=None):
        self._overview = overview or []
        self._trend = trend or []
        self._funnel = funnel or []
        self._confirmed = confirmed or []

    async def fetch_all(self, query, values=None):
        q = " ".join(query.split())
        mid = (values or {}).get("mid")
        def scoped(rows):  # emulate WHERE merchant_id = :mid (guards the service passes it)
            return [r for r in rows if r.get("merchant_id") in (None, mid)]
        if "DISTINCT ON (provider)" in q:
            return scoped(self._overview)
        if "FROM citation_scan_runs" in q and "provider = :p" in q:
            return scoped(self._trend)
        if "GROUP BY status" in q:
            return scoped(self._funnel)
        if "status = 'confirmed'" in q:
            return scoped(self._confirmed)
        return []


async def test_dashboard_overview_splits_by_provider(monkeypatch) -> None:
    rows = [
        {"provider": "chatgpt", "runs": 3, "merchant_cited_runs": 1, "visibility_score": 33,
         "channel_mix_jsonb": {"community": 2}, "gap_count": 2, "created_at": "t"},
        {"provider": "gemini", "runs": 3, "merchant_cited_runs": 3, "visibility_score": 100,
         "channel_mix_jsonb": '{"retailer": 1}', "gap_count": 1, "created_at": "t"},  # JSON str
    ]
    monkeypatch.setattr(dash, "database", _DashDB(overview=rows))
    out = await dash.overview(merchant_id="m1")
    assert out["has_data"] is True
    assert out["providers"]["chatgpt"]["visibility_score"] == 33
    assert out["providers"]["chatgpt"]["channel_mix"] == {"community": 2}
    assert out["providers"]["gemini"]["channel_mix"] == {"retailer": 1}  # str coerced to dict


async def test_dashboard_trend_is_chronological(monkeypatch) -> None:
    rows = [  # DB returns newest-first
        {"visibility_score": 50, "gap_count": 2, "created_at": "t3"},
        {"visibility_score": 30, "gap_count": 3, "created_at": "t2"},
        {"visibility_score": 10, "gap_count": 4, "created_at": "t1"},
    ]
    monkeypatch.setattr(dash, "database", _DashDB(trend=rows))
    out = await dash.trend(merchant_id="m1", provider="chatgpt")
    assert [p["visibility_score"] for p in out["points"]] == [10, 30, 50]  # oldest->newest


async def test_dashboard_progress_funnel_and_confirmed(monkeypatch) -> None:
    funnel = [{"status": "draft", "n": 2}, {"status": "measuring", "n": 1},
              {"status": "confirmed", "n": 3}]
    confirmed = [{"action_id": "a1", "action_type": "reddit_reply", "target_id": "t1",
                  "posted_url": "u", "updated_at": "t"}]
    monkeypatch.setattr(dash, "database", _DashDB(funnel=funnel, confirmed=confirmed))
    out = await dash.progress(merchant_id="m1")
    assert out["funnel"]["confirmed"] == 3
    assert out["confirmed_count"] == 3
    assert len(out["confirmed"]) == 1
