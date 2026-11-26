"""Build, merge and score the identity truth set (services/identity_truth_set.py). OFFLINE ONLY.

This script reads local files and writes local files. It makes no database, store or model call.
The barcode rows come from the reviewed read-only SQL run through the controller's gate:
    readonly_sql_gated.sh scripts/ops_identity_truth_set_barcode_rows.sql <sha256> TRUTHSET
whose saved log is passed to `from-rows`: each row is 'TRUTHSET_ROW {json}', other lines are ignored.
The plan is reviewed first with ops_identity_truth_set_barcode_rows_explain.sql (prefix TRUTHSETX).
At the SQL's 1800-row cap the last barcode group may be cut, so it is dropped and the run is narrowed.

Every output path must be OUTSIDE this public repository (the set holds real store and product
names); the script refuses otherwise. It prints counts only, never names.

  python3 -m scripts.ops.build_identity_truth_set from-fixture --fixture F.json --out DIR/tier3.jsonl
  python3 -m scripts.ops.build_identity_truth_set from-owner-labels --pairs P.json --labels-dir D --reverted irp_x --out DIR/owner.jsonl
  python3 -m scripts.ops.build_identity_truth_set from-rows --rows RUN.log --out DIR/barcode.jsonl
  python3 -m scripts.ops.build_identity_truth_set merge --in DIR/tier3.jsonl --in DIR/barcode.jsonl --out DIR/truth_set.jsonl
  python3 -m scripts.ops.build_identity_truth_set judge-input --truth DIR/truth_set.jsonl --out DIR/judge_input.jsonl
  python3 -m scripts.ops.build_identity_truth_set score --truth DIR/truth_set.jsonl --verdicts V.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services import identity_truth_set as ts  # noqa: E402


def _print(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, sort_keys=True, ensure_ascii=False), flush=True)


ROW_PREFIX = "TRUTHSET_ROW "
ROW_CAP = 1800  # the SQL's LIMIT: exactly this many rows means the output was cut


def _rows_from_log(path: str) -> List[Dict[str, Any]]:
    """The JSON rows of a gated run's log (each 'TRUTHSET_ROW {...}'), or of a plain JSON-lines file."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(ROW_PREFIX):
                line = line[len(ROW_PREFIX):]
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "listing_key" in obj:
                rows.append(obj)
    return rows


def _barcode_group(row: Dict[str, Any]) -> str:
    return str(row.get("gtin") or "").strip().lstrip("0")  # the SQL's ORDER BY key (ltrim of '0')


def drop_cut_group(rows: List[Dict[str, Any]], cap: int = ROW_CAP) -> Tuple[List[Dict[str, Any]], bool]:
    """At the cap the last barcode group may be partial, and a partial group can look clean when the
    row that disagrees was cut: drop every row of the last group. Returns (rows, truncated)."""
    if len(rows) < cap:
        return rows, False
    last = _barcode_group(rows[-1])
    return [r for r in rows if _barcode_group(r) != last], True


def _write(path: str, pairs: List[ts.TruthPair]) -> int:
    return ts.write_jsonl(path, (ts.to_row(p) for p in pairs))


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("from-fixture")
    f.add_argument("--fixture", required=True)
    f.add_argument("--out", required=True)
    o = sub.add_parser("from-owner-labels")
    o.add_argument("--pairs", required=True, help="the review page's pair list (JSON array)")
    o.add_argument("--labels-dir", required=True, help="the page's label documents, one JSON file each")
    o.add_argument("--reverted", default="", help="comma-separated proposal ids reverted after labelling")
    o.add_argument("--out", required=True)
    r = sub.add_parser("from-rows")
    r.add_argument("--rows", required=True, help="the gated SQL run's log, or a JSON-lines file of rows")
    r.add_argument("--out", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--in", dest="inputs", action="append", required=True)
    m.add_argument("--out", required=True)
    j = sub.add_parser("judge-input")
    j.add_argument("--truth", required=True)
    j.add_argument("--out", required=True)
    s = sub.add_parser("score")
    s.add_argument("--truth", required=True)
    s.add_argument("--verdicts", required=True, help="JSON lines of {pair_id, verdict}")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "from-fixture":
            with open(args.fixture, encoding="utf-8") as fh:
                pairs, counts = ts.from_tier3_fixture(json.load(fh))
            _print({"truth_set": "from_fixture", "written": _write(args.out, pairs), "counts": counts,
                    "summary": ts.summary(pairs)})
        elif args.cmd == "from-owner-labels":
            with open(args.pairs, encoding="utf-8") as fh:
                page_pairs = json.load(fh)
            labels = []
            for name in sorted(os.listdir(args.labels_dir)):
                if name.endswith(".json"):
                    with open(os.path.join(args.labels_dir, name), encoding="utf-8") as fh:
                        labels.append(json.load(fh))
            reverted = [x.strip() for x in args.reverted.split(",") if x.strip()]
            pairs, counts = ts.from_owner_pair_labels(page_pairs, labels, reverted_proposals=reverted)
            _print({"truth_set": "from_owner_labels", "labels": len(labels), "written": _write(args.out, pairs),
                    "counts": counts, "summary": ts.summary(pairs)})
        elif args.cmd == "from-rows":
            read = _rows_from_log(args.rows)
            rows, truncated = drop_cut_group(read)
            pairs, counts = ts.from_barcode_rows(rows)
            _print({"truth_set": "from_rows", "rows": len(read), "rows_used": len(rows), "truncated": truncated,
                    "written": _write(args.out, pairs), "counts": counts, "summary": ts.summary(pairs)})
        elif args.cmd == "merge":
            sets = [[ts.from_row(x) for x in ts.read_jsonl(p)] for p in args.inputs]
            pairs, counts = ts.merge(*sets)
            _print({"truth_set": "merge", "written": _write(args.out, pairs), "counts": counts,
                    "summary": ts.summary(pairs)})
        elif args.cmd == "judge-input":
            pairs = [ts.from_row(x) for x in ts.read_jsonl(args.truth)]
            n = ts.write_jsonl(args.out, (ts.judge_view(p) for p in pairs))
            _print({"truth_set": "judge_input", "written": n})
        elif args.cmd == "score":
            pairs = [ts.from_row(x) for x in ts.read_jsonl(args.truth)]
            verdicts = {str(v["pair_id"]): str(v["verdict"]) for v in ts.read_jsonl(args.verdicts)}
            result = ts.score(pairs, verdicts)
            _print({"truth_set": "score", **result})
            return 0 if result["gate"] == "pass" else 1
    except (ts.TruthSetError, OSError, ValueError, KeyError) as exc:
        _print({"truth_set": "refused", "error": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
