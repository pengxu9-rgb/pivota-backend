"""
Phase C-4 / PR-D: Pivota canonical PDP indexing-arc state.

When a merchant audit falls through to fallback (c) — the Pivota
canonical PDP at agent.pivota.cc/products/sig_<hex> — the report
needs to set expectations honestly: this URL has a real Google
indexing arc that gates grounded LLM citations. The arc is well-
characterized empirically (see scripts/agent_center_pivota_pdp_baseline.py
+ reports/pivota-pdp-baseline.md): fresh URLs aren't crawled, then
crawled but not deeply indexed, then indexed and citation-eligible.

Pre-PR-D the report carried a static "30-90 day" caveat regardless
of when the sig was minted. Post-PR-D each per-product report
computes the actual phase from the row's `pivota_signature_minted_at`
timestamp:

  fresh             — < 7d since mint. Google likely hasn't crawled
                      yet. Citations not expected; merchant should
                      confirm the canonical URL is in their sitemap +
                      submitted to Search Console.
  indexing          — 7d-90d. Google crawl + first-pass indexing
                      window. Some queries may start surfacing the
                      URL toward the end of this band.
  expected_steady   — > 90d. Should be in steady-state; if grounded
                      citations remain at zero, the diagnostic shifts
                      to "your URL is indexed but isn't winning queries"
                      (a content / SEO issue, not an arc issue).

Pure function — no I/O, no DB access. The route + service layer pass
in `minted_at` and `now` (so tests can deterministically test phase
boundaries).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


FRESH_DAYS_MAX = 7
INDEXING_DAYS_MAX = 90


def compute_indexing_arc_state(
    minted_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return a JSON-serializable dict describing the arc state.

    `minted_at`: when the sig + canonical_url were first persisted
    (set by services.catalog_sync_service.make_pivota_canonical_fields
    on go-forward sync, or by routes/merchant_audit_routes.py's lazy-
    mint UPDATE on first audit, or by migration 073's backfill =
    catalog_products.created_at for legacy rows).

    `now`: current UTC time (override for deterministic tests).
    Defaults to `datetime.now(timezone.utc)`.

    Returns:
      {
        "phase": "fresh" | "indexing" | "expected_steady" | "unknown",
        "days_since_mint": int | None,
        "minted_at": ISO 8601 string | None,
        "expected_first_citation_at": ISO 8601 string | None,
                  -- the boundary date when "indexing" → "expected_steady",
                  -- so the merchant has a concrete date to re-check.
        "caveat": short merchant-facing sentence describing this phase,
      }
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if minted_at is None:
        return {
            "phase": "unknown",
            "days_since_mint": None,
            "minted_at": None,
            "expected_first_citation_at": None,
            "caveat": (
                "Pivota canonical PDPs follow a 30-90 day Google "
                "indexing arc post-publication. Mint timestamp not "
                "available for this row — re-audit after content sync "
                "completes to get a precise arc estimate."
            ),
        }

    # Normalize to UTC. Naive datetimes (e.g. SQLite's TIMESTAMP
    # without tz) are treated as UTC — Postgres TIMESTAMPTZ already
    # carries tz info.
    if minted_at.tzinfo is None:
        minted_at = minted_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    days_since = max(0, int((now - minted_at).total_seconds() // 86400))
    expected_first_citation_at = minted_at + timedelta(days=INDEXING_DAYS_MAX)

    if days_since < FRESH_DAYS_MAX:
        phase = "fresh"
        caveat = (
            f"This canonical URL was minted {days_since} day"
            f"{'s' if days_since != 1 else ''} ago. Google typically "
            f"hasn't crawled it yet at this stage; grounded LLM "
            f"citations are not expected. Confirm the URL is in your "
            f"sitemap + submitted to Search Console URL Inspection."
        )
    elif days_since < INDEXING_DAYS_MAX:
        phase = "indexing"
        caveat = (
            f"This canonical URL was minted {days_since} days ago — "
            f"in the Google indexing window (typically 7-90 days). "
            f"First grounded citations may appear toward the end of "
            f"this band. Re-audit on or after "
            f"{expected_first_citation_at.date().isoformat()} to "
            f"check progress."
        )
    else:
        phase = "expected_steady"
        caveat = (
            f"This canonical URL was minted {days_since} days ago — "
            f"past the typical 90-day indexing arc. Google should "
            f"have indexed it by now. If grounded citations are still "
            f"zero, the diagnostic shifts from 'wait for indexing' to "
            f"'your URL is indexed but isn't winning queries' — a "
            f"content / SEO problem, not an arc problem."
        )

    return {
        "phase": phase,
        "days_since_mint": days_since,
        "minted_at": minted_at.isoformat(),
        "expected_first_citation_at": expected_first_citation_at.isoformat(),
        "caveat": caveat,
    }
