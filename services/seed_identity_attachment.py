"""Seed → PDP identity attachment (convergence P1.3) — flag-gated, DARK by default.

Populates external_product_seeds.attached_product_key by DETERMINISTIC-EXACT
matching only (source_product_id / canonical_url), mirroring the audit ER gate's
_EXACT_MATCHERS trust split. The fuzzy title-trigram matcher NEVER auto-attaches
here — a brand+title collision (no-GTIN content_key is deliberately non-unique)
must route to HITL review, not silently merge distinct products.

This is the Phase-2 multi-offer serving prerequisite: an attached seed dedups
against its canonical product so the same physical product isn't served twice.

!!! CO-GATE WARNING — why this is DARK by default !!!
Attaching a seed removes it from the LIVE mainline external-seed serving lane
(services/external_seed_search.fetch_external_seed_rows(only_unattached=True) →
WHERE attached_product_key IS NULL). The canonical replacement is served ONLY by
the pivot lane (PIVOT_MULTI_SERVE_ENABLED), which is OFF in prod. So enabling
attachment BEFORE the pivot serve cutover would DROP those products from serving
with no replacement. Enable SEED_IDENTITY_ATTACHMENT_ENABLED in lockstep with the
Phase-2 pivot serve cutover — never before. See plan fluffy-inventing-treasure.md.

The matcher itself is doubly-safe: _candidates_for_seed restricts candidates to
pdp_scope='multi_merchant_canonical' (a cross-merchant seed can never attach to a
single-merchant private PDP), and only the two EXACT signals auto-apply.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from services.pdp_matcher.deterministic import matches_for_seed

logger = logging.getLogger(__name__)

# Only deterministic-EXACT signals auto-attach. Keep in lockstep with
# services/audit_index_intake._EXACT_MATCHERS (the audit ER gate's split).
_EXACT_MATCHERS = frozenset({"source_product_id_match", "canonical_url_match"})

_FLAG = "SEED_IDENTITY_ATTACHMENT_ENABLED"


def seed_identity_attachment_enabled() -> bool:
    """Master gate. Default OFF — see the CO-GATE WARNING above."""
    return os.getenv(_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


async def attach_seed_identity_exact(
    seed: Dict[str, Any],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Attach one seed to an existing multi_merchant_canonical PDP iff a
    deterministic-EXACT match exists. Returns the match dict
    ({product_key, matcher, confidence, ...}) when it attaches (or would, under
    dry_run), else None.

    - ``force`` bypasses the master flag — for the operator backfill, a
      deliberate action. The write-time hook passes force=False so it stays dark
      until the flag flips alongside the pivot cutover.
    - Idempotent: skips a seed that already carries attached_product_key, and the
      underlying UPDATE is ``WHERE attached_product_key IS NULL``.
    - A fuzzy-only or no match leaves the seed UNATTACHED (NULL) — recoverable by
      the HITL review path / batch runner, never silently merged here.
    """
    if not force and not seed_identity_attachment_enabled():
        return None
    if not isinstance(seed, dict) or not seed.get("id"):
        return None
    if seed.get("attached_product_key"):
        return None

    # Reuse the batch matcher's candidate + apply primitives so this and the
    # runner CLI converge on identical behavior (candidates are scope-gated).
    from services.pdp_matcher.runner import _apply_attachment, _candidates_for_seed

    candidates = await _candidates_for_seed(seed)
    if not candidates:
        return None
    match = matches_for_seed(seed=seed, candidates=candidates)
    if not match or match.get("matcher") not in _EXACT_MATCHERS:
        return None

    await _apply_attachment(
        seed_id=str(seed["id"]),
        product_key=str(match["product_key"]),
        matcher=str(match["matcher"]),
        confidence=float(match.get("confidence") or 0.0),
        evidence=str(match.get("evidence") or "exact"),
        dry_run=dry_run,
    )
    return match


async def attach_seed_identity_best_effort(seed: Dict[str, Any]) -> None:
    """Write-time hook shape: flag-gated (force=False) and never raises — safe to
    call from the seed ingest path. No-op branch when the flag is off (DARK)."""
    if not seed_identity_attachment_enabled():
        return
    try:
        await attach_seed_identity_exact(seed, force=False)
    except Exception as exc:  # noqa: BLE001 — must never break seed ingest
        logger.warning(
            "seed_identity_attachment: attach failed for seed=%s: %s",
            seed.get("id"), str(exc)[:200],
        )
