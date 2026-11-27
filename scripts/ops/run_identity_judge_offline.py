"""Run the live judge (services/identity_live_judge.py) OFFLINE over the identity truth set and
score it (services/identity_truth_set.score). OFFLINE ONLY: reads local files, writes local files,
makes no database, store, platform or model call.

Agents: "rules" (the deterministic stand-ins; no model) or "replay:FILE" (answers recorded
elsewhere, JSON lines {pair_id, role, answer}). `prompts` writes the model prompts for one role so
a model run can be made separately, once the models and each source's vendor terms are settled.

Outputs hold real store and product names, so every output path must be OUTSIDE this public
repository (identity_truth_set.write_jsonl refuses otherwise). It prints counts only.

  python3 -m scripts.ops.run_identity_judge_offline run --truth DIR/truth_set.jsonl --out DIR/verdicts.jsonl
  python3 -m scripts.ops.run_identity_judge_offline run --truth T --matcher replay:DIR/answers.jsonl --sceptic replay:DIR/answers.jsonl --out V
  python3 -m scripts.ops.run_identity_judge_offline prompts --truth T --role sceptic --out DIR/prompts.jsonl

`run` exits 0 when the gate passes, 1 when it fails, 2 on a refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services import identity_live_judge as lj  # noqa: E402
from services import identity_truth_set as ts  # noqa: E402


def _print(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, sort_keys=True, ensure_ascii=False), flush=True)


def _agent(spec: str, role: str) -> lj.Agent:
    if spec == "rules":
        return lj.rules_matcher if role == "matcher" else lj.rules_sceptic
    if spec.startswith("replay:"):
        return lj.ReplayAgent(role, ts.read_jsonl(spec[len("replay:"):]))
    raise ValueError(f"unknown agent {spec!r} (rules | replay:FILE)")


def _by_basis(pairs: List[ts.TruthPair], verdicts: Dict[str, str]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for p in pairs:
        row = out.setdefault(f"{p.grade}/{p.label}/{p.basis}", {})
        v = verdicts.get(p.pair_id, "missing")
        row[v] = row.get(v, 0) + 1
    return dict(sorted(out.items()))


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--truth", required=True)
    r.add_argument("--matcher", default="rules")
    r.add_argument("--sceptic", default="rules")
    r.add_argument("--out", required=True, help="the verdicts, JSON lines (outside the repo)")
    p = sub.add_parser("prompts")
    p.add_argument("--truth", required=True)
    p.add_argument("--role", required=True, choices=lj.ROLES)
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    try:
        pairs = [ts.from_row(x) for x in ts.read_jsonl(args.truth)]
        views = [ts.judge_view(x) for x in pairs]
        if args.cmd == "prompts":
            n = ts.write_jsonl(args.out, ({"pair_id": v["pair_id"], "role": args.role,
                                           "prompt": lj.build_agent_prompt(args.role, v)} for v in views))
            _print({"judge_offline": "prompts", "role": args.role, "written": n})
            return 0
        results = lj.judge_all(views, _agent(args.matcher, "matcher"), _agent(args.sceptic, "sceptic"))
        written = ts.write_jsonl(args.out, results)
        verdicts = {str(x["pair_id"]): x["verdict"] for x in results}
        scored = ts.score(pairs, verdicts)
        _print({"judge_offline": "run", "judge": lj.JUDGE_VERSION, "matcher": args.matcher,
                "sceptic": args.sceptic, "written": written, "score": scored,
                "by_basis": _by_basis(pairs, verdicts), "reasons": lj.reason_counts(results)})
        return 0 if scored["gate"] == "pass" else 1
    except (ts.TruthSetError, OSError, ValueError, KeyError) as exc:
        _print({"judge_offline": "refused", "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
