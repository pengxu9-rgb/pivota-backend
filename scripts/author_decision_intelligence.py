#!/usr/bin/env python3
"""Decision-intelligence lane — author the structured DECISION DOSSIER fields for
the serving brand-official cohort, then publish them to the served PDP.

THE GAP (2026-07-19 prod census): agent_pdp_view carries bullet_points /
usage_scenarios / evidence_profile / required_disclaimers, but across the ~1,132
serving brand-official canonicals they are ~0% populated. We serve a structured
CATALOG PDP but not a structured DECISION DOSSIER (ADR-002). This runner closes
that gap over the cohort by ORCHESTRATING existing writers — it does not
reinvent them:

  bullet_points / usage_scenarios
      -> services.decision_intelligence.author_decision_copy (NEW): a grounded
         DeepSeek closed-context generator + a composed anti-fabrication gate
         (polarity guard, contiguous-span, efficacy->substantiated-evidence) ->
         db.product_enrichment.upsert_enrichment written to the CONTENT_KEY's
         pick_canonical winner (see S1 note) — the exact overlay
         assemble_row._fetch_enrichment_for_canonical reads.

  evidence_profile / required_disclaimers / actives / concerns
      -> services.beauty_enrichment_persist.enrich_and_persist_product (EXISTING,
         non-LLM, INCI-substantiated): the canonical evidence writer to
         beauty_product_profiles. Degrades gracefully — no INCI -> no
         substantiated claim, honestly empty. It also refreshes the view +
         recomputes serving when it writes (so step 3 below is gated to avoid a
         double refresh).

  publish
      -> services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key
         once per content_key, so both writes reach the served PDP + the
         serving-eligibility gate.

Processing is CONTENT_KEY-driven, not row-driven: assemble_row serves ONE row
per content_key (pick_canonical), and reads that winner's enrichment overlay. A
prior version keyed the overlay to whatever cohort row it fetched — if that
wasn't the canonical winner the bullets were written but NEVER surfaced (silent
no-serve), and a cluster could fire N model calls. This runner dedupes to one
call per content_key and writes to the winner's identity triple.

Hardening mirrors scripts/promote_brand_official_canonicals.py: dry-run default,
--apply to write, --pks / --limit selection, keyset pagination, per-content_key
failure isolation, transient-proxy reconnect (DB_POOL via db.database).
Idempotent: fill-only (cohort filters av.bullet_points IS NULL; the evidence
writer is itself fill-only). DO NOT run against prod without sign-off.

    DATABASE_URL=... python3 scripts/author_decision_intelligence.py            # dry-run
    DATABASE_URL=... python3 scripts/author_decision_intelligence.py --limit 50 --apply
    DATABASE_URL=... python3 scripts/author_decision_intelligence.py --pks pk1,pk2 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from db.product_enrichment import upsert_enrichment  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    fetch_products_for_key,
    pick_canonical,
    refresh_agent_pdp_view_for_content_key,
)
from services.beauty_enrichment import extract_key_actives  # noqa: E402
from services.beauty_enrichment_persist import (  # noqa: E402
    _inci_substantiated_claims,
    enrich_and_persist_product,
)
from services.beauty_evidence import derive_substantiated_claims  # noqa: E402
from services.catalog_enrichment_agent.bulk_writer import is_transport_error  # noqa: E402
from services.decision_intelligence import DecisionCopy, author_decision_copy  # noqa: E402

REASON = "decision_intelligence_lane_v1"
PAGE_SIZE = 150

# Cohort: DISTINCT content_keys of serving brand-official canonicals whose served
# PDP has NO bullet_points yet. "Serving" = a live agent_pdp_view row the gate
# cleared (index_pipeline_state.serving_eligible). "Brand-official" = the
# enrichment-minted source_system the promotion lane targets. Keyset-paginated on
# content_key (small pages survive the ~530ms public-proxy weather).
_SELECT_COHORT_PAGE = """
    SELECT DISTINCT p.content_key
    FROM catalog_products p
    JOIN agent_pdp_view av ON av.content_key = p.content_key
    JOIN index_pipeline_state i ON i.content_key = p.content_key
                               AND i.serving_eligible = TRUE
    WHERE p.source_system = 'catalog_enrichment_agent_v1'
      AND p.sync_status = 'live'
      AND p.content_key IS NOT NULL
      AND av.bullet_points IS NULL
      AND p.content_key > :after
    ORDER BY p.content_key
    LIMIT :page
"""

# Explicit product_key set -> their DISTINCT content_keys (a human asked for
# exactly these; no serving/minted filter).
_SELECT_PK_CKS = """
    SELECT DISTINCT p.content_key
    FROM catalog_products p
    WHERE p.product_key = ANY(:pks)
      AND p.sync_status = 'live'
      AND p.content_key IS NOT NULL
"""

# INCI for the canonical winner (sku_key = product_key || '::canonical').
_SELECT_INCI = """
    SELECT raw_inci, active_ingredients_json
    FROM beauty_sku_ingredients
    WHERE sku_key = :sk
    LIMIT 1
"""


# --- DB resilience (mirrors promote_brand_official_canonicals.py) -----------

def _should_heal(exc: BaseException) -> bool:
    return is_transport_error(exc) or "already acquired" in str(exc)


async def _reconnect() -> None:
    try:
        await database.disconnect()
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(1, 6):
        try:
            await database.connect()
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  (reconnect attempt {attempt} failed: {str(exc)[:100]})")
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("could not re-establish DB connection")


async def _fetch_all_hardened(sql: str, values: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    for attempt in range(1, 5):
        try:
            return [dict(r) for r in await database.fetch_all(sql, values)]
        except Exception as exc:  # noqa: BLE001
            if not _should_heal(exc) or attempt == 4:
                raise
            print(f"  (fetch transport error, attempt {attempt}: {str(exc)[:100]} — reconnecting)")
            await _reconnect()
    return []


async def _fetch_cohort(limit: int) -> List[str]:
    cks: List[str] = []
    after = ""
    while True:
        page = await _fetch_all_hardened(_SELECT_COHORT_PAGE, {"after": after, "page": PAGE_SIZE})
        cks.extend(str(r["content_key"]) for r in page)
        if len(page) < PAGE_SIZE or (limit and len(cks) >= limit):
            break
        after = cks[-1]
    return cks[:limit] if limit else cks


# --- per-content_key lane ---------------------------------------------------

async def _prepare(ck: str) -> Optional[Dict[str, Any]]:
    """Resolve the content_key to its served canonical (pick_canonical) + inputs.
    Returns None when there's no servable canonical (e.g. only audit seeds)."""
    products = await fetch_products_for_key(ck, db=database)
    if not products:
        return None
    canonical = pick_canonical(products)
    if not all((canonical.get("merchant_id"), canonical.get("platform"),
                canonical.get("source_product_id"))):
        return None
    inci = await database.fetch_one(
        _SELECT_INCI, {"sk": f"{canonical.get('product_key')}::canonical"}
    )
    raw_inci = inci["raw_inci"] if inci else None
    stored_actives = (inci["active_ingredients_json"] if inci else None) or None
    title = canonical.get("title")
    description = canonical.get("description")
    category_kind = canonical.get("category_kind")
    # Actives: prefer the persisted INCI-verified set; else derive (same engine
    # the evidence writer uses) so grounding + substantiation are consistent.
    actives = stored_actives or extract_key_actives(
        raw_inci, fallback_text=" ".join(s for s in (title, description) if s)
    )
    # Graded, provenance-backed claims — the ONLY efficacy the generator may
    # state (ADR-002). derive_substantiated_claims = benefit<-active<-text;
    # _inci_substantiated_claims = INCI ingredient-presence. Both are
    # substantiated; their union is the efficacy allow-list.
    subs = [
        c.claim_text for c in derive_substantiated_claims(
            category_kind, actives, title, description
        )
    ]
    subs += [c["claim_text"] for c in _inci_substantiated_claims(actives)]
    return {
        "content_key": ck,
        "canonical": canonical,
        "raw_inci": raw_inci,
        "actives": actives,
        "substantiated_claims": subs,
    }


async def _author_copy(ctx_row: Dict[str, Any]) -> DecisionCopy:
    c = ctx_row["canonical"]
    return await author_decision_copy(
        title=c.get("title"),
        description=c.get("description"),
        brand=c.get("brand"),
        category_path=c.get("category") or c.get("category_kind"),
        tags=c.get("tags"),
        actives=ctx_row["actives"],
        raw_inci=ctx_row["raw_inci"],
        substantiated_claims=ctx_row["substantiated_claims"],
    )


async def _apply_ck(ck: str) -> Dict[str, Any]:
    """Author + persist + publish one content_key's dossier. Isolated: any
    failure is captured and returned, never raised."""
    out: Dict[str, Any] = {"content_key": ck}
    prep = await _prepare(ck)
    if prep is None:
        out["skip"] = "no_servable_canonical"
        return out
    canonical = prep["canonical"]
    out["product_key"] = canonical.get("product_key")

    # 1. Copy -> product_enrichment keyed to the pick_canonical WINNER (S1), the
    #    exact overlay assemble_row reads. Only write a publishable, grounded set.
    enrich_refreshed = False
    for attempt in (1, 2):
        try:
            copy = await _author_copy(prep)
            out["bullets"] = len(copy.bullet_points)
            out["scenarios"] = len(copy.usage_scenarios)
            out["dropped"] = len(copy.dropped)
            if copy.is_publishable():
                await upsert_enrichment(
                    str(canonical["merchant_id"]),
                    str(canonical["platform"]),
                    str(canonical["source_product_id"]),
                    "default",
                    {
                        "bullet_points": copy.bullet_points,
                        "usage_scenarios": copy.usage_scenarios or None,
                        "llm_safety_flags": {"auto_generated_by": REASON},
                    },
                )
                out["copy_written"] = True
            else:
                out["copy_written"] = False
                out["copy_skip_reason"] = "not_publishable" if copy.generated else "no_generation"
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and _should_heal(exc):
                await _reconnect()
                continue
            out["copy_error"] = str(exc)[:160]
            break

    # 2. Evidence side -> beauty_product_profiles via the EXISTING writer
    #    (idempotent, fill-only, non-LLM). It refreshes the view + recomputes
    #    serving WHEN it writes anything — capture that so step 3 doesn't double.
    for attempt in (1, 2):
        try:
            ev = await enrich_and_persist_product(canonical["product_key"], dry_run=False)
            written = ev.get("written") or {}
            out["evidence"] = ev.get("status")
            out["evidence_written"] = bool(written.get("evidence_claims"))
            enrich_refreshed = bool(
                written.get("concerns") or written.get("actives_skus") or written.get("evidence_claims")
            )
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and _should_heal(exc):
                await _reconnect()
                continue
            out["evidence_error"] = str(exc)[:160]
            break

    # 3. Publish: refresh the served view so the copy write reaches the PDP.
    #    S3: skip when the evidence writer already refreshed this content_key
    #    (its refresh already re-read the fresh product_enrichment overlay).
    if out.get("copy_written") and not enrich_refreshed:
        for attempt in (1, 2):
            try:
                out["published"] = await refresh_agent_pdp_view_for_content_key(
                    ck, refresh_source=REASON
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1 and _should_heal(exc):
                    await _reconnect()
                    continue
                out["publish_error"] = str(exc)[:160]
                break
    elif enrich_refreshed:
        out["published"] = True  # evidence writer's refresh published the copy too
    return out


# --- entrypoint -------------------------------------------------------------

async def run(*, pks: Optional[List[str]], limit: int, apply: bool) -> int:
    await _reconnect()  # initial connect through the healer (proxy TLS bad-window)

    if pks:
        rows = await _fetch_all_hardened(_SELECT_PK_CKS, {"pks": pks})
        cks = [str(r["content_key"]) for r in rows]
        print(f"[cohort] {len(pks)} pks -> {len(cks)} distinct content_keys (apply={apply})")
    else:
        cks = await _fetch_cohort(limit)
        print(f"[cohort] {len(cks)} serving brand-official content_keys missing "
              f"bullet_points (apply={apply})")

    if not apply:
        dist: Counter = Counter()
        # Bounded probe so a dry-run doesn't fire the model over the whole cohort.
        for ck in cks[: min(len(cks), 20)]:
            try:
                prep = await _prepare(ck)
                if prep is None:
                    dist["no_servable_canonical"] += 1
                    continue
                copy = await _author_copy(prep)
            except Exception as exc:  # noqa: BLE001
                dist["author_error"] += 1
                print(f"  WARN author failed ck={ck}: {str(exc)[:120]}")
                continue
            dist["publishable" if copy.is_publishable() else "not_publishable"] += 1
            dist["dropped_ungrounded_lines"] += len(copy.dropped)
            dist["has_substantiated"] += 1 if prep["substantiated_claims"] else 0
        print(f"[dry] probed {min(len(cks), 20)}/{len(cks)}: {dict(dist)}")
        print("[dry] no writes. Re-run with --apply.")
        await database.disconnect()
        return 0

    written = Counter()
    for i, ck in enumerate(cks, 1):
        res = await _apply_ck(ck)
        written["copy_written"] += 1 if res.get("copy_written") else 0
        written["evidence_written"] += 1 if res.get("evidence_written") else 0
        written["published"] += 1 if res.get("published") else 0
        for k in ("copy_error", "evidence_error", "publish_error"):
            if res.get(k):
                written[k] += 1
                print(f"  WARN {k} ck={ck}: {res[k]}")
        if i % 25 == 0:
            print(f"  [apply] {i}/{len(cks)} (copy={written['copy_written']} "
                  f"evidence={written['evidence_written']})")

    print(f"[applied] content_keys={len(cks)} copy_written={written['copy_written']} "
          f"evidence_written={written['evidence_written']} published={written['published']} "
          f"errors={{copy:{written['copy_error']}, evidence:{written['evidence_error']}, "
          f"publish:{written['publish_error']}}}")
    await database.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--pks", help="comma-separated product_keys (explicit set)")
    p.add_argument("--limit", type=int, default=0, help="cap cohort size (0 = all)")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = p.parse_args()
    pks = [s.strip() for s in (args.pks or "").split(",") if s.strip()] or None
    return asyncio.run(run(pks=pks, limit=args.limit, apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
