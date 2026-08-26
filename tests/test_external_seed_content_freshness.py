"""The refresh queue must order on CRAWL recency, not WRITE recency.

Migration 200 already made this argument for the destination URL, in its own header:
`updated_at` "measured 'when did we last write this row', never 'when did we last see
this URL'". The identical defect governed the CONTENT refresh queue until migration 202 --
`get_external_referral_refresh_candidate_seed_ids` ordered by `updated_at`, which is bumped
by `external_seed_servability` on attach, by `identity_resolution` on a status flip, by
`pdp_governance_service`, and by any operator PATCH. None of those go near the origin.

The sharpest case, and the one this file exists to pin: the selector's primary query is
`WHERE attached_product_key IS NOT NULL ORDER BY ...`, and ATTACHING a product key is
itself an `updated_at` bump. A seed becoming servable -- the moment its price starts being
quoted to a buyer -- was sent to the BACK of the queue that keeps its price honest.

Two halves, both pinned here:
  * the WRITER stamps `last_crawled_at` only when a fetch actually reached the origin;
  * the READER orders on it, keeping `updated_at` strictly as a tiebreak.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from services.external_offers_service import ExternalOfferUnavailable

DEST = "https://brand.com/products/toner"


# --------------------------------------------------------------------------- fixtures

def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_fresh_1",
        "external_product_id": "ext_fresh_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://brand.com/products/toner",
        "canonical_url": "https://brand.com/products/toner",
        "domain": "brand.com",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": 28.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {"title": "Toner", "snapshot": {}},
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
        "destination_checked_at": None,
        "destination_verdict": None,
        "destination_failure_streak": 0,
        "last_crawled_at": None,
    }
    row.update(overrides)
    return row


def _snapshot(**overrides: Any) -> SimpleNamespace:
    base = {
        "canonical_url": "https://brand.com/products/toner",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": 28.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "domain": "brand.com",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Recorder:
    def __init__(self) -> None:
        self.observations: List[Any] = []
        self.retired: List[str] = []

    async def record(self, seed_id, observation, *, now=None):
        self.observations.append(observation)
        return {"seed_id": seed_id, "verdict": observation.verdict, "failure_streak": 0}

    async def retire(self, seed_id, observation, *, now=None):
        self.retired.append(seed_id)
        return {"seed_id": seed_id, "retired": True}


def _run_refresh(
    monkeypatch: pytest.MonkeyPatch,
    stored_row: Dict[str, Any],
    *,
    resolve,
) -> Tuple[Dict[str, Any], List[str]]:
    """Returns (result, executed_update_queries).

    The stamp is SQL TEXT (`last_crawled_at = NOW()`), not a bound value, so the harness
    has to capture the query itself -- asserting on `values` would silently pass.
    """
    import routes.employee_products as mod

    queries: List[str] = []

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(query: str, values):
        queries.append((query, dict(values or {})))
        stored_row.update(values)

    recorder = _Recorder()
    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", resolve)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", recorder.record)
    monkeypatch.setattr(mod.destination_liveness, "retire_seed_for_dead_destination", recorder.retire)

    result = asyncio.run(mod._refresh_external_seed_by_id(stored_row["id"]))
    return result, queries


def _assigns(queries, column: str) -> bool:
    """Does any statement ASSIGN this column?

    A substring test cannot tell `SET last_crawled_at = NOW()` from a passing mention in a
    WHERE clause -- a mutant that only mentioned the column survived the first version of
    this suite. Anchor on the assignment inside SET.
    """
    return any(re.search(rf"\b{re.escape(column)}\s*=", _set_clause(q)) for q, _v in queries)


def _set_clause(query: str) -> str:
    match = re.search(r"\bSET\b(.*?)\bWHERE\b", query, re.S | re.I)
    return match.group(1) if match else ""


def _stamped(queries) -> bool:
    """Did the freshness clock ACTUALLY advance?

    The statement writes `last_crawled_at = CASE WHEN :reached_served_origin ...`, so the SET
    clause mentions the column on every run and the query TEXT cannot answer this. The bound
    flag is the decision -- asserting on text alone would pass for a refresh that never
    reached the origin, which is the precise bug this file exists to prevent.
    """
    for query, values in queries:
        assignment = _assignment_of(_set_clause(query), "last_crawled_at")
        if assignment is None:
            continue
        # THE SQL MUST ACTUALLY CONSULT THE FLAG. Reading `values` alone is not enough: a
        # mutant that replaced the CASE with a bare `= NOW()` still BOUND
        # `reached_served_origin`, so a helper trusting the parameter reported "not stamped"
        # for a statement that stamps every time -- and survived.
        if ":reached_served_origin" in assignment:
            return bool(values.get("reached_served_origin"))
        return True
    return False


def _assignment_of(set_clause: str, column: str) -> Optional[str]:
    """The right-hand side assigned to `column`, or None if it is never assigned."""
    match = re.search(
        rf"\b{re.escape(column)}\s*=\s*(.*?)(?=,\s*\n\s*[a-z_]+\s*=|\Z)",
        set_clause,
        re.S,
    )
    return match.group(1) if match else None


def _attempted(queries) -> bool:
    return _assigns(queries, "last_crawl_attempt_at")


# ------------------------------------------------------------------ the writer's half

def _ok(**snap):
    """A fetch that reached the origin for the URL we serve."""
    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
        return _snapshot(**snap)
    return resolve


def test_a_successful_refresh_stamps_both_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    result, queries = _run_refresh(monkeypatch, _seed_row(), resolve=_ok())

    assert result["status"] == "success"
    assert _stamped(queries), "a fetch that reached the origin must record that we READ it"
    assert _attempted(queries), "every terminal outcome must record that we TRIED"


def test_a_cached_snapshot_fallback_does_not_buy_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE HOLE THE FIRST VERSION OF THIS CHANGE SHIPPED.

    `resolve_external_offer` honours `raise_on_unavailable` only in its
    `except ExternalOfferUnavailable` arm. A timeout, a TLS/DNS error, a `RobotsDisallowed`
    (a bare RuntimeError) or a failure inside the extractor all land in its generic
    `except Exception`, which returns the CACHED snapshot -- so the refresh runs its entire
    success path having never left the process. `observed` stays empty; that is the only
    evidence available, and it is what the stamp is now gated on.

    Getting this wrong is not a missed stamp, it is an inverted queue: these hosts would be
    marked fresh on EVERY run and sorted permanently to the BACK, which is the starvation the
    column exists to remove.
    """
    async def resolve(*, observed=None, **kwargs):
        return _snapshot()  # a snapshot, but `observed` never populated

    result, queries = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] == "success"
    assert not _stamped(queries), "no origin response -- this row was never actually read"
    assert _attempted(queries), "but we did spend the request, so the queue must advance"


def test_a_fetch_of_a_url_we_do_not_serve_does_not_buy_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`destination_url` and `canonical_url` drift apart BY DESIGN -- this function rewrites
    canonical_url from the fetched page. Reading a legacy destination proves nothing about the
    link the buyer is handed, so it may not stamp the served row as fresh."""
    row = _seed_row(canonical_url="https://brand.com/products/toner-v2")

    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
        return _snapshot()

    _, queries = _run_refresh(monkeypatch, row, resolve=resolve)

    assert not _stamped(queries)
    assert _attempted(queries)


def test_the_stamp_rides_the_same_statement_as_the_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freshness and the value it vouches for must not be separable."""
    _, queries = _run_refresh(monkeypatch, _seed_row(), resolve=_ok(price_amount=31.0))

    stamping = [q for q, _v in queries if re.search(r"\blast_crawled_at\s*=", _set_clause(q))]
    assert len(stamping) == 1
    assert "price_amount" in stamping[0]


@pytest.mark.parametrize(
    "resolve_factory,label",
    [
        (lambda: _raises(ExternalOfferUnavailable(status_code=404, url=DEST)), "404"),
        (lambda: _raises(ExternalOfferUnavailable(status_code=429, url=DEST)), "429"),
        (lambda: _raises(RuntimeError("connection reset")), "transport"),
    ],
)
def test_a_failed_refresh_advances_the_queue_but_not_freshness(
    monkeypatch: pytest.MonkeyPatch, resolve_factory, label: str
) -> None:
    """Both halves matter and they pull in opposite directions.

    NOT fresh: a dead or unreadable link must never read as a verified price.
    BUT attempted: without this the seed keeps `last_crawl_attempt_at` NULL, stays first in the
    queue, and is refetched every run forever -- 10.4% of the corpus is already permanently
    broken, so the batch would spend itself on URLs proven gone and never reach the rest.
    """
    result, queries = _run_refresh(monkeypatch, _seed_row(), resolve=resolve_factory())

    assert result["status"] != "success"
    assert not _stamped(queries), f"{label}: a failure must not buy freshness"
    assert _attempted(queries), f"{label}: a failure must still advance the queue"


def test_an_unavailable_fetch_on_a_url_we_do_not_serve_still_advances_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The THIRD degraded exit, which the 404/429 cases above do not reach.

    When the fetched URL is not the one we serve, the function returns before recording any
    verdict -- correctly, since a verdict about a URL we do not serve is worse than none. But
    it still spent the request, so the queue must advance or this seed is retried forever.
    A mutant that dropped the stamp from exactly this branch survived until this test existed.
    """
    row = _seed_row(canonical_url="https://brand.com/products/toner-v2")

    async def resolve(*, observed=None, **kwargs):
        raise ExternalOfferUnavailable(status_code=404, url=DEST)

    result, queries = _run_refresh(monkeypatch, row, resolve=resolve)

    assert result["destination_refresh"]["reason"] == "not_the_served_url"
    assert not _stamped(queries)
    assert _attempted(queries)


def _raises(exc: BaseException):
    async def resolve(*, observed=None, **kwargs):
        if observed is not None and isinstance(exc, ExternalOfferUnavailable):
            observed["status_code"] = getattr(exc, "status_code", 500)
            observed["final_url"] = "https://brand.com/products/toner"
        raise exc
    return resolve


# ------------------------------------------------------------------ the reader's half

def _selector_queries(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    import services.external_referral_readiness as module

    seen: List[str] = []

    async def fake_fetch_all(query, values=None):
        seen.append(str(query))
        if "merchant_stores" in str(query):
            return [{"domain": "brand.com"}]
        if "attached_product_key IS NOT NULL" in str(query):
            return [{"id": "eps_attached_1"}]
        return [{"id": "eps_unattached_1", "domain": "brand.com"}]

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    asyncio.run(module.get_external_referral_refresh_candidate_seed_ids(limit=10))
    return [q for q in seen if "external_product_seeds" in q]


def _order_by_specs(query: str) -> List[str]:
    """FULL key specs, normalised -- e.g. `last_crawl_attempt_at ASC NULLS FIRST`.

    An earlier version returned bare column names, discarding ASC/DESC and NULLS placement. A
    mutant that flipped every key to `DESC NULLS LAST` -- re-crawling the most recently touched
    rows and sorting the never-crawled ones LAST, the worst possible order -- passed the whole
    suite. Direction and null placement ARE the semantics: bare `ASC` means NULLS LAST in
    Postgres, which also silently costs the partial index.
    """
    match = re.search(r"ORDER BY(.+?)(?:LIMIT|$)", query, re.S)
    assert match, f"no ORDER BY in: {query}"
    return [" ".join(part.split()) for part in match.group(1).split(",") if part.strip()]


def _order_by_keys(query: str) -> List[str]:
    return [spec.split()[0] for spec in _order_by_specs(query)]


def test_every_seed_query_leads_on_the_attempt_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOTH branches -- the attached and unattached queries are separate SQL strings, and
    fixing one while leaving the other is the shape of bug this asserts against."""
    queries = _selector_queries(monkeypatch)
    assert len(queries) == 2, "expected the attached and unattached seed queries"

    for query in queries:
        assert _order_by_keys(query)[0] == "last_crawl_attempt_at", (
            f"queue must lead on the attempt clock, got {_order_by_specs(query)!r}"
        )


def test_every_order_by_key_sorts_oldest_first_with_nulls_leading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE SEMANTICS, not just the column names.

    A mutant that flipped every key to `DESC NULLS LAST` -- re-crawling the most recently
    touched rows and putting the never-crawled ones last, the worst possible order -- passed
    the first version of this suite, because it only compared bare identifiers. `ASC` also
    defaults to NULLS LAST in Postgres, which additionally costs the partial index.
    """
    for query in _selector_queries(monkeypatch):
        for spec in _order_by_specs(query):
            assert spec.endswith("ASC NULLS FIRST"), (
                f"every key must be explicitly ASC NULLS FIRST, got {spec!r}"
            )


def test_freshness_sits_beneath_the_attempt_clock_and_updated_at_beneath_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering the queue on `last_crawled_at` is what deadlocks it; keeping it as the
    tiebreak is what makes the least-recently-VERIFIED row win within an attempt round."""
    for query in _selector_queries(monkeypatch):
        keys = _order_by_keys(query)
        for column in ("last_crawl_attempt_at", "last_crawled_at", "updated_at"):
            assert column in keys, f"{column} missing from {keys!r}"
        assert keys.index("last_crawl_attempt_at") < keys.index("last_crawled_at")
        assert keys.index("last_crawled_at") < keys.index("updated_at")


def test_the_attached_query_is_the_one_that_was_starved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the original pathology: attaching bumps `updated_at`, and the attached query is
    the one selecting on `attached_product_key` -- so servable rows, the only ones whose price
    a buyer can see, sank in the queue."""
    attached = [q for q in _selector_queries(monkeypatch) if "attached_product_key IS NOT NULL" in q]
    assert len(attached) == 1
    assert _order_by_keys(attached[0])[0] == "last_crawl_attempt_at"
