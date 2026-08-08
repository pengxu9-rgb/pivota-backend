"""Takedown of already-ingested ad-campaign landing pages (PIVOTA-Agent#1926).

This runner withdraws LIVE products from serving, so the tests are weighted to
the two ways it can be wrong in a way nobody notices:

* withdrawing something real — pinned by the cluster predicate tests;
* reporting a takedown that did not happen — pinned by the read-back tests.

The cluster predicate exists because fetching the URL cannot separate an ad
variant from a delisted-but-live product: measured in prod, biodance's ad pages
and TONYMOLY's real sheet masks BOTH return 200 / og:type=product / InStock.
What separates them is structural — an ad farm puts many rows on one
content_key; a delisted real product is a lone occupant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.remediate_unpublished_crawl_rows as remediate  # noqa: E402
from services.shopify_publication_signal import UNKNOWN, UNPUBLISHED  # noqa: E402


class _FakeDB:
    def __init__(self, serving_by_key=None):
        self.executed = []
        self._serving = serving_by_key or {}

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), values or {}))

    async def fetch_all(self, query, values=None):
        return []

    async def fetch_one(self, query, values=None):
        ck = (values or {}).get("ck")
        if ck not in self._serving:
            return None
        return {"serving_eligible": self._serving[ck]}

    def touching(self, fragment):
        return [(q, v) for q, v in self.executed if fragment in q]


def _row(seed_id, url, content_key="ck_1", reason=None, rows_on_key=5, seed_status="active"):
    return {
        "seed_id": seed_id, "destination_url": url, "content_key": content_key,
        "product_key": f"prod::{seed_id}", "seed_status": seed_status,
        "suppression_reason": reason, "suppressed_at": None, "rows_on_key": rows_on_key,
    }


def _patch(monkeypatch, db):
    monkeypatch.setattr(remediate, "database", db)
    monkeypatch.setattr(remediate, "recompute_serving_eligibility",
                        lambda ck, reason=None: _async(None))


# --- what actually gets written ---------------------------------------------


@pytest.mark.asyncio
async def test_withdraw_sets_suppressed_at_not_just_a_reason(monkeypatch):
    """suppression_reason alone leaves the PDP serving — the serving gate keys
    on suppressed_at and nothing else."""
    db = _FakeDB({"ck_1": False})
    _patch(monkeypatch, db)
    await remediate._withdraw([_row("s1", "https://biodance.com/products/0627_cm_a")])

    assert db.touching("suppressed_at=NOW()")
    assert db.touching("status='inactive'")
    assert db.touching("suppression_reason=:reason")


@pytest.mark.asyncio
async def test_withdraw_preserves_an_existing_suppression_reason(monkeypatch):
    db = _FakeDB({"ck_1": False})
    _patch(monkeypatch, db)
    await remediate._withdraw([_row("s1", "https://x.com/products/a", reason="step5_campaign_clone_dup")])
    assert all("suppression_reason IS NULL" in q for q, _ in db.touching("suppression_reason=:reason"))


@pytest.mark.asyncio
async def test_withdraw_records_prior_seed_status_for_revert(monkeypatch):
    db = _FakeDB({"ck_1": False})
    _patch(monkeypatch, db)
    await remediate._withdraw([_row("s1", "https://x.com/products/a", seed_status="inactive")])
    meta = db.touching("suppression_metadata")
    assert meta and meta[0][1]["prior"] == "inactive"
    assert meta[0][1]["script"] == remediate.SCRIPT_NAME


@pytest.mark.asyncio
async def test_every_write_is_idempotency_guarded(monkeypatch):
    db = _FakeDB({"ck_1": False})
    _patch(monkeypatch, db)
    await remediate._withdraw([_row("s1", "https://x.com/products/a")])
    for q, _ in db.executed:
        assert " IS NULL" in q or "status='active'" in q, f"unguarded write: {q}"


# --- the read-back: a failed recompute must not read as a takedown -----------


@pytest.mark.asyncio
async def test_states_are_read_back_from_the_table_not_inferred(monkeypatch):
    """recompute_serving_eligibility swallows exceptions and returns False, so
    its return value cannot tell 'went dark' from 'blew up'. Ask the table."""
    db = _FakeDB({"ck_dark": False, "ck_live": True})  # ck_missing absent entirely
    _patch(monkeypatch, db)
    states = await remediate._withdraw([
        _row("s1", "https://x.com/products/a", content_key="ck_dark"),
        _row("s2", "https://x.com/products/b", content_key="ck_live"),
        _row("s3", "https://x.com/products/c", content_key="ck_missing"),
    ])
    assert states == {"ck_dark": "dark", "ck_live": "serving", "ck_missing": "unknown"}


# --- the cluster predicate ---------------------------------------------------


@pytest.mark.asyncio
async def test_lone_sitemap_absent_rows_are_spared(monkeypatch):
    """Measured: every false positive in review (21 live TONYMOLY sheet masks,
    15 live arencia.us serums) was a LONE occupant of its content_key, and every
    biodance ad-farm row was clustered. So a lone row is never withdrawn."""
    rows = [
        _row("tonymoly_1", "https://tonymoly.us/products/i-am-peach-sheet-mask",
             content_key="ck_lone", rows_on_key=1),
        _row("biodance_1", "https://biodance.com/products/0627_cm_a",
             content_key="ck_farm", rows_on_key=45),
    ]

    class _Oracle:
        async def classify(self, item):
            return UNPUBLISHED

    monkeypatch.setattr(remediate, "PublicationOracle", lambda: _Oracle())
    actionable, _counts = await remediate._adjudicate(rows)

    kept = [r for r in actionable if int(r["rows_on_key"]) >= 2]
    spared = [r for r in actionable if int(r["rows_on_key"]) < 2]
    assert [r["seed_id"] for r in kept] == ["biodance_1"]
    assert [r["seed_id"] for r in spared] == ["tonymoly_1"]


def test_candidates_query_computes_cluster_size():
    assert "rows_on_key" in remediate.CANDIDATES_SQL
    assert "suppressed_at IS NULL" in remediate.CANDIDATES_SQL
    # step5-suppressed rows are the ones whose takedown never landed; excluding
    # them by reason would skip 213 of biodance's 250.
    assert "suppression_reason IS NULL" not in remediate.CANDIDATES_SQL


# --- revert ------------------------------------------------------------------


def test_revert_reads_a_different_row_set_than_withdrawal():
    """Sharing CANDIDATES_SQL made --revert a silent no-op: it excludes rows
    that ARE suppressed, which is exactly the set revert needs."""
    assert "suppressed_at IS NOT NULL" in remediate.REVERT_CANDIDATES_SQL
    assert "suppression_metadata->>'script'" in remediate.REVERT_CANDIDATES_SQL


@pytest.mark.asyncio
async def test_revert_restores_both_halves(monkeypatch):
    """Clearing suppressed_at alone leaves a NEW half-state (live row, dead
    seed), not the state we found."""
    db = _FakeDB({"ck_1": True})
    _patch(monkeypatch, db)
    row = _row("s1", "https://x.com/products/a")
    row["suppression_metadata"] = {"script": remediate.SCRIPT_NAME, "prior_seed_status": "active"}
    await remediate._revert([row])

    assert db.touching("suppressed_at=NULL")
    assert db.touching("status='active'")


@pytest.mark.asyncio
async def test_revert_does_not_resurrect_a_seed_that_was_already_dead(monkeypatch):
    db = _FakeDB({"ck_1": True})
    _patch(monkeypatch, db)
    row = _row("s1", "https://x.com/products/a")
    row["suppression_metadata"] = {"script": remediate.SCRIPT_NAME, "prior_seed_status": "inactive"}
    await remediate._revert([row])
    assert not db.touching("status='active'")


@pytest.mark.asyncio
async def test_revert_leaves_a_foreign_suppression_reason_alone(monkeypatch):
    """A row that carried step5's reason keeps it; we only clear our own."""
    db = _FakeDB({"ck_1": True})
    _patch(monkeypatch, db)
    row = _row("s1", "https://x.com/products/a")
    row["suppression_metadata"] = {"script": remediate.SCRIPT_NAME}
    await remediate._revert([row])
    clears = db.touching("suppression_reason=NULL")
    assert clears and "suppression_reason=:reason" in clears[0][0]


# --- adjudication ------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_unpublished_rows_are_actionable(monkeypatch):
    verdicts = {
        "https://x.com/products/real": "published",
        "https://x.com/products/0627_cm_ad": UNPUBLISHED,
        "https://nosite.com/products/x": UNKNOWN,
    }

    class _Oracle:
        async def classify(self, item):
            return verdicts[item["destination_url"]]

    monkeypatch.setattr(remediate, "PublicationOracle", lambda: _Oracle())
    rows = [_row(f"s{i}", u) for i, u in enumerate(verdicts)]
    actionable, counts = await remediate._adjudicate(rows)

    assert [r["destination_url"] for r in actionable] == ["https://x.com/products/0627_cm_ad"]
    assert counts[UNKNOWN] == 1


def test_host_filter_is_www_insensitive():
    assert remediate._host_of("https://www.biodance.com/products/a") == "biodance.com"
    assert remediate._host_of("https://biodance.com/products/a") == "biodance.com"
    assert remediate._host_of(None) == ""


async def _async(value):
    return value


def test_bind_params_in_untypeable_positions_are_cast():
    """Regression: the first production --apply died at PREPARE with "could not
    determine data type of parameter $2". jsonb_build_object is variadic "any",
    so Postgres cannot infer a bind param's type from position — every param
    fed to it, or compared against a ->> extraction, needs an explicit CAST.

    The unit tests here record SQL against a fake connection and never ask
    Postgres to plan it, so nothing else in this file can catch that class of
    bug. This pins the casts textually as the cheap substitute."""
    src = Path(remediate.__file__).read_text(encoding="utf-8")
    build_obj = src[src.index("jsonb_build_object("):]
    build_obj = build_obj[: build_obj.index("updated_at = NOW()")]
    assert ":script" not in build_obj.replace("CAST(:script AS text)", "")
    assert ":prior" not in build_obj.replace("CAST(:prior AS text)", "")
    assert "CAST(:script AS text)" in remediate.REVERT_CANDIDATES_SQL
