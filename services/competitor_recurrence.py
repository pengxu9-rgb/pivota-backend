"""Cross-audit competitor-brand recurrence — the catalog-coverage demand proxy.

Which competitor brands recur across MANY merchant audits is a proprietary,
compounding signal of what to onboard into the commerce index FIRST: a brand that
keeps showing up as Pivota audits more merchants in a category has more proven
shopper/competitive demand than one seen once. Same idea as
`niche_query_recurrence` (distinct-merchant demand proxy), applied to the
`competitors_named` the audits surface — and read the same way `host_recurrence`
reads citation_observations.

Read-time aggregation over `merchant_audit_runs.report_jsonb` (the audits already
store the competitor landscape) via a single `jsonb_path_query` extraction — no new
table, no audit-pipeline change. Best-effort: any DB miss degrades to [] rather than
raising. If report-scanning ever gets slow, mirror niche_recurrence with an upsert
table recorded on each audit (noted in the catalog-coverage memo).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

# Pull every competitors_named string across skus[].authority_hosts[], tolerant of
# missing/non-array nodes (jsonb_path_query yields nothing for those).
_EXTRACT_SQL = """
    SELECT mar.merchant_id AS merchant_id,
           mar.run_id::text AS run_id,
           (comp #>> '{}') AS competitor
    FROM merchant_audit_runs mar,
         LATERAL jsonb_path_query(
             mar.report_jsonb,
             '$.authority_map.skus[*].authority_hosts[*].competitors_named[*]'
         ) AS comp
    WHERE mar.status = 'succeeded' AND mar.report_jsonb IS NOT NULL
"""


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


# competitors_named is LLM-extracted, so it includes marketplaces / retailers /
# platforms that are NOT brands to onboard as canonical anchors. A conservative
# stoplist keeps the curation queue clean. (Brand-vs-retailer is fuzzy; only the
# clearest non-brands are listed — anything ambiguous is kept.)
_NON_BRAND = {
    _norm(x)
    for x in (
        "ebay", "amazon", "amazon com", "walmart", "target", "costco", "kroger",
        "macys", "macy s", "jcpenney", "sephora", "ulta", "ulta beauty", "nordstrom",
        "olive young", "olive young global", "olive young us", "yesstyle", "stylevana",
        "iherb", "aliexpress", "etsy", "google shopping", "tiktok shop", "instagram",
        "holiholic", "dodo skin", "shop app", "best buy",
    )
}


async def top_recurring_competitors(
    *,
    limit: int = 100,
    min_merchants: int = 1,
    exclude_non_brands: bool = True,
    db: Any = None,
) -> List[Dict[str, Any]]:
    """Rank competitor brands by cross-merchant audit recurrence.

    Returns dicts: {brand (display), normalized, distinct_merchants, distinct_audits,
    total_mentions}, ordered by distinct_merchants desc then total_mentions desc.
    `min_merchants` floors out single-audit noise; `exclude_non_brands` drops obvious
    marketplaces/retailers. Empty list on any DB miss."""
    read_db = db or database
    try:
        rows = await read_db.fetch_all(_EXTRACT_SQL)
    except Exception as exc:  # noqa: BLE001
        logger.debug("competitor_recurrence extract failed: %s", str(exc)[:200])
        return []

    merchants: Dict[str, set] = {}
    audits: Dict[str, set] = {}
    mentions: Counter = Counter()
    display: Dict[str, Counter] = {}
    for r in rows or []:
        comp = str(r["competitor"] or "").strip()
        key = _norm(comp)
        if not key:
            continue
        merchants.setdefault(key, set()).add(r["merchant_id"])
        audits.setdefault(key, set()).add(r["run_id"])
        mentions[key] += 1
        display.setdefault(key, Counter())[comp] += 1

    out: List[Dict[str, Any]] = []
    for key, m in merchants.items():
        if len(m) < max(1, int(min_merchants or 1)):
            continue
        if exclude_non_brands and key in _NON_BRAND:
            continue
        out.append(
            {
                "brand": display[key].most_common(1)[0][0],  # most frequent original casing
                "normalized": key,
                "distinct_merchants": len(m),
                "distinct_audits": len(audits.get(key, ())),
                "total_mentions": mentions[key],
            }
        )
    out.sort(key=lambda d: (d["distinct_merchants"], d["total_mentions"]), reverse=True)
    return out[: max(1, int(limit or 1))]


async def recurrence_rank(*, db: Any = None) -> Dict[str, int]:
    """Map normalized-brand -> distinct_merchants, for prioritizing a candidate
    list (e.g. ordering audit_to_candidates output by demand). Best-effort {}."""
    top = await top_recurring_competitors(limit=100000, min_merchants=1, db=db)
    return {d["normalized"]: d["distinct_merchants"] for d in top}
