"""Run the live judge (services/identity_live_judge.py) OFFLINE over the identity truth set and
score it (services/identity_truth_set.score). OFFLINE ONLY: reads local files and writes local
files; it makes no database, store or platform call. A model call is made only when an agent is
"gemini" (below), and then only for pairs whose source terms allow it.

Agents:
  rules            the deterministic stand-ins; no model
  replay:FILE      answers recorded elsewhere, JSON lines {pair_id, role, answer}
  gemini[:MODEL]   a Gemini model (services/identity_judge_gemini.py) through services/vertex_gemini.
                   Default models: gemini-4-hydrogen for both roles (sceptic prompt p2). Vertex needs
                   VERTEX_AI_ENABLED=true, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION and ADC;
                   otherwise GEMINI_API_KEY. Only pairs whose two listings read as Shopify listings
                   are sent (the one source whose written terms allow a model vendor), so a run with
                   a gemini agent scores ONLY those pairs (--only-shopify is implied); the count
                   left out is printed. --record FILE keeps every call (answer, finish reason,
                   tokens) so the run can be replayed with replay:FILE at no cost.
`prompts` writes the model prompts for one role.

EVALUATION ONLY: --agents-only skips the vetoes, so the agents (verifier and grounding still on)
are scored on pairs the rules would have ended; --only-vetoed keeps just those pairs. Every verdict
row then carries mode "agents_only" and the vetoes it skipped; such a run measures the agents and
is never a live verdict. "answers" prints how often each role said same, per gold label.

Outputs hold real store and product names, so every output path must be OUTSIDE this public
repository (identity_truth_set.write_jsonl refuses otherwise). It prints counts only.

  python3 -m scripts.ops.run_identity_judge_offline run --truth DIR/truth_set.jsonl --out DIR/verdicts.jsonl
  python3 -m scripts.ops.run_identity_judge_offline run --truth T --matcher replay:DIR/answers.jsonl --sceptic replay:DIR/answers.jsonl --out V
  python3 -m scripts.ops.run_identity_judge_offline run --truth T --matcher gemini --sceptic gemini --record DIR/answers.jsonl --out V
  python3 -m scripts.ops.run_identity_judge_offline prompts --truth T --role sceptic --out DIR/prompts.jsonl

`run` exits 0 when the gate passes, 1 when it fails, 2 on a refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services import identity_live_judge as lj  # noqa: E402
from services import identity_truth_set as ts  # noqa: E402


def _print(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, sort_keys=True, ensure_ascii=False), flush=True)


def _is_model(spec: str) -> bool:
    return spec == "gemini" or spec.startswith("gemini:")


def _agent(spec: str, role: str, thinking: str = "MEDIUM", post: Any = None) -> lj.Agent:
    if spec == "rules":
        return lj.rules_matcher if role == "matcher" else lj.rules_sceptic
    if spec.startswith("replay:"):
        return lj.ReplayAgent(role, ts.read_jsonl(spec[len("replay:"):]))
    if _is_model(spec):
        from services import identity_judge_gemini as jg

        default = jg.DEFAULT_MATCHER_MODEL if role == "matcher" else jg.DEFAULT_SCEPTIC_MODEL
        model = spec.split(":", 1)[1] if ":" in spec else default
        return jg.GeminiAgent(role, model, thinking=thinking, **({"post": post} if post else {}))
    raise ValueError(f"unknown agent {spec!r} (rules | replay:FILE | gemini[:MODEL])")


def _by_basis(pairs: List[ts.TruthPair], verdicts: Dict[str, str]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for p in pairs:
        row = out.setdefault(f"{p.grade}/{p.label}/{p.basis}", {})
        v = verdicts.get(p.pair_id, "missing")
        row[v] = row.get(v, 0) + 1
    return dict(sorted(out.items()))


def _answers_by_label(pairs: List[ts.TruthPair], records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, int]]]:
    """Per role and gold label: how often the agent said same, not same, or gave no answer."""
    label = {p.pair_id: p.label for p in pairs}
    out: Dict[str, Dict[str, Dict[str, int]]] = {}
    for r in records:
        lab = label.get(str(r.get("pair_id")))
        if lab is None:
            continue
        ans = r.get("answer")
        said = "no_answer" if not isinstance(ans, dict) else ("same" if ans.get("same") is True else "not_same")
        row = out.setdefault(str(r.get("role")), {}).setdefault(lab, {"same": 0, "not_same": 0, "no_answer": 0})
        row[said] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}


def main(argv: Optional[List[str]] = None, *, post: Any = None) -> int:
    """`post` stands in for the Gemini transport in tests; a real run uses services/vertex_gemini."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--truth", required=True)
    r.add_argument("--matcher", default="rules")
    r.add_argument("--sceptic", default="rules")
    r.add_argument("--out", required=True, help="the verdicts, JSON lines (outside the repo)")
    r.add_argument("--only-shopify", action="store_true",
                   help="score only pairs whose two listings read as Shopify listings (implied by a gemini agent)")
    r.add_argument("--thinking", default="MEDIUM", choices=("LOW", "MEDIUM", "HIGH"))
    r.add_argument("--record", help="gemini agents: write every call here, JSON lines (outside the repo)")
    r.add_argument("--workers", type=int, default=4, help="pairs judged at once (gemini agents)")
    r.add_argument("--max-pairs", type=int, help="score at most this many pairs (after the source filter)")
    r.add_argument("--agents-only", action="store_true",
                   help="EVALUATION ONLY: skip the vetoes so the agents are measured on vetoed pairs too")
    r.add_argument("--only-vetoed", action="store_true",
                   help="with --agents-only: keep only the pairs the rules veto")
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
        uses_model = _is_model(args.matcher) or _is_model(args.sceptic)
        if uses_model and not args.record:
            raise ValueError("a gemini agent needs --record FILE (every call is kept for replay)")
        if args.workers < 1 or (args.max_pairs is not None and args.max_pairs < 1):
            raise ValueError("--workers and --max-pairs must be at least 1")
        if args.only_vetoed and not args.agents_only:
            raise ValueError("--only-vetoed needs --agents-only (a vetoed pair never reaches an agent otherwise)")
        left_out = 0
        if uses_model or args.only_shopify:
            from services import identity_judge_gemini as jg

            keep = [i for i, v in enumerate(views) if jg.shopify_pair(v)]
            left_out = len(views) - len(keep)
            pairs, views = [pairs[i] for i in keep], [views[i] for i in keep]
        if args.only_vetoed:
            keep = [i for i, v in enumerate(views) if lj.vetoes(lj.normalise(v["a"]), lj.normalise(v["b"]))]
            pairs, views = [pairs[i] for i in keep], [views[i] for i in keep]
        if args.max_pairs is not None:
            pairs, views = pairs[:args.max_pairs], views[:args.max_pairs]
        if not views:
            raise ValueError("no pairs left to score")
        if args.record:  # refuse a repo path BEFORE any call is paid for
            ts.write_jsonl(args.record, [])
        matcher = _agent(args.matcher, "matcher", args.thinking, post)
        sceptic = _agent(args.sceptic, "sceptic", args.thinking, post)
        one = lj.judge_agents_only if args.agents_only else lj.judge
        if uses_model and args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                results = list(pool.map(lambda v: one(v, matcher, sceptic), views))
        else:
            results = lj.judge_all(views, matcher, sceptic, agents_only=args.agents_only)
        written = ts.write_jsonl(args.out, results)
        out: Dict[str, Any] = {}
        if args.record:
            records = [r for a in (matcher, sceptic) for r in getattr(a, "records", [])]
            ts.write_jsonl(args.record, records)
            from services import identity_judge_gemini as jg

            out = {"recorded": len(records), "usage": jg.usage_totals(records),
                   "calls": jg.error_summary(records), "answers": _answers_by_label(pairs, records)}
        verdicts = {str(x["pair_id"]): x["verdict"] for x in results}
        scored = ts.score(pairs, verdicts)
        _print({"judge_offline": "run", "judge": lj.JUDGE_VERSION,
                "mode": lj.AGENTS_ONLY if args.agents_only else "with_vetoes", "matcher": args.matcher,
                "sceptic": args.sceptic, "written": written, "left_out_by_source": left_out, **out,
                "score": scored, "by_basis": _by_basis(pairs, verdicts), "reasons": lj.reason_counts(results)})
        return 0 if scored["gate"] == "pass" else 1
    except (ts.TruthSetError, OSError, ValueError, KeyError) as exc:
        _print({"judge_offline": "refused", "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
