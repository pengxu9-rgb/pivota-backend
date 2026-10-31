#!/usr/bin/env python3
"""READ-ONLY measure: which live rows the setting-spray leaf (beauty/makeup/face/setting-spray) re-files.

Run it on an image that carries the leaf (it refuses to run on one that does not), after the merge and
before the seeder: it needs the code's resolvers, not the category_taxonomy row.

WHAT IT DOES.
  1. Preconditions, each printed as a JSON line; it stops at the first that fails and never prints
     GATE_OPEN then:
       * DATABASE_URL is set;
       * this image's taxonomy has the leaf (an older image would measure nothing and look clean);
       * the transaction is READ ONLY (`SHOW transaction_read_only` = on) and its
         `SET LOCAL statement_timeout` took (`SHOW statement_timeout`).
     Then it prints the bare line `GATE_OPEN`.
  2. Selects the candidates with PostgreSQL regex (`\\y` is the word boundary there; `\\b` is a
     backspace), generated from the classifier's own SETTING_SPRAY_PHRASES: rows on the old gap
     spelling, and rows whose title, merchant type or category names a setting phrase (CANDIDATE_SQL).
  3. Runs the REAL resolvers from this image on each row, in Python:
       * `row`  = services.pdp_category_classifier.resolve_path_from_row: the classifier's answer for the
         row. The backfill WRITES it only on a NULL or interior path and only when strictly deeper, so a
         toner -> leaf or powder -> "" move here is not a backfill write;
       * `feed` = services.curated_brand_feed._resolve_category under a coarse "beauty" job flag (what a
         drain re-ingest re-derives; the drain passes the job's category, not the row's stored path).
         "" is UNRESOLVED: the drain leaves that product out of its plan (category_unresolved), so a
         re-ingest does not re-file it. "beauty" is the coarse fallback (0.3), which the drain marks
         resolved: every two-product shelf reads that way.
  4. Prints from->to counts by source_domain for each resolver, serving rows counted separately, with the
     combined spray+powder shelf and every other multi-product type broken out, and one `key` line per
     row whose answer moves (the measured key list the re-file steps are scoped to).
  5. Prints the taxonomy drift as `taxonomy_code_vs_table_drift` computes it, and the same with this
     leaf's own two rows (the leaf and its gap alias) taken out, so it compares with the baseline taken
     on the previous image.

OUTPUT. Every line is TEXT, so Cloud Logging keeps it as textPayload (a bare-JSON stdout line lands in
jsonPayload and comes back blank through run_oneoff_job.sh's `value(textPayload,...)`):
`MEASURE {json}` for every line, `MEASURE_KEY {json}` per moved key, the bare `GATE_OPEN` after the
preconditions and again at the end, and last `MEASURE_DONE lines=N`, N = the lines printed before it,
so a reader can check that nothing was dropped (the log read is capped and newest first).

SAFETY. One READ ONLY transaction with SET LOCAL statement_timeout; it writes nothing and creates
nothing:

    IMAGE=.../backend:<merge sha> scripts/ops/run_oneoff_job.sh scripts/ops/measure_setting_spray_refile.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

if os.path.isdir("/app") and "/app" not in sys.path:
    sys.path.insert(0, "/app")

STATEMENT_TIMEOUT = "30s"
LEAF = "beauty/makeup/face/setting-spray"
GAP_SPELLING = "beauty/makeup/setting-spray"
CANDIDATE_SQL = """
SELECT cp.product_key, cp.category_path, cp.category, cp.product_type, cp.title, cp.source_domain,
       coalesce(ips.serving_eligible, false) AS serving_eligible
FROM catalog_products cp
LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
WHERE cp.category_path = $1 OR cp.title ~* $2 OR cp.product_type ~* $2 OR cp.category ~* $2
ORDER BY cp.product_key
"""


def candidate_regex() -> str:
    """The classifier's Setting Spray phrases (pdp_category_classifier.SETTING_SPRAY_PHRASES), in
    PostgreSQL's dialect: only the word boundary differs."""
    from services.pdp_category_classifier import SETTING_SPRAY_PHRASES

    return r"\y(" + SETTING_SPRAY_PHRASES.replace(r"\b", r"\y") + r")\y"


class _Rows:
    """`fetch_all` over the transaction's own connection, for services.category_taxonomy_store.load."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None) -> List[Any]:
        return await self._conn.fetch(query)


def _verdicts(row: Dict[str, Any]) -> Dict[str, str]:
    from services import curated_brand_feed as feed
    from services.pdp_category_classifier import resolve_path_from_row

    hit = resolve_path_from_row(category=row.get("category"), product_type=row.get("product_type"),
                                title=row.get("title"))
    # The drain passes its JOB's category as the flag path (shopify_product_to_record), not the row's
    # stored one: a coarse "beauty" job is the common case. A job flagged with a leaf keeps that leaf.
    # ...and then folds the result through the alias map, as shopify_product_to_record does.
    from services.category_path_aliases import resolve

    path, _confidence = feed._resolve_category(product_type=row.get("product_type"), title=row.get("title"),
                                               flag_path="beauty", domain=row.get("source_domain"))
    return {"row": hit[1] if hit else "", "feed": resolve(path) or ""}


def _shelf_kind(product_type: Optional[str]) -> str:
    from services import curated_brand_feed as feed

    if not str(product_type or "").strip():
        return "none"
    if feed._shelf_paths(product_type) in feed._COMBINED_SHELVES:
        return "combined_spray_powder"
    return "multi_product" if feed._pattern_matches(product_type) > 1 else "single"


def summarize(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The JSON lines for `rows` (dicts with the CANDIDATE_SQL columns)."""
    lines: List[Dict[str, Any]] = []
    by_domain: Dict[tuple, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    for row in rows:
        before = row.get("category_path") or ""
        after = _verdicts(row)
        shelf = _shelf_kind(row.get("product_type"))
        serving = bool(row.get("serving_eligible"))
        for resolver, path in after.items():
            moved = path != before
            by_domain[(row.get("source_domain") or "", resolver, shelf)][(before, path, serving)] += 1
            totals[(resolver, "moved" if moved else "same", "serving" if serving else "not_serving")] += 1
        if any(path != before for path in after.values()):
            lines.append({"kind": "key", "product_key": row["product_key"], "source_domain": row.get("source_domain"),
                          "serving": serving, "shelf": shelf, "from": before, "row": after["row"], "feed": after["feed"],
                          "product_type": row.get("product_type")})
    for (domain, resolver, shelf), counts in sorted(by_domain.items()):
        lines.append({"kind": "from_to", "source_domain": domain, "resolver": resolver, "shelf": shelf,
                      "moves": [{"from": f, "to": t, "serving": s, "rows": n}
                                for (f, t, s), n in sorted(counts.items())]})
    lines.append({"kind": "summary", **{"_".join(k): v for k, v in sorted(totals.items())}})
    return lines


def drift_lines(drift: Dict[str, Any]) -> List[Dict[str, Any]]:
    detail = drift.get("detail") or {}
    seen = list(drift.get("sample_keys") or [])
    for name in ("in_code_not_in_table", "in_table_not_in_code", "alias_conflicts", "stale_table_aliases"):
        seen += list(detail.get(name) or [])
    own = sorted({k for k in seen if LEAF in k or GAP_SPELLING in k})
    return [{"kind": "drift", "count": drift.get("count"), "detail": drift.get("detail"),
             "count_excluding_setting_spray_leaf": (drift.get("count") or 0) - len(own),
             "setting_spray_entries": own}]


async def collect(conn: Any) -> List[Any]:
    """Preconditions, GATE_OPEN, then the measure; returns printable lines (dicts, or the bare str)."""
    from services.category_path_aliases import TAXONOMY_LEAVES

    out: List[Any] = []
    if LEAF not in TAXONOMY_LEAVES:
        return [{"kind": "error", "error": f"this image's taxonomy has no {LEAF}: run it on the merge image"}]
    out.append({"kind": "precondition", "check": "image_has_leaf", "ok": True})
    async with conn.transaction(readonly=True):
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        read_only = await conn.fetchval("SHOW transaction_read_only")
        timeout = await conn.fetchval("SHOW statement_timeout")
        if read_only != "on":
            return out + [{"kind": "error", "error": f"transaction_read_only = {read_only!r}"}]
        out.append({"kind": "precondition", "check": "read_only", "ok": True})
        if timeout != STATEMENT_TIMEOUT:
            return out + [{"kind": "error", "error": f"statement_timeout = {timeout!r}"}]
        out.append({"kind": "precondition", "check": "statement_timeout", "ok": True, "value": timeout})
        out.append("GATE_OPEN")
        rows = [dict(r) for r in await conn.fetch(CANDIDATE_SQL, GAP_SPELLING, candidate_regex())]
        from services.catalog_invariant_checks import _run_taxonomy_code_vs_table_drift

        drift = await _run_taxonomy_code_vs_table_drift(_Rows(conn))
    out.append({"kind": "params", "candidates": len(rows), "statement_timeout": STATEMENT_TIMEOUT})
    return out + summarize(rows) + drift_lines(drift)


def render(lines: List[Any]) -> List[str]:
    """The printed form: text lines only (see OUTPUT), GATE_OPEN repeated at the end when it opened,
    and the closing MEASURE_DONE count."""
    out: List[str] = []
    for line in lines:
        if isinstance(line, str):
            out.append(line)
        else:
            tag = "MEASURE_KEY" if line.get("kind") == "key" else "MEASURE"
            out.append(f"{tag} {json.dumps(line, sort_keys=True, default=str)}")
    if "GATE_OPEN" in lines and not any(isinstance(l, dict) and l.get("kind") == "error" for l in lines):
        out.append("GATE_OPEN")
    out.append(f"MEASURE_DONE lines={len(out)}")
    return out


async def _main() -> int:
    import asyncpg

    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        lines: List[Any] = [{"kind": "error", "error": "DATABASE_URL is not set"}]
    else:
        lines = [{"kind": "precondition", "check": "database_url", "ok": True}]
        conn = await asyncpg.connect(url)
        try:
            lines += await collect(conn)
        finally:
            await conn.close()
    for text in render(lines):
        print(text, flush=True)
    return 2 if any(isinstance(line, dict) and line.get("kind") == "error" for line in lines) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
