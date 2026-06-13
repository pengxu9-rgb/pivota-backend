"""Behavioral (model-in-the-loop) Pivota-vs-native eval runner.

Assembles each product's live agent record (same path as the structural eval),
derives a realistic buyer query, and builds the actionability judge prompts for
BOTH the Pivota record and its native baseline (services.behavioral_eval). It then
either runs them through an injected judge LLM (--run-inline, needs a provider) or
dumps the prompts to JSON for an external judge (default) — because LLM keys are
not pulled locally; a keyed env or a Codex job executes the judging
(feedback_dispatch_codex_for_dry_runs).

Usage:
  # default: assemble + dump prompts for an external judge
  DATABASE_URL=... python -m scripts.run_behavioral_eval --product-key pk1 --out prompts.json
  # with a provider wired in-process:
  DATABASE_URL=... python -m scripts.run_behavioral_eval --product-key pk1 --run-inline
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional

from db.database import database
from services import behavioral_eval as be
from services.decision_grade_eval import native_baseline_record
# Reuse the structural runner's record assembly (payload + best US offer + alts).
from scripts.run_kbeauty_decision_grade_eval import (
    _assemble_record,
    _connect_if_needed,
    _disconnect_if_needed,
)


def _derive_query(record: Dict[str, Any]) -> str:
    """A realistic buyer query from the record's category + top concern, so the
    judge is asked something a shopper would actually ask. Honest fallback when no
    concern is known."""
    cat = (record.get("category_kind") or "skincare").replace("_", " ")
    concerns = record.get("concerns") or []
    fmt = record.get("skincare_format") or record.get("haircare_format")
    if concerns:
        base = f"recommend a {cat} {fmt or 'product'} for {concerns[0]}"
    else:
        base = f"recommend a good {cat} {fmt or 'product'}"
    return base


def _prompt_pair(query: str, record: Dict[str, Any]) -> Dict[str, str]:
    pivota_card = be.build_context_card(record)
    native_card = be.build_context_card(native_baseline_record(record))
    return {
        "pivota_prompt": be.actionability_prompt(query, pivota_card),
        "native_prompt": be.actionability_prompt(query, native_card),
    }


def _resolve_judge() -> Optional[be.JudgeFn]:
    """Wire judge_fn to an in-process provider when one is configured. Returns None
    when no provider/keys are available -> the runner falls back to dump mode."""
    # Intentionally not pulling Railway LLM keys here (feedback_dispatch_codex_for_dry_runs).
    # A keyed deployment can implement this hook against services.llm_providers.
    return None


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was_connected = await _connect_if_needed(db)
    try:
        items: List[Dict[str, Any]] = []
        for pk in args.product_keys:
            try:
                record = await _assemble_record(pk, db=db)
            except Exception as exc:  # noqa: BLE001
                items.append({"product_key": pk, "error": str(exc)[:200]})
                continue
            query = args.query or _derive_query(record)
            items.append({"product_key": pk, "query": query, "record": record})
        ok = [it for it in items if "record" in it]

        judge = _resolve_judge() if args.run_inline else None
        if judge is not None:
            agg = be.compare_behavioral_cohort(
                [{"query": it["query"], "record": it["record"]} for it in ok], judge
            )
            return {"mode": "inline", "n": len(ok), **agg}

        # Dump mode: emit the judge prompts for an external/Codex judge.
        prompts = [
            {"product_key": it["product_key"], "query": it["query"], **_prompt_pair(it["query"], it["record"])}
            for it in ok
        ]
        if args.out:
            with open(args.out, "w") as fh:
                json.dump(prompts, fh, indent=1)
        return {
            "mode": "dump_prompts",
            "n": len(prompts),
            "out": args.out,
            "note": "Run each pivota_prompt / native_prompt through a judge LLM; "
                    "feed the JSON verdicts to services.behavioral_eval.parse_actionability "
                    "and compare scores. (--run-inline once a provider is wired.)",
            "errors": [it for it in items if "error" in it],
        }
    finally:
        await _disconnect_if_needed(db, was_connected)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product-key", dest="product_keys", action="append", default=[])
    ap.add_argument("--query", default=None, help="override the auto-derived buyer query")
    ap.add_argument("--out", default=None, help="write judge prompts to this JSON file")
    ap.add_argument("--run-inline", action="store_true", help="judge in-process (needs a provider)")
    args = ap.parse_args()
    if not args.product_keys:
        ap.error("provide at least one --product-key")
    print(json.dumps(asyncio.run(_drive(args)), indent=1))


if __name__ == "__main__":
    main()
