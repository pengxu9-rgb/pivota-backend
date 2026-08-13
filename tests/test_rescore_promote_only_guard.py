"""The rescore must be promote-only as an ENFORCED property, not an emergent one.

Before this guard, "the rescore can only promote" rested entirely on the FETCH
filter `NOT ips.serving_eligible` — which `--include-eligible` removes, and which
the adversarial review of #1741 identified as the single point between an
ordinary ops run and demoting currently-public products (append-only snapshots +
latest-wins eligibility lateral + IndexNow firing on the down transition).

Now the drive loop PREVIEWS each eligible candidate's score with the identical
payload and source-backed components, and refuses the row when it would land
below QUALITY_SCORE_THRESHOLD. Dark rows are exempt — a low rescore leaves them
exactly as dark as they were.

Also covered: `_rescored_ids()` was platform- and merchant-blind
(`SELECT DISTINCT platform_product_id`), so a cross-merchant id collision
silently skipped an unscored product. Now keyed by the full identity triple.

Each test here is verified to kill a specific mutant; see the commit message.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.backfill_external_seed_quality_rescore as mod  # noqa: E402


def _row(pid: str, *, merchant: str = "m1", eligible: bool = False) -> Dict[str, Any]:
    return {
        "product_key": f"prod::{merchant}::external_seed::{pid}",
        "merchant_id": merchant,
        "source_product_id": pid,
        "is_serving_eligible": eligible,
        "title": f"Product {pid}", "description": "d " * 40, "brand": "B",
        "product_type": "serum", "category_kind": "beauty",
        "image_url": "https://x/i.jpg", "seed_id": f"seed::{pid}",
        "price_amount": 10.0, "raw_inci": "a, b, c, d, e, f, g, h",
        "pdp_details_sections": None,
    }


class _FakeDB:
    """Dispatches the two module queries; everything else is a no-op."""

    def __init__(self, candidates: List[Dict[str, Any]],
                 done_triples: Optional[List[tuple]] = None) -> None:
        self._candidates = candidates
        self._done = done_triples or []
        self.is_connected = True

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def fetch_all(self, sql: str, params: Dict[str, Any] = None):
        text = " ".join(str(sql).split()).lower()
        if "from product_quality_snapshot" in text:
            return [
                {"merchant_id": m, "platform": p, "platform_product_id": i}
                for (m, p, i) in self._done
            ]
        if "from catalog_products" in text:
            return list(self._candidates)
        raise AssertionError(f"unexpected fetch_all: {text[:90]}")

    async def fetch_one(self, sql: str, params: Dict[str, Any] = None):
        return {"n": 0}


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """Real run(), fake DB, recorded servability calls, deterministic scorer."""
    calls: List[str] = []
    scores: Dict[str, float] = {}

    async def fake_servable(*, product_key, seed_id, source_product_id,
                            quality_payload, reason):
        calls.append(source_product_id)
        return {"quality": True, "serving_eligible": True}

    def fake_preview(payload, score_source_backed_components=None):
        # Keyed by title so each candidate can carry its own score.
        title = str(payload.get("title") or payload.get("title_canonical") or "")
        pid = title.replace("Product ", "")
        return {"content_quality_score": scores.get(pid, 99.0)}

    monkeypatch.setattr(mod, "make_external_seed_servable", fake_servable)
    monkeypatch.setattr(mod, "preview_quality", fake_preview)

    def install(db: _FakeDB) -> None:
        monkeypatch.setattr(mod, "database", db)

    return {"calls": calls, "scores": scores, "install": install}


@pytest.mark.asyncio
async def test_an_eligible_row_scoring_below_the_bar_is_refused(harness, capsys) -> None:
    """The guard itself: currently-serving + preview < 71.4 -> no write at all."""
    harness["scores"]["p-low"] = 60.0
    harness["install"](_FakeDB([_row("p-low", eligible=True)]))

    await mod.run(apply=True, limit=None, include_eligible=True, skip_trust=True)

    assert harness["calls"] == [], "a would-demote row reached the write path"
    out = capsys.readouterr().out
    assert "would-demote" in out
    assert "would_demote_skipped=1" in out


@pytest.mark.asyncio
async def test_an_eligible_row_scoring_above_the_bar_proceeds(harness) -> None:
    harness["scores"]["p-high"] = 88.0
    harness["install"](_FakeDB([_row("p-high", eligible=True)]))

    await mod.run(apply=True, limit=None, include_eligible=True, skip_trust=True)
    assert harness["calls"] == ["p-high"]


@pytest.mark.asyncio
async def test_a_dark_row_is_rescored_regardless_of_score(harness) -> None:
    """Dark rows are exempt on purpose: a low rescore cannot demote what is
    already not serving, and blocking them would freeze the whole backlog —
    the population this run exists to move."""
    harness["scores"]["p-dark"] = 12.0
    harness["install"](_FakeDB([_row("p-dark", eligible=False)]))

    await mod.run(apply=True, limit=None, include_eligible=False, skip_trust=True)
    assert harness["calls"] == ["p-dark"]


@pytest.mark.asyncio
async def test_done_skip_is_scoped_to_the_full_identity_triple(harness) -> None:
    """A v3 snapshot belonging to ANOTHER merchant under the same
    platform_product_id must not mark this product done. The pid-only version
    did exactly that — a cross-merchant slug collision silently starved the
    unscored product forever."""
    harness["install"](_FakeDB(
        [_row("shared-slug", merchant="m1"), _row("other", merchant="m1")],
        done_triples=[
            ("m2", "external_seed", "shared-slug"),   # someone ELSE's snapshot
            ("m1", "external_seed", "other"),         # genuinely done
        ],
    ))

    await mod.run(apply=True, limit=None, skip_trust=True)
    assert harness["calls"] == ["shared-slug"], (
        "either a cross-merchant collision starved the product, or a genuinely "
        "done product was re-scored"
    )
