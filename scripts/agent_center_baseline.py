#!/usr/bin/env python3
"""
Empirical baseline validation for the Demand Test Agent (V1.5).

Runs the V1.5 LLM probe against two known-state inputs and asserts that
the score matrix matches reality. Use this BEFORE relying on agent
output for any merchant-facing decision — it answers the question
"does this thing actually identify visibility problems, or am I just
trusting a chain of mocks?"

The two baselines:

  POSITIVE: a real, currently-live Pivota PDP URL + product info that
            should be reasonably visible/citable on the live web. We
            expect Gemini's grounded search to return cited URLs that
            include (or are a path-prefix of) the verified PDP.

  NEGATIVE: a clearly-bogus product title that no LLM has any reason
            to know about. Catches the false-positive case where the
            model just hallucinates "yes it's visible".

Each baseline runs two scan_modes:

  open_product_visibility_test     — does grounding find ANY buying
                                     path for this product?
  pivota_pdp_attribution_test      — does grounding find the verified
                                     URL (or a path-match)?

PASS criteria:
  POSITIVE.visibility_score        ≥ pos_visibility_min  (default 30)
  POSITIVE.attribution_score       ≥ pos_attribution_min (default 50)
  POSITIVE has at least one in_grounding=true url_match across runs
  NEGATIVE.visibility_score        ≤ neg_visibility_max  (default 30)

A FAIL on POSITIVE means the probe still can't recognize a real PDP
even after PR 13-17 — investigate prompt quality / grounding config.
A FAIL on NEGATIVE means the probe is producing false positives —
investigate the URL match heuristic + LLM scoring.

Usage:
  PIVOTA_AGENT_INTERNAL_API_KEY=...   \\
  PIVOTA_AGENT_INTERNAL_BASE_URL=...  \\
  python scripts/agent_center_baseline.py \\
    --merchant-id m1 \\
    --store-id s1 \\
    --verified-pivota-pdp https://pivota.io/p/abc-123 \\
    --positive-product-title "Vitamin C Tonic 50ml" \\
    --positive-product-vendor Acme \\
    --positive-product-type serum \\
    --negative-product-title "Quantum Banana Hyperdrive XYZ-9999"

PIVOTA-Agent must be configured with GEMINI_API_KEY and
PIVOTA_AGENT_CENTER_MOCK_GEMINI=false (or pass --provider gemini
explicitly) for this to be a real test. Otherwise it falls through to
the deterministic mock and PASS/FAIL means nothing.

Exit codes:
  0  — all baselines met expectations
  1  — at least one baseline failed
  2  — invalid arguments / config error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Pure logic: evaluate a probe result against expectations.
# Kept in a pure function so we can unit-test without hitting Gemini.
# ---------------------------------------------------------------------------


def _has_grounding_match(result: Dict[str, Any]) -> bool:
    """True if any run in the result has a confirmed grounding URL match."""
    for run in result.get("raw_runs") or []:
        url_match = run.get("url_match") or {}
        if url_match.get("in_grounding") is True:
            return True
    return False


def evaluate_baseline(
    *,
    label: str,
    expectation: str,  # "positive" or "negative"
    visibility_result: Dict[str, Any],
    attribution_result: Optional[Dict[str, Any]] = None,
    pos_visibility_min: int = 30,
    pos_attribution_min: int = 50,
    neg_visibility_max: int = 30,
) -> Tuple[bool, List[str]]:
    """Return (pass, failure_reasons). Empty list = PASS."""
    failures: List[str] = []
    vis_score = ((visibility_result or {}).get("scores") or {}).get("visibility_score", 0)
    att_score = (
        ((attribution_result or {}).get("scores") or {}).get("visibility_score", 0)
        if attribution_result is not None
        else None
    )

    if expectation == "positive":
        if vis_score < pos_visibility_min:
            failures.append(
                f"{label}: visibility_score={vis_score} < expected ≥ {pos_visibility_min}"
            )
        if attribution_result is not None:
            if att_score is not None and att_score < pos_attribution_min:
                failures.append(
                    f"{label}: attribution_score={att_score} < expected ≥ {pos_attribution_min}"
                )
            if not _has_grounding_match(attribution_result):
                failures.append(
                    f"{label}: no run produced url_match.in_grounding=true — "
                    "Gemini didn't actually cite the verified URL"
                )
    elif expectation == "negative":
        if vis_score > neg_visibility_max:
            failures.append(
                f"{label}: visibility_score={vis_score} > expected ≤ {neg_visibility_max} "
                "— probe is hallucinating visibility for a fake product"
            )
    else:
        failures.append(f"{label}: unknown expectation {expectation!r}")
    return (len(failures) == 0, failures)


# ---------------------------------------------------------------------------
# Probe invocation — wraps services.agent_center_llm_client so the script
# uses the same HTTP path production does.
# ---------------------------------------------------------------------------


async def _run_probe(
    *,
    scan_mode: str,
    merchant_id: str,
    store_id: str,
    context: Dict[str, Any],
    provider: str,
    max_runs: int,
) -> Dict[str, Any]:
    # Late import so the script can `--help` without a fully-configured backend env.
    from services import agent_center_llm_client as llm_client

    # Synthetic scan_target_id so the upstream's per-run keying still works
    # without touching the database.
    scan_target_id = f"baseline-{scan_mode}-{os.urandom(4).hex()}"
    return await llm_client.probe(
        scan_mode=scan_mode,
        scan_target_id=scan_target_id,
        merchant_id=merchant_id,
        store_id=store_id,
        context=context,
        provider=provider,
        max_runs=max_runs,
    )


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _format_result_summary(label: str, result: Dict[str, Any]) -> str:
    scores = result.get("scores") or {}
    vis = scores.get("visibility_score")
    echo = scores.get("attribution_echo_rate")
    runs = result.get("runs_count")
    findings = result.get("findings") or []
    cited_urls: List[str] = []
    for run in result.get("raw_runs") or []:
        cited_urls.extend(run.get("grounding_chunks") or [])
    cited_urls = list(dict.fromkeys(cited_urls))[:5]  # de-dupe, top-5
    return (
        f"  {label}\n"
        f"    runs={runs} visibility={vis} echo_rate={echo} "
        f"findings={len(findings)} aborted={result.get('aborted')}\n"
        f"    cited_urls (top 5): {cited_urls if cited_urls else '—'}"
    )


async def _main_async(args: argparse.Namespace) -> int:
    print(f"== Demand Test Agent — empirical baseline ({args.provider}) ==\n")

    # POSITIVE baseline: real PDP + real product info.
    positive_context_visibility: Dict[str, Any] = {
        "queries": [],  # let the auto generator do its thing
        "product": {
            "title": args.positive_product_title,
            "vendor": args.positive_product_vendor or None,
            "product_type": args.positive_product_type or None,
        },
    }
    positive_context_attribution: Dict[str, Any] = {
        **positive_context_visibility,
        "pivota_pdp_url": args.verified_pivota_pdp,
    }

    print("→ POSITIVE baseline (real product + verified Pivota PDP):")
    pos_vis_result = await _run_probe(
        scan_mode="open_product_visibility_test",
        merchant_id=args.merchant_id,
        store_id=args.store_id,
        context=positive_context_visibility,
        provider=args.provider,
        max_runs=args.max_runs,
    )
    print(_format_result_summary("open_product_visibility_test", pos_vis_result))
    pos_att_result = await _run_probe(
        scan_mode="pivota_pdp_attribution_test",
        merchant_id=args.merchant_id,
        store_id=args.store_id,
        context=positive_context_attribution,
        provider=args.provider,
        max_runs=args.max_runs,
    )
    print(_format_result_summary("pivota_pdp_attribution_test", pos_att_result))

    # NEGATIVE baseline: clearly-bogus product title.
    print("\n→ NEGATIVE baseline (bogus product, control for false-positive):")
    neg_context: Dict[str, Any] = {
        "queries": [],
        "product": {"title": args.negative_product_title},
    }
    neg_vis_result = await _run_probe(
        scan_mode="open_product_visibility_test",
        merchant_id=args.merchant_id,
        store_id=args.store_id,
        context=neg_context,
        provider=args.provider,
        max_runs=args.max_runs,
    )
    print(_format_result_summary("open_product_visibility_test", neg_vis_result))

    # Evaluate.
    print("\n== Verdict ==")
    pos_pass, pos_fail = evaluate_baseline(
        label="POSITIVE",
        expectation="positive",
        visibility_result=pos_vis_result,
        attribution_result=pos_att_result,
        pos_visibility_min=args.pos_visibility_min,
        pos_attribution_min=args.pos_attribution_min,
    )
    neg_pass, neg_fail = evaluate_baseline(
        label="NEGATIVE",
        expectation="negative",
        visibility_result=neg_vis_result,
        neg_visibility_max=args.neg_visibility_max,
    )
    print(f"  POSITIVE: {'PASS' if pos_pass else 'FAIL'}")
    for line in pos_fail:
        print(f"    ✗ {line}")
    print(f"  NEGATIVE: {'PASS' if neg_pass else 'FAIL'}")
    for line in neg_fail:
        print(f"    ✗ {line}")

    if args.json_output:
        print(
            "\n"
            + json.dumps(
                {
                    "provider": args.provider,
                    "positive": {
                        "visibility": pos_vis_result.get("scores"),
                        "attribution": pos_att_result.get("scores"),
                        "passed": pos_pass,
                        "failures": pos_fail,
                    },
                    "negative": {
                        "visibility": neg_vis_result.get("scores"),
                        "passed": neg_pass,
                        "failures": neg_fail,
                    },
                },
                indent=2,
                default=str,
            )
        )

    return 0 if (pos_pass and neg_pass) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Empirical baseline validation for the Demand Test Agent (V1.5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    p.add_argument("--merchant-id", required=True)
    p.add_argument("--store-id", required=True)
    p.add_argument("--verified-pivota-pdp", required=True,
                   help="Verified Pivota PDP URL for the positive baseline product")
    p.add_argument("--positive-product-title", required=True,
                   help="Real product title — should be reasonably visible on the live web")
    p.add_argument("--positive-product-vendor", default=None)
    p.add_argument("--positive-product-type", default=None,
                   help="e.g. 'serum' / 'jacket' / 'phone case'")
    p.add_argument("--negative-product-title", required=True,
                   help="Clearly-bogus product title (e.g. 'Quantum Banana Hyperdrive XYZ-9999')")
    p.add_argument("--provider", default="gemini", choices=["mock", "gemini"],
                   help="Default 'gemini'. Use 'mock' to validate the script wiring "
                        "without burning Gemini quota.")
    p.add_argument("--max-runs", type=int, default=10,
                   help="Number of Gemini calls per scan_mode. Default 10 matches the "
                        "V1.5 production default; raise for tighter signal at higher cost.")
    p.add_argument("--pos-visibility-min", type=int, default=30)
    p.add_argument("--pos-attribution-min", type=int, default=50)
    p.add_argument("--neg-visibility-max", type=int, default=30)
    p.add_argument("--json-output", action="store_true",
                   help="Print a machine-readable JSON summary after the human-readable verdict.")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level CLI catch
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
