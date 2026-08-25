"""Destination liveness: the rules that decide whether a published link is really dead.

Every assertion here is a rule that, if it flips, either publishes a broken link or retires a
live product. Three of them guard mistakes this lane actually made:

  * a `/products.json` pagination that broke partway was returned as if it were the whole
    catalogue, turning every unread page into a fabricated dead handle (285 on one host);
  * a Cloudflare bot challenge arrives as HTTP 429, so it was fed to the pacing backoff and
    retried — which can never succeed, because the client is being refused, not throttled;
  * `products.json` was treated as proof: on cosrx.com 5 of 12 delisted handles serve a live
    product page, so the join is a candidate finder and nothing more.

No sleeping and no network: `asyncio.sleep` is patched out and the HTTP client is a canned
sequence of `httpx.Response`s.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest

from services import crawl_politeness as cp
from services import external_seed_destination_liveness as liveness


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch):
    """The gate itself is tested in tests/test_crawl_politeness.py; here it must not slow anything."""
    cp.reset_for_tests()

    async def _allow(url, *, user_agent, max_wait=None):
        return None

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(cp, "before_request", _allow)
    monkeypatch.setattr(cp, "note_response", lambda *a, **k: None)
    monkeypatch.setattr(liveness.asyncio, "sleep", _no_sleep)
    yield
    cp.reset_for_tests()


class _CannedClient:
    """Returns queued responses in order; records every URL it was asked for."""

    def __init__(self, responses: List[httpx.Response]) -> None:
        self._responses = list(responses)
        self.urls: List[str] = []

    async def get(self, url, headers=None):  # noqa: ANN001
        self.urls.append(url)
        if not self._responses:
            raise AssertionError(f"no canned response left for {url}")
        resp = self._responses.pop(0)
        resp.request = httpx.Request("GET", url)
        return resp


def _page(handles: List[str], *, status: int = 200, headers: Optional[dict] = None):
    return httpx.Response(
        status, json={"products": [{"handle": h} for h in handles]}, headers=headers or {}
    )


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- stage 1: catalogue read

def test_complete_catalogue_is_ok_and_carries_every_handle():
    client = _CannedClient([_page(["a", "b", "c"])])
    read = _run(liveness.read_brand_catalogue(client, "brand.com"))
    assert read.status == liveness.CATALOGUE_OK
    assert read.usable
    assert read.handles == {"a", "b", "c"}
    assert read.product_count == 3


def test_truncated_pagination_yields_no_handles_so_nothing_can_be_called_dead():
    """The fabricated-dead-handle bug: page 1 succeeded, page 2 did not.

    Returning the partial handle set marks every seed on the unread pages as delisted. The read
    is discarded instead, and `usable` is False so the host leaves the denominator.
    """
    full_page = _page([f"h{i}" for i in range(liveness.PAGE_LIMIT)])
    client = _CannedClient([full_page] + [httpx.Response(429) for _ in range(3)])

    read = _run(liveness.read_brand_catalogue(client, "brand.com", attempts=3))

    assert read.status == liveness.CATALOGUE_INCOMPLETE
    assert not read.usable
    assert read.handles == set(), "a partial catalogue must not be usable as a catalogue"
    assert "broke at page 2" in read.note


def test_bot_challenge_is_not_retried_and_is_its_own_outcome():
    """`cf-mitigated: challenge` is a refusal, not a pacing signal."""
    client = _CannedClient([httpx.Response(429, headers={"cf-mitigated": "challenge"})])

    read = _run(liveness.read_brand_catalogue(client, "brand.com", attempts=5))

    assert read.status == liveness.CATALOGUE_BOT_CHALLENGE
    assert not read.usable
    assert "cf-mitigated=challenge" in read.note
    assert len(client.urls) == 1, "a challenge must cost exactly one request, not `attempts`"


def test_a_real_429_is_still_retried():
    """Without the challenge header, 429 keeps its ordinary back-off-and-retry treatment."""
    client = _CannedClient([httpx.Response(429), _page(["a"])])
    read = _run(liveness.read_brand_catalogue(client, "brand.com", attempts=3))
    assert read.status == liveness.CATALOGUE_OK
    assert read.handles == {"a"}
    assert len(client.urls) == 2


def test_a_404_on_page_one_is_reported_as_the_status_not_as_an_empty_catalogue():
    client = _CannedClient([httpx.Response(404, text="nope")])
    read = _run(liveness.read_brand_catalogue(client, "brand.com"))
    assert read.status == "http_404"
    assert not read.usable
    assert read.handles == set()


# --------------------------------------------------------------- classification

@pytest.mark.parametrize(
    "status, final_url, expected, expected_note",
    [
        (404, None, liveness.VERDICT_DEAD_404, "http_404"),
        (410, None, liveness.VERDICT_DEAD_404, "http_410"),
        (200, "https://brand.com/products/toner", liveness.VERDICT_LIVE, ""),
        (200, "https://brand.com/products/toner-v2", liveness.VERDICT_REDIRECTED_TO_PRODUCT, "toner-v2"),
        (200, "https://brand.com/collections/all", liveness.VERDICT_REDIRECTED_OFF_PRODUCT, "left"),
        (200, "https://brand.com/", liveness.VERDICT_REDIRECTED_OFF_PRODUCT, "left"),
        # 403/429/5xx are the origin refusing or failing, NOT the product being gone. The NOTE is
        # asserted, not just the verdict: every one of these already reads `unverifiable`, so a
        # verdict-only assertion cannot fail if the branch is deleted.
        (403, None, liveness.VERDICT_UNVERIFIABLE, "http_403"),
        (429, None, liveness.VERDICT_UNVERIFIABLE, "http_429"),
        (503, None, liveness.VERDICT_UNVERIFIABLE, "http_503"),
    ],
)
def test_classification(status, final_url, expected, expected_note):
    obs = liveness.classify_destination(
        requested_url="https://brand.com/products/toner",
        status_code=status,
        final_url=final_url,
    )
    assert obs.verdict == expected
    assert expected_note in obs.note


def test_a_bot_challenge_on_a_pdp_is_unverifiable_and_says_so():
    obs = liveness.classify_destination(
        requested_url="https://brand.com/products/toner", status_code=429, bot_challenged=True
    )
    assert obs.verdict == liveness.VERDICT_UNVERIFIABLE
    assert obs.note == "bot_challenge"
    assert not obs.confirmed_dead
    assert not obs.reached_origin


def test_a_transport_failure_never_reaches_the_origin():
    obs = liveness.classify_destination(
        requested_url="https://brand.com/products/toner",
        status_code=None,
        transport_error="ConnectTimeout",
    )
    assert obs.verdict == liveness.VERDICT_UNVERIFIABLE
    assert not obs.reached_origin


def test_a_delisted_but_live_page_is_not_broken():
    """Measured on cosrx.com: 5 of 12 delisted handles serve a 200 product page.

    Folding those into "dead" is the difference between a 12.4% delisted rate and a 10.4%
    broken rate — and only the second is a link a shopper cannot follow.
    """
    obs = liveness.classify_destination(
        requested_url="https://brand.com/products/toner",
        status_code=200,
        final_url="https://brand.com/products/toner",
        listed_in_catalogue=False,
    )
    assert obs.verdict == liveness.VERDICT_LIVE_DELISTED
    assert not obs.confirmed_dead
    assert obs.reached_origin


def test_absence_from_the_catalogue_can_never_promote_a_verdict_to_dead():
    """`listed_in_catalogue=False` may downgrade a live page. It may not kill one."""
    for status, final in ((200, "https://brand.com/products/toner"),):
        obs = liveness.classify_destination(
            requested_url="https://brand.com/products/toner",
            status_code=status,
            final_url=final,
            listed_in_catalogue=False,
        )
        assert obs.verdict not in liveness.CONFIRMED_DEAD_VERDICTS


# --------------------------------------------------------------- retirement policy

@pytest.mark.parametrize(
    "verdict, streak, expected",
    [
        (liveness.VERDICT_DEAD_404, 0, False),
        (liveness.VERDICT_DEAD_404, 1, False),
        (liveness.VERDICT_DEAD_404, 2, True),
        (liveness.VERDICT_REDIRECTED_OFF_PRODUCT, 2, True),
        # The three that must NEVER retire a seed, no matter how many times they repeat.
        (liveness.VERDICT_UNVERIFIABLE, 99, False),
        (liveness.VERDICT_LIVE_DELISTED, 99, False),
        (liveness.VERDICT_REDIRECTED_TO_PRODUCT, 99, False),
        (liveness.VERDICT_LIVE, 99, False),
    ],
)
def test_should_retire(verdict, streak, expected):
    assert liveness.should_retire(verdict, streak) is expected


def test_one_bad_night_cannot_retire_anything():
    """`RETIREMENT_STREAK` is 2 and `record_destination_observation` enforces a 24h gap.

    Together they mean a single sweep — however wrong — retires nothing, which is what makes
    re-running a sweep after a failure free.
    """
    assert liveness.RETIREMENT_STREAK >= 2
    assert liveness.RETIREMENT_MIN_GAP >= timedelta(hours=24)
    assert liveness.should_retire(liveness.VERDICT_DEAD_404, 1) is False


# --------------------------------------------------------------- the observation write

class _FakeDb:
    """Records the UPDATE parameters `record_destination_observation` computes."""

    def __init__(self, row: Dict[str, Any]) -> None:
        self.row = row
        self.executed: List[Any] = []

    async def fetch_one(self, _query, _params=None):
        return self.row

    async def execute(self, query, params=None):
        self.executed.append((query, params))


def _observe(monkeypatch, row, observation, now=None):
    db = _FakeDb(row)
    monkeypatch.setattr(liveness, "database", db)
    result = _run(liveness.record_destination_observation("eps_1", observation, now=now))
    return result, db


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def test_a_confirmed_dead_observation_advances_the_streak(monkeypatch):
    row = {
        "destination_checked_at": NOW - timedelta(days=3),
        "destination_verdict": liveness.VERDICT_DEAD_404,
        "destination_failure_streak": 1,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, corroborated=True)
    result, db = _observe(monkeypatch, row, obs, now=NOW)
    assert result["failure_streak"] == 2
    assert result["retire"] is True
    assert db.executed[0][1]["reached_origin"] is True
    assert db.executed[0][1]["checked_at"] == NOW


def test_a_second_look_inside_the_gap_does_not_advance_the_streak(monkeypatch):
    """Two probes in one run must not add up to a retirement."""
    row = {
        "destination_checked_at": NOW - timedelta(hours=1),
        "destination_verdict": liveness.VERDICT_DEAD_404,
        "destination_failure_streak": 1,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, corroborated=True)
    result, _db = _observe(monkeypatch, row, obs, now=NOW)
    assert result["failure_streak"] == 1
    assert result["retire"] is False


def test_an_unverifiable_observation_freezes_everything(monkeypatch):
    """"We could not look" must not move the clock OR the streak.

    Moving `destination_checked_at` would make a host that stopped talking to us look freshly
    verified; moving the streak would let a bot challenge retire a live product.
    """
    row = {
        "destination_checked_at": NOW - timedelta(days=30),
        "destination_verdict": liveness.VERDICT_LIVE,
        "destination_failure_streak": 1,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_UNVERIFIABLE, 429, None, "bot_challenge")
    result, db = _observe(monkeypatch, row, obs, now=NOW)
    assert result["failure_streak"] == 1, "an unverifiable must not advance the streak"
    assert result["checked_at"] is None, "an unverifiable must not stamp destination_checked_at"
    assert result["retire"] is False

    # THE STATEMENT, not just the parameters. `reached_origin=False` is inert unless the SQL
    # actually branches on it — a mutant that writes `destination_checked_at = :checked_at`
    # unconditionally passes every value-level assertion above while stamping the column on
    # every bot challenge, which is the exact lie this rule exists to prevent.
    query, params = db.executed[0]
    sql = " ".join(str(query).split())
    assert "destination_checked_at = CASE WHEN CAST(:reached_origin AS BOOLEAN) THEN :checked_at" in sql, (
        "an unverifiable observation must leave destination_checked_at where it was"
    )
    assert "ELSE destination_checked_at END" in sql
    assert params["reached_origin"] is False


def test_a_live_observation_resets_the_streak(monkeypatch):
    row = {
        "destination_checked_at": NOW - timedelta(days=3),
        "destination_verdict": liveness.VERDICT_DEAD_404,
        "destination_failure_streak": 1,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_LIVE, 200, None)
    result, _db = _observe(monkeypatch, row, obs, now=NOW)
    assert result["failure_streak"] == 0
    assert result["retire"] is False


def test_a_live_delisted_observation_also_resets_the_streak(monkeypatch):
    """The link works. That the brand unlisted it is a review signal, not a failure."""
    row = {
        "destination_checked_at": NOW - timedelta(days=3),
        "destination_verdict": liveness.VERDICT_DEAD_404,
        "destination_failure_streak": 1,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_LIVE_DELISTED, 200, None)
    result, _db = _observe(monkeypatch, row, obs, now=NOW)
    assert result["failure_streak"] == 0


def test_a_never_checked_row_advances_on_its_first_dead_observation(monkeypatch):
    row = {
        "destination_checked_at": None,
        "destination_verdict": None,
        "destination_failure_streak": 0,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, corroborated=True)
    result, _db = _observe(monkeypatch, row, obs, now=NOW)
    assert result["failure_streak"] == 1
    assert result["retire"] is False, "one observation is never enough"


# --------------------------------------------------------------- grouping + coverage

def test_group_by_host_keeps_a_locale_storefront_separate():
    """`nl.beautyofjoseon.com` has its own catalogue; folding it to the apex invents dead links."""
    rows = [
        {"id": "a", "canonical_url": "https://beautyofjoseon.com/products/x"},
        {"id": "b", "canonical_url": "https://nl.beautyofjoseon.com/products/x"},
        {"id": "c", "canonical_url": "https://www.beautyofjoseon.com/products/y"},
    ]
    grouped = liveness.group_by_host(rows)
    assert set(grouped) == {"beautyofjoseon.com", "nl.beautyofjoseon.com"}
    assert len(grouped["beautyofjoseon.com"]) == 2, "www. folds onto the apex; a locale does not"


def test_group_by_host_skips_rows_with_no_product_handle_and_falls_back_to_destination_url():
    rows = [
        {"id": "a", "canonical_url": "https://brand.com/collections/all"},
        {"id": "b", "canonical_url": None, "destination_url": "https://brand.com/products/toner"},
    ]
    grouped = liveness.group_by_host(rows)
    assert list(grouped) == ["brand.com"]
    assert [r["id"] for r in grouped["brand.com"]] == ["b"]


def test_coverage_alarm_fires_when_most_hosts_are_unreadable():
    """A sweep that cannot see its hosts reports zero dead links and looks healthy."""
    assert liveness.coverage_alarm({"hosts": 286, "hosts_unverifiable": 213}) is not None
    assert liveness.coverage_alarm({"hosts": 286, "hosts_unverifiable": 10}) is None
    assert liveness.coverage_alarm({"hosts": 0, "hosts_unverifiable": 0}) is None


def test_the_verdict_vocabulary_matches_the_database_constraint():
    """A verdict the CHECK constraint rejects would fail the UPDATE at write time, in prod."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    sql = (repo / "db/migrations/200_external_seed_destination_liveness.sql").read_text()
    guard = (repo / "db/schema_guard.py").read_text()
    for verdict in liveness.ALL_VERDICTS:
        assert f"'{verdict}'" in sql, f"{verdict} missing from migration 200's CHECK"
        assert f"'{verdict}'" in guard, f"{verdict} missing from the schema_guard CHECK"


# --------------------------------------------------------------- the sweep, end to end

class _SweepClient:
    """One canned response per URL pattern, so the sweep's own routing is what is tested."""

    def __init__(self, catalogue: List[str], pdp: Dict[str, httpx.Response]) -> None:
        self.catalogue = catalogue
        self.pdp = pdp
        self.urls: List[str] = []

    async def get(self, url, headers=None):  # noqa: ANN001
        self.urls.append(url)
        if "/products.json" in url:
            page = 1 if "page=1" in url else 2
            resp = _page(self.catalogue if page == 1 else [])
        else:
            resp = self.pdp[url]
        resp.request = httpx.Request("GET", url)
        return resp

    async def aclose(self):
        return None


def _sweep(monkeypatch, seeds, client, **kwargs):
    recorded: List[Any] = []
    retired: List[str] = []

    async def fake_candidates(limit):
        return seeds

    async def fake_record(seed_id, observation, *, now=None):
        recorded.append((seed_id, observation))
        return {"seed_id": seed_id, "verdict": observation.verdict, "retire": False}

    async def fake_retire(seed_id, observation, *, now=None):
        retired.append(seed_id)
        return {"retired": True}

    monkeypatch.setattr(liveness, "get_sweep_candidates", fake_candidates)
    monkeypatch.setattr(liveness, "record_destination_observation", fake_record)
    monkeypatch.setattr(liveness, "retire_seed_for_dead_destination", fake_retire)
    summary = _run(liveness.run_destination_sweep(client=client, **kwargs))
    return summary, recorded, retired


def test_a_listed_handle_is_verified_by_the_catalogue_read_alone(monkeypatch):
    """Otherwise NOTHING is ever verified.

    The sweep only probes the delisted handles — that is what makes it affordable. If a
    listed handle recorded no observation, every healthy seed would sit at
    `destination_never_verified` forever and the external lane would never serve again.
    """
    seeds = [
        {"id": "eps_live", "canonical_url": "https://brand.com/products/toner"},
        {"id": "eps_gone", "canonical_url": "https://brand.com/products/old"},
    ]
    client = _SweepClient(
        catalogue=["toner"],
        pdp={"https://brand.com/products/old": httpx.Response(404)},
    )

    summary, recorded, _retired = _sweep(monkeypatch, seeds, client)

    by_seed = {seed_id: obs for seed_id, obs in recorded}
    assert by_seed["eps_live"].verdict == liveness.VERDICT_LIVE
    assert by_seed["eps_live"].reached_origin is True
    assert by_seed["eps_live"].http_status is None, (
        "we read the catalogue, not the PDP — writing 200 would invent a response"
    )
    assert by_seed["eps_gone"].verdict == liveness.VERDICT_DEAD_404
    assert summary["listed"] == 1 and summary["probed"] == 1
    assert summary["dead_links_found"] == 1

    pdp_fetches = [u for u in client.urls if "/products.json" not in u]
    assert pdp_fetches == ["https://brand.com/products/old"], (
        "a listed handle must not cost a PDP fetch — that is the whole economy of stage 1"
    )


def test_an_unreadable_host_produces_no_verdicts_at_all(monkeypatch):
    """A bot challenge must not write `unverifiable` across a whole host.

    It would bury the number that matters — how much of the corpus we can still see — under
    rows that were never in question, and it would stamp opinions on seeds we did not look at.
    """
    seeds = [{"id": "eps_1", "canonical_url": "https://brand.com/products/toner"}]

    class _Blocked:
        urls: List[str] = []

        async def get(self, url, headers=None):  # noqa: ANN001
            self.urls.append(url)
            resp = httpx.Response(429, headers={"cf-mitigated": "challenge"})
            resp.request = httpx.Request("GET", url)
            return resp

        async def aclose(self):
            return None

    summary, recorded, retired = _sweep(monkeypatch, seeds, _Blocked())

    assert recorded == [], "no observation may be recorded for a host we could not read"
    assert retired == []
    assert summary["hosts_unverifiable"] == 1
    assert summary["dead_links_found"] == 0
    assert liveness.coverage_alarm(summary) is not None


def test_the_sweep_does_not_retire_when_asked_not_to(monkeypatch):
    """The first production run is observe-only; `--no-retire` has to actually mean it."""
    seeds = [{"id": "eps_gone", "canonical_url": "https://brand.com/products/old"}]
    client = _SweepClient(
        catalogue=["toner"], pdp={"https://brand.com/products/old": httpx.Response(404)}
    )

    async def fake_record(seed_id, observation, *, now=None):
        return {"seed_id": seed_id, "verdict": observation.verdict, "retire": True}

    monkeypatch.setattr(liveness, "record_destination_observation", fake_record)
    summary, _recorded, retired = _sweep(monkeypatch, seeds, client, retire=False)

    assert retired == []
    assert summary["seeds_retired"] == 0
    assert summary["dead_links_found"] == 1, "it still MEASURES; it just does not act"


def test_one_exploding_host_does_not_void_the_rest_of_the_pass(monkeypatch):
    """The counters are the deliverable; a half-swept corpus beats none.

    Also: the failed host lands in `hosts_unverifiable`, so the coverage dial still reflects
    what we could not see rather than silently reporting a smaller, cleaner-looking corpus.
    """
    seeds = [
        {"id": "eps_a", "canonical_url": "https://good.com/products/toner"},
        {"id": "eps_b", "canonical_url": "https://bad.com/products/toner"},
    ]

    class _HalfBroken:
        async def get(self, url, headers=None):  # noqa: ANN001
            if "bad.com" in url:
                raise RuntimeError("host exploded")
            resp = _page(["toner"]) if "/products.json" in url else httpx.Response(200)
            resp.request = httpx.Request("GET", url)
            return resp

        async def aclose(self):
            return None

    # The catalogue reader swallows transport errors, so blow up further in — the point is that
    # gather() does not lose the other host.
    real_read = liveness.read_brand_catalogue

    async def exploding_read(client, host, **kw):
        if host == "bad.com":
            raise RuntimeError("host exploded")
        return await real_read(client, host, **kw)

    monkeypatch.setattr(liveness, "read_brand_catalogue", exploding_read)
    summary, recorded, _retired = _sweep(monkeypatch, seeds, _HalfBroken())

    assert [seed_id for seed_id, _ in recorded] == ["eps_a"]
    assert summary["hosts_unverifiable"] == 1
    assert summary["listed"] == 1


# ------------------------------------------------- corroboration is what licenses a retirement

def test_a_url_that_never_named_a_product_handle_is_unverifiable_not_dead():
    """A 200 on a non-Shopify-shaped URL must not be a CONFIRMED-DEAD verdict.

    `redirected_off_product` is reached by asking "did we land on the handle we asked for".
    When the seed's URL carries no handle at all that question has no answer, and its absence
    is not evidence — but the comparison still failed, so a perfectly healthy page on
    `/p/<sku>`, `/store/products/<x>` or `/shop/<x>.html` was classified dead. 682 of the
    11,352 active seeds carry such a URL. The sweep never sees them (`group_by_host` drops
    handle-less rows); the refresh route classified them unconditionally.
    """
    # NB `/store/products/<h>` is deliberately absent: `_HANDLE_RE` searches rather than
    # anchors, so that URL really does name a handle and really is judgeable. These do not.
    for url in (
        "https://www.ulta.com/p/hydrating-serum-pimprod2031234",
        "https://brand.com/shop/toner.html",
        "https://brand.com/collections/all",
        "https://brand.com/",
    ):
        obs = liveness.classify_destination(requested_url=url, status_code=200, final_url=url)
        assert obs.verdict == liveness.VERDICT_UNVERIFIABLE, url
        assert obs.confirmed_dead is False, url
        assert obs.corroborated is False, url


def test_only_a_catalogue_corroborated_verdict_is_marked_corroborated():
    """`listed_in_catalogue is False` is the stage-1 witness. Nothing else sets the flag."""
    dead = dict(requested_url="https://b.com/products/x", status_code=404)
    assert liveness.classify_destination(**dead, listed_in_catalogue=False).corroborated is True
    assert liveness.classify_destination(**dead).corroborated is False
    assert liveness.classify_destination(**dead, listed_in_catalogue=True).corroborated is False


def test_an_uncorroborated_dead_observation_holds_the_streak(monkeypatch):
    """A 404 with no second witness is recorded, but it may not push toward retirement.

    A WAF that answers 404 to an unfamiliar client and a deleted product are the same bytes,
    and a WAF policy is MORE persistent than a dead product — so repetition plus the 24h gap
    cannot tell them apart. `services/live_offer_verification._check_one` refuses the same
    inference for the same reason. Without this, two employee refreshes a day apart retired a
    live product.
    """
    row = {
        "destination_checked_at": NOW - timedelta(days=3),
        "destination_verdict": liveness.VERDICT_DEAD_404,
        "destination_failure_streak": 1,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, corroborated=False)
    result, _db = _observe(monkeypatch, row, obs, now=NOW)
    assert result["failure_streak"] == 1, "held, not advanced"
    assert result["retire"] is False
    assert result["verdict"] == liveness.VERDICT_DEAD_404, "still recorded — it is a real answer"


def test_an_uncorroborated_dead_observation_can_never_reach_the_retirement_threshold(monkeypatch):
    """Repeat it as often as you like, days apart: it never retires."""
    streak = 0
    for day in range(6):
        row = {
            "destination_checked_at": NOW - timedelta(days=90 - day * 10),
            "destination_verdict": liveness.VERDICT_DEAD_404,
            "destination_failure_streak": streak,
            "status": "active",
        }
        obs = liveness.DestinationObservation(
            liveness.VERDICT_DEAD_404, 404, None, corroborated=False
        )
        result, _db = _observe(monkeypatch, row, obs, now=NOW)
        streak = result["failure_streak"]
        assert result["retire"] is False, f"retired on day {day}"
    assert streak == 0


# ------------------------------------------------- a non-observation writes no fact

def test_an_unverifiable_observation_does_not_overwrite_a_confirmed_dead_verdict(monkeypatch):
    """The verdict is the field SERVING reads, so overwriting it un-blocks a dead seed.

    The clock and the streak were already frozen for an `unverifiable`, but the verdict was
    written unconditionally. `destination_dead` requires `verdict in CONFIRMED_DEAD_VERDICTS`,
    so one 429 on a seed sitting at dead_404/streak-2 cleared the blocker and handed its 404
    link back to the serving lane until another confirmed-dead observation happened to land.
    """
    row = {
        "destination_checked_at": NOW - timedelta(days=1),
        "destination_verdict": liveness.VERDICT_DEAD_404,
        "destination_failure_streak": 2,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_UNVERIFIABLE, 429, None, "bot_challenge")
    _result, db = _observe(monkeypatch, row, obs, now=NOW)

    sql, params = db.executed[0]
    assert params["reached_origin"] is False
    # The guard has to be in the STATEMENT: with a params-only assertion, a mutant that drops
    # the CASE and writes :verdict unconditionally still passes.
    assert "destination_verdict = CASE" in sql
    assert "destination_http_status = CASE" in sql
    assert "WHEN CAST(:reached_origin AS BOOLEAN) THEN :verdict" in sql


def test_a_conclusive_observation_does_overwrite_the_verdict(monkeypatch):
    """The freeze applies to `unverifiable` only — a real answer still lands."""
    row = {
        "destination_checked_at": NOW - timedelta(days=1),
        "destination_verdict": liveness.VERDICT_DEAD_404,
        "destination_failure_streak": 2,
        "status": "active",
    }
    obs = liveness.DestinationObservation(liveness.VERDICT_LIVE, 200, None)
    result, db = _observe(monkeypatch, row, obs, now=NOW)
    assert db.executed[0][1]["reached_origin"] is True
    assert db.executed[0][1]["verdict"] == liveness.VERDICT_LIVE
    assert result["failure_streak"] == 0, "a live answer resets the streak"


# ------------------------------------------------- an empty catalogue is not a catalogue

def test_a_page_one_with_no_products_is_not_a_usable_catalogue():
    """200 + `{"products": []}` is far more often a gated feed than a shop selling nothing.

    Treating it as usable marks every seed on the host delisted and converts the sweep's
    one-request-per-host economy into one PDP fetch per seed — aimed at exactly the hosts most
    likely to be refusing us.
    """
    client = _CannedClient([_page([])])
    read = _run(liveness.read_brand_catalogue(client, "brand.com", attempts=1))
    assert read.status == liveness.CATALOGUE_EMPTY
    assert read.usable is False
    assert read.handles == set()


def test_an_empty_LATER_page_still_ends_a_good_catalogue():
    """The empty-page-1 rule must not break normal pagination termination."""
    client = _CannedClient([_page([f"h{i}" for i in range(liveness.PAGE_LIMIT)]), _page([])])
    read = _run(liveness.read_brand_catalogue(client, "brand.com", attempts=1))
    assert read.status == liveness.CATALOGUE_OK
    assert read.usable is True
    assert len(read.handles) == liveness.PAGE_LIMIT


# ------------------------------------------------- the mirror withdrawal is scoped

def test_retirement_scopes_the_mirror_by_source_system_and_stamps_updated_at(monkeypatch):
    """`source_ref` alone is not the seed->product link; the PAIR is.

    services/external_offer_dual_write states it and `resolve_mirror_product` queries on both,
    so matching on source_ref alone would suppress any row from another door carrying the same
    value. `updated_at` matches identity_resolution.SUPPRESS_SQL so incremental consumers can
    see the withdrawal.
    """
    db = _FakeDb({"status": "active"})
    monkeypatch.setattr(liveness, "database", db)
    obs = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, corroborated=True)
    _run(liveness.retire_seed_for_dead_destination("eps_1", obs, now=NOW))

    mirror_sql, mirror_params = next(
        (sql, params) for sql, params in db.executed if "catalog_products" in sql
    )
    assert "source_system = :source_system" in mirror_sql
    assert mirror_params["source_system"] == liveness.MIRROR_SOURCE_SYSTEM
    assert "updated_at = NOW()" in mirror_sql
    assert mirror_params["reason"] == liveness.SUPPRESSION_REASON
