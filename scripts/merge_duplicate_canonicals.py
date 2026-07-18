#!/usr/bin/env python3
"""Merge duplicate canonical catalog rows — move-then-retire, REVERSIBLE.

Consumes the merge_duplicate proposals a human adjudicated in the StyleKorean
HITL file (scripts/stylekorean_hitl.py emit → founder/agent sets
recommendation="merge" + a "KEEP ck_..." note). For each approved pair it
RE-HOMES the loser canonical's offers onto the winner, RE-POINTS the loser's
seeds, and RETIRES the loser reversibly (suppression_reason + suppressed_at +
sync_status='archived') — never a physical delete. Every mutation's
before-state is written to a run manifest so `revert` can undo the whole run.

Why move-then-retire (not delete): the winner is the surviving canonical the
agent serves; the loser's offers are real seller coverage that must land on
the winner's PDP (agent_pdp_view aggregates offers by product_key), and
archiving-not-deleting keeps the operation reversible (catalog_offers has no
FKs; the serving gate honors catalog_products.sync_status != 'live' via the
index-pipeline 'not_live' blocker + offer removal → 'no_price').

WINNER SELECTION is defense-in-depth: the KEEP content_key parsed from the
adjudicator's note is the decision, and it MUST agree with scope rank
(multi_merchant_canonical > merchant_owned). A note that keeps the
lower-scope row over a higher-scope sibling is a red flag → the pair is
SKIPPED, never guessed (a wrong merge collapses two real products).

    # 1. dry-run: re-derive from live DB, classify every offer, write a plan
    DATABASE_URL=... python3 scripts/merge_duplicate_canonicals.py propose \
        --reviewed hitl_review.jsonl --out merge_plan.json

    # 2. apply the plan (per-pair transaction; writes a reversal manifest)
    DATABASE_URL=... python3 scripts/merge_duplicate_canonicals.py apply \
        --plan merge_plan.json --manifest merge_run.json

    # 3. undo an entire run
    DATABASE_URL=... python3 scripts/merge_duplicate_canonicals.py revert \
        --manifest merge_run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from scripts.attach_retailer_offer import _offer_id  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
)
from services.index_pipeline_state_service import (  # noqa: E402
    recompute_serving_eligibility,
)
from services.retailer_ingest.single_writer_lock import (  # noqa: E402
    SingleWriterLockError,
    retailer_ingest_lock,
)

MERGE_REASON = "duplicate_canonical_merge_v1"
_KEEP_RE = re.compile(r"KEEP\s+(ck_[0-9a-f]+)", re.I)
_SCOPE_RANK = {"multi_merchant_canonical": 2, "merchant_owned": 1}
_RETAILER_OFFER_PREFIX = "offer:retailer:"


def _scope_rank(scope: Optional[str]) -> int:
    return _SCOPE_RANK.get(str(scope or ""), 0)


def resolve_winner_loser(row: Dict[str, Any]) -> Tuple[Optional[Dict], Optional[Dict], str]:
    """(winner, loser, note). winner/loser None when the pair must be SKIPPED.

    The keep is the ck_ named in the adjudicator's review_note; it must be one
    of the two rows AND its scope rank must be >= the sibling's (never retire a
    higher-scope canonical in favor of a lower-scope one)."""
    rows = row.get("rows") or []
    if len(rows) != 2:
        return None, None, f"expected 2 rows, got {len(rows)}"
    m = _KEEP_RE.search(str(row.get("review_note") or ""))
    if not m:
        return None, None, "no 'KEEP ck_...' in review_note"
    keep_ck = m.group(1)
    winner = next((r for r in rows if str(r.get("content_key")) == keep_ck), None)
    if winner is None:
        return None, None, f"KEEP {keep_ck} matches neither row"
    loser = next(r for r in rows if r is not winner)
    if str(loser.get("content_key")) == str(winner.get("content_key")):
        return None, None, "both rows share one content_key (already merged?)"
    if _scope_rank(winner.get("pdp_scope")) < _scope_rank(loser.get("pdp_scope")):
        return None, None, (f"KEEP is lower scope ({winner.get('pdp_scope')}) than loser "
                            f"({loser.get('pdp_scope')}) — refusing to retire the stronger row")
    return winner, loser, "ok"


async def _live_product(product_key: str) -> Optional[Dict[str, Any]]:
    r = await database.fetch_one(
        "SELECT product_key, content_key, brand, title, pdp_scope, sync_status, "
        "suppression_reason, suppressed_at, suppression_metadata "
        "FROM catalog_products WHERE product_key = :pk",
        {"pk": product_key},
    )
    return dict(r) if r else None


async def _active_offers(product_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        "SELECT offer_id, product_key, merchant_id, list_price, availability, "
        "sku_key, source_system FROM catalog_offers "
        "WHERE product_key = :pk AND suppressed_at IS NULL",
        {"pk": product_key},
    )
    return [dict(r) for r in rows]


async def _winner_offers(winner_pk: str) -> Dict[str, Any]:
    """Winner offer facts: active merchant_ids, ALL offer_ids (incl suppressed),
    suppressed offer_id→merchant map, and the canonical sku_key it uses."""
    rows = [dict(r) for r in await database.fetch_all(
        "SELECT offer_id, merchant_id, suppressed_at, sku_key FROM catalog_offers "
        "WHERE product_key = :pk", {"pk": winner_pk})]
    active_merchants = {r["merchant_id"] for r in rows if r["suppressed_at"] is None}
    all_ids = {r["offer_id"] for r in rows}
    suppressed_id_merchant = {r["offer_id"]: r["merchant_id"] for r in rows if r["suppressed_at"] is not None}
    # the winner's real canonical sku_key (a merchant-owned winner may not use
    # the '<pk>::canonical' scheme); fall back to it only when no offer exists.
    sku_key = next((r["sku_key"] for r in rows if r.get("sku_key")), winner_pk + "::canonical")
    return {"active_merchants": active_merchants, "all_ids": all_ids,
            "suppressed_id_merchant": suppressed_id_merchant, "sku_key": sku_key}


def _target_offer_id(offer: Dict[str, Any], winner_pk: str) -> str:
    """Winner-homed offer_id. Retailer offers re-key to the deterministic
    retailer scheme under the winner; other offers keep their (globally unique)
    id and just change product_key."""
    if str(offer.get("offer_id") or "").startswith(_RETAILER_OFFER_PREFIX):
        return _offer_id(winner_pk, offer["merchant_id"])
    return offer["offer_id"]


async def _classify_offers(winner_pk: str, loser_pk: str) -> Dict[str, List[Dict]]:
    """Split loser's active offers into move / suppress-as-redundant /
    reactivate-winner. A PDP must never show two active offers from one seller,
    and no seller's live coverage is silently dropped."""
    wf = await _winner_offers(winner_pk)
    active_merchants = set(wf["active_merchants"])
    all_winner_ids = set(wf["all_ids"])
    suppressed_id_merchant = wf["suppressed_id_merchant"]
    winner_sku = wf["sku_key"]
    to_move: List[Dict] = []
    to_suppress: List[Dict] = []
    to_reactivate: List[Dict] = []
    for off in await _active_offers(loser_pk):
        tid = _target_offer_id(off, winner_pk)
        if off["merchant_id"] in active_merchants:
            # winner already carries this merchant live → its offer wins.
            to_suppress.append({**off, "_reason": "winner already covers merchant"})
        elif tid in suppressed_id_merchant:
            # winner has a SUPPRESSED offer at this id → reactivate the winner's
            # (restores M's live coverage) and retire the loser's, rather than
            # silently dropping merchant M (a10... NIT #5).
            to_reactivate.append({"offer_id": tid, "merchant_id": off["merchant_id"]})
            to_suppress.append({**off, "_reason": "reactivated winner's suppressed offer"})
            active_merchants.add(off["merchant_id"])
        elif tid in all_winner_ids:
            # some other id collision → suppress loser's (avoid PK clash).
            to_suppress.append({**off, "_reason": "target offer_id already on winner"})
        else:
            to_move.append({**off, "_target_offer_id": tid, "_target_sku_key": winner_sku})
            all_winner_ids.add(tid)
            active_merchants.add(off["merchant_id"])
    return {"move": to_move, "suppress": to_suppress, "reactivate": to_reactivate}


async def _loser_seed_ids(loser_pk: str) -> List[str]:
    rows = await database.fetch_all(
        "SELECT id FROM external_product_seeds WHERE attached_product_key = :pk",
        {"pk": loser_pk},
    )
    return [dict(r)["id"] for r in rows]


# --- propose (dry) ------------------------------------------------------------

async def _propose(args: argparse.Namespace) -> int:
    reviewed = [json.loads(l) for l in open(args.reviewed) if l.strip()]
    merges = [r for r in reviewed
              if r.get("kind") == "merge_duplicate" and r.get("recommendation") == "merge"]
    print(f"[propose] {len(merges)} merge-recommended pairs "
          f"(of {sum(1 for r in reviewed if r.get('kind')=='merge_duplicate')} merge rows)")
    await database.connect()
    plan: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    try:
        for row in merges:
            winner, loser, note = resolve_winner_loser(row)
            if winner is None:
                skipped.append({"match_key": row.get("match_key"), "reason": note})
                print(f"  SKIP  {row.get('match_key','?')[:48]:50s} {note}")
                continue
            wl = await _live_product(winner["product_key"])
            ll = await _live_product(loser["product_key"])
            if not wl or not ll:
                skipped.append({"match_key": row.get("match_key"),
                                "reason": "winner or loser product_key absent from live catalog"})
                print(f"  SKIP  {row.get('match_key','?')[:48]:50s} product absent (moved/merged?)")
                continue
            if wl["content_key"] == ll["content_key"]:
                skipped.append({"match_key": row.get("match_key"),
                                "reason": "already share content_key (merged since emit)"})
                print(f"  SKIP  {row.get('match_key','?')[:48]:50s} already merged")
                continue
            if wl["sync_status"] == "archived" or wl["suppression_reason"]:
                skipped.append({"match_key": row.get("match_key"),
                                "reason": f"winner is retired (sync_status={wl['sync_status']})"})
                print(f"  SKIP  {row.get('match_key','?')[:48]:50s} winner retired")
                continue
            offers = await _classify_offers(wl["product_key"], ll["product_key"])
            seeds = await _loser_seed_ids(ll["product_key"])
            entry = {
                "match_key": row.get("match_key"),
                "winner": {k: wl[k] for k in ("product_key", "content_key", "title", "pdp_scope")},
                "loser": {k: ll[k] for k in ("product_key", "content_key", "title", "pdp_scope")},
                "offers_move": [{"offer_id": o["offer_id"], "merchant_id": o["merchant_id"],
                                 "target_offer_id": o["_target_offer_id"]} for o in offers["move"]],
                "offers_suppress": [{"offer_id": o["offer_id"], "merchant_id": o["merchant_id"]}
                                    for o in offers["suppress"]],
                "offers_reactivate": offers["reactivate"],
                "seed_ids": seeds,
            }
            plan.append(entry)
            print(f"  MERGE {row.get('match_key','?')[:46]:48s} "
                  f"keep={wl['product_key'][-12:]} retire={ll['product_key'][-12:]} "
                  f"move={len(offers['move'])} suppress={len(offers['suppress'])} "
                  f"reactivate={len(offers['reactivate'])} seeds={len(seeds)}")
    finally:
        await database.disconnect()
    out = {"reason": MERGE_REASON, "pairs": plan, "skipped": skipped}
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[propose] wrote {len(plan)} actionable pairs ({len(skipped)} skipped) -> {args.out}")
    print('      review it, then: apply --plan {} --manifest <run>.json'.format(args.out))
    return 0


# --- apply --------------------------------------------------------------------

async def _apply_one(entry: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """One merge in a single transaction. Returns the reversal ledger entry.
    Re-verifies live state INSIDE the txn; raises to abort (and roll back) if
    the pair drifted from the plan."""
    wpk = entry["winner"]["product_key"]
    lpk = entry["loser"]["product_key"]
    ledger: Dict[str, Any] = {"match_key": entry["match_key"], "winner_pk": wpk,
                              "loser_pk": lpk, "moved": [], "suppressed": [],
                              "reactivated": [], "seeds": [], "loser_before": {}}

    async with database.transaction():
        wl = await _live_product(wpk)
        ll = await _live_product(lpk)
        if not wl or not ll or wl["content_key"] == ll["content_key"]:
            raise RuntimeError(f"pair drifted since plan (winner/loser absent or already merged): {entry['match_key']}")
        if wl["sync_status"] == "archived" or wl["suppression_reason"]:
            # winner retired since plan (e.g. an earlier chained pair) — never
            # home real offers onto a dead canonical.
            raise RuntimeError(f"winner retired since plan (sync_status={wl['sync_status']}): {entry['match_key']}")
        # re-classify live (offers may have moved since propose)
        offers = await _classify_offers(wpk, lpk)
        winner_sku = (await _winner_offers(wpk))["sku_key"]

        for off in offers["move"]:
            tid = off["_target_offer_id"]
            await database.execute(
                "UPDATE catalog_offers SET product_key = :wpk, sku_key = :sku, "
                "offer_id = :tid, updated_at = NOW() WHERE offer_id = :old",
                {"wpk": wpk, "sku": winner_sku, "tid": tid, "old": off["offer_id"]},
            )
            ledger["moved"].append({"new_offer_id": tid, "old_offer_id": off["offer_id"],
                                    "from_pk": lpk, "from_sku": off.get("sku_key")})

        # reactivate winner's suppressed offer for a merchant the loser covers
        # live (restores seller coverage instead of dropping it).
        for r in offers["reactivate"]:
            await database.execute(
                "UPDATE catalog_offers SET suppressed_at = NULL, suppression_reason = NULL, "
                "updated_at = NOW() WHERE offer_id = :oid",
                {"oid": r["offer_id"]},
            )
            ledger["reactivated"].append({"offer_id": r["offer_id"]})

        for off in offers["suppress"]:
            await database.execute(
                "UPDATE catalog_offers SET suppressed_at = NOW(), "
                "suppression_reason = :reason, "
                "suppression_metadata = CAST(:meta AS jsonb) WHERE offer_id = :oid "
                "AND suppressed_at IS NULL",
                {"reason": MERGE_REASON, "oid": off["offer_id"],
                 "meta": json.dumps({"run_id": run_id, "winner_product_key": wpk})},
            )
            ledger["suppressed"].append({"offer_id": off["offer_id"]})

        # re-point loser seeds to the winner (preserve bootstrap content)
        seed_ids = await _loser_seed_ids(lpk)
        for sid in seed_ids:
            await database.execute(
                "UPDATE external_product_seeds SET attached_product_key = :wpk, "
                "updated_at = NOW() WHERE id = :sid",
                {"wpk": wpk, "sid": sid},
            )
        ledger["seeds"] = seed_ids

        # retire the loser canonical (reversible) + suppress its leftover skus.
        # capture the FULL prior suppression state so revert restores it exactly.
        _meta = ll.get("suppression_metadata")
        ledger["loser_before"] = {
            "sync_status": ll["sync_status"],
            "suppression_reason": ll["suppression_reason"],
            "suppressed_at": ll["suppressed_at"].isoformat() if ll.get("suppressed_at") else None,
            "suppression_metadata": (_meta if isinstance(_meta, str) else
                                     (json.dumps(_meta) if _meta is not None else None)),
        }
        await database.execute(
            "UPDATE catalog_products SET sync_status = 'archived', "
            "suppression_reason = :reason, suppressed_at = NOW(), "
            "suppression_metadata = CAST(:meta AS jsonb), updated_at = NOW() "
            "WHERE product_key = :lpk",
            {"reason": MERGE_REASON, "lpk": lpk,
             "meta": json.dumps({"run_id": run_id, "winner_product_key": wpk})},
        )
        await database.execute(
            "UPDATE catalog_skus SET suppressed_at = NOW(), suppression_reason = :reason "
            "WHERE product_key = :lpk AND suppressed_at IS NULL",
            {"reason": MERGE_REASON, "lpk": lpk},
        )
    # AFTER commit: refresh views + recompute the serving gate. The winner picks
    # up moved offers; the loser MUST have serving_eligible recomputed or its
    # (undeleted, now-offerless) view row lingers served with no_price until the
    # nightly tick (32792ab7 review SHOULD-FIX #1 — the assembler refresh is
    # pure of eligibility, so the IPS recompute is load-bearing here).
    await _refresh_and_recompute(entry["winner"]["content_key"], entry["loser"]["content_key"])
    return ledger


async def _refresh_and_recompute(*content_keys: str) -> None:
    for ck in content_keys:
        if not ck:
            continue
        try:
            await refresh_agent_pdp_view_for_content_key(ck, refresh_source=MERGE_REASON)
        except Exception as exc:  # noqa: BLE001 — mutations already committed; log only
            print(f"    WARN view refresh failed for {ck}: {str(exc)[:120]}")
        try:
            await recompute_serving_eligibility(ck, reason=MERGE_REASON)
        except Exception as exc:  # noqa: BLE001
            print(f"    WARN serving recompute failed for {ck}: {str(exc)[:120]}")


async def _apply(args: argparse.Namespace) -> int:
    plan = json.load(open(args.plan))
    pairs = plan.get("pairs") or []
    # deterministic run_id from the manifest path (no clock — reproducible)
    run_id = "merge_" + re.sub(r"[^a-z0-9]+", "", os.path.basename(args.manifest).lower())[:24]
    print(f"[apply] {len(pairs)} pairs, run_id={run_id}")
    await database.connect()
    ledgers: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    try:
        async with retailer_ingest_lock(database):
            for entry in pairs:
                try:
                    led = await _apply_one(entry, run_id)
                    ledgers.append(led)
                    print(f"  MERGED {entry['match_key'][:46]:48s} "
                          f"moved={len(led['moved'])} suppressed={len(led['suppressed'])} "
                          f"reactivated={len(led['reactivated'])} seeds={len(led['seeds'])}")
                except Exception as exc:  # noqa: BLE001 — one pair rolls back alone
                    failed.append({"match_key": entry.get("match_key"), "error": str(exc)[:200]})
                    print(f"  FAIL   {entry.get('match_key','?')[:46]:48s} {str(exc)[:120]}")
    except SingleWriterLockError as exc:
        print(f"[lock] {exc} — exiting without further writes.")
    finally:
        await database.disconnect()
    manifest = {"run_id": run_id, "reason": MERGE_REASON, "ledgers": ledgers, "failed": failed}
    with open(args.manifest, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"[apply] {len(ledgers)} merged, {len(failed)} failed -> manifest {args.manifest}")
    return 0 if not failed else 1


# --- revert -------------------------------------------------------------------

async def _revert(args: argparse.Namespace) -> int:
    manifest = json.load(open(args.manifest))
    ledgers = manifest.get("ledgers") or []
    run_id = manifest.get("run_id")
    print(f"[revert] undoing run_id={run_id}: {len(ledgers)} merges")
    await database.connect()
    cks: set = set()
    try:
        async with retailer_ingest_lock(database):
            for led in ledgers:
                async with database.transaction():
                    lpk = led["loser_pk"]
                    # move offers back
                    for mv in led["moved"]:
                        await database.execute(
                            "UPDATE catalog_offers SET product_key = :lpk, sku_key = :sku, "
                            "offer_id = :old, updated_at = NOW() WHERE offer_id = :new",
                            {"lpk": lpk, "sku": mv.get("from_sku") or (lpk + "::canonical"),
                             "old": mv["old_offer_id"], "new": mv["new_offer_id"]},
                        )
                    # un-suppress redundant offers — GUARDED on our reason so a
                    # concurrent writer's suppression for a different reason is
                    # not wrongly cleared (matches the sku un-suppress guard).
                    for sp in led["suppressed"]:
                        await database.execute(
                            "UPDATE catalog_offers SET suppressed_at = NULL, "
                            "suppression_reason = NULL, suppression_metadata = NULL "
                            "WHERE offer_id = :oid AND suppression_reason = :reason",
                            {"oid": sp["offer_id"], "reason": MERGE_REASON},
                        )
                    # re-suppress the winner offers we reactivated (restore state)
                    for rc in led.get("reactivated", []):
                        await database.execute(
                            "UPDATE catalog_offers SET suppressed_at = NOW() "
                            "WHERE offer_id = :oid",
                            {"oid": rc["offer_id"]},
                        )
                    # re-point seeds back
                    for sid in led["seeds"]:
                        await database.execute(
                            "UPDATE external_product_seeds SET attached_product_key = :lpk, "
                            "updated_at = NOW() WHERE id = :sid",
                            {"lpk": lpk, "sid": sid},
                        )
                    # un-retire the loser — restore the EXACT prior suppression
                    # state (sync_status, reason, timestamp, metadata).
                    before = led.get("loser_before") or {}
                    await database.execute(
                        "UPDATE catalog_products SET sync_status = :ss, "
                        "suppression_reason = :sr, "
                        "suppressed_at = CAST(:sat AS timestamptz), "
                        "suppression_metadata = CAST(:smeta AS jsonb), updated_at = NOW() "
                        "WHERE product_key = :lpk",
                        {"ss": before.get("sync_status") or "live",
                         "sr": before.get("suppression_reason"),
                         "sat": before.get("suppressed_at"),
                         "smeta": before.get("suppression_metadata"), "lpk": lpk},
                    )
                    await database.execute(
                        "UPDATE catalog_skus SET suppressed_at = NULL, suppression_reason = NULL "
                        "WHERE product_key = :lpk AND suppression_reason = :reason",
                        {"lpk": lpk, "reason": MERGE_REASON},
                    )
                cks.add(led["winner_pk"])
                cks.update([led["loser_pk"]])
                print(f"  REVERTED {led['match_key'][:52]}")
            # refresh views + recompute the serving gate for both content_keys
            # (the loser must be re-evaluated back to serving).
            for led in ledgers:
                for pk in (led["winner_pk"], led["loser_pk"]):
                    ck = await database.fetch_one(
                        "SELECT content_key FROM catalog_products WHERE product_key = :pk", {"pk": pk})
                    if ck and dict(ck).get("content_key"):
                        await _refresh_and_recompute(dict(ck)["content_key"])
    except SingleWriterLockError as exc:
        print(f"[lock] {exc} — exiting.")
        return 1
    finally:
        await database.disconnect()
    print(f"[revert] done")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("propose", help="dry-run: classify + write a merge plan (no writes)")
    pr.add_argument("--reviewed", required=True, help="adjudicated HITL JSONL")
    pr.add_argument("--out", required=True, help="merge plan JSON output")
    ap = sub.add_parser("apply", help="apply a merge plan (per-pair txn; writes manifest)")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--manifest", required=True, help="reversal-ledger output")
    rv = sub.add_parser("revert", help="undo a run from its manifest")
    rv.add_argument("--manifest", required=True)
    args = p.parse_args()
    driver = {"propose": _propose, "apply": _apply, "revert": _revert}[args.cmd]
    return asyncio.run(driver(args))


if __name__ == "__main__":
    raise SystemExit(main())
