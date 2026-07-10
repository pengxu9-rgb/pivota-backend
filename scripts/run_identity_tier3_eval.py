#!/usr/bin/env python3
"""ADR-010 D-2 Phase C — the Tier-3 judge eval gate.

Runs the judge over the gold-label fixture
(reports/step5/tier3_eval_fixture_2026-07-10.json — 254 groups labeled by
the step-5 human review) and computes the deployment gate:

  GATE: zero keep_separate-labeled groups judged 'collapse' at or above the
  confidence floor. Mis-merge is worse than fragmentation; until this is
  zero, the judge adjudicates nothing live.

Also reported: collapse coverage (how much of the mechanical work it would
have caught), keep coverage, unsure/failed rate. The full per-group verdict
list + gate summary is written next to the fixture, stamped with
judge_version, so gate results accumulate per version.

Needs GEMINI_API_KEY (run via railway, which carries it). ~250 calls; use
--limit for a smoke run.

  python3 scripts/run_identity_tier3_eval.py --limit 20
  python3 scripts/run_identity_tier3_eval.py            # full gate run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.identity_tier3_judge import (  # noqa: E402
    JUDGE_VERSION,
    eval_gate,
    judge_group,
)

FIXTURE = "reports/step5/tier3_eval_fixture_2026-07-10.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="Judge only the first N of each label (smoke run)")
    parser.add_argument("--fixture", default=FIXTURE)
    parser.add_argument("--sleep", type=float, default=0.3,
                        help="Pause between calls (rate-limit kindness)")
    args = parser.parse_args()

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("PIVOTA_GEMINI_API_KEY")):
        print("ABORT: no GEMINI_API_KEY in env — the judge never guesses offline.")
        return 1

    fixture = json.load(open(args.fixture))
    groups = fixture["groups"]
    if args.limit:
        by_label: dict = {}
        picked = []
        for g in groups:
            by_label.setdefault(g["label"], []).append(g)
        for gs in by_label.values():
            picked.extend(gs[: args.limit])
        groups = picked

    results = []
    for i, g in enumerate(groups):
        verdict = judge_group(g["rows"]) or {"verdict": None, "confidence": 0.0}
        results.append({
            "group_id": g["group_id"], "label": g["label"], "source": g["source"],
            "verdict": verdict.get("verdict"),
            "confidence": verdict.get("confidence"),
            "reasoning": verdict.get("reasoning"),
        })
        if (i + 1) % 25 == 0:
            print(f"...{i + 1}/{len(groups)}", file=sys.stderr)
        time.sleep(args.sleep)

    gate = eval_gate(results)
    out_path = args.fixture.replace(
        ".json", f"_eval_{JUDGE_VERSION.replace('.', '_')}.json")
    json.dump({"gate": gate, "results": results}, open(out_path, "w"), indent=1)
    print(json.dumps(gate, indent=2))
    print(f"full results -> {out_path}", file=sys.stderr)
    return 0 if gate["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
