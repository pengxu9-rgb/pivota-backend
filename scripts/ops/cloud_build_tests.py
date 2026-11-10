"""Helpers for infra/gcp/cloudbuild.tests.yaml: the backend test suites on Cloud Build.

    python scripts/ops/cloud_build_tests.py pg-files --shard K --manifest PATH
        Prints the Postgres dialect gate's file list for shard K. The shard count, the ride-along
        shard and the ride-along files are READ from .github/workflows/postgres-dialect-gate.yml,
        so this runner and CI can never disagree about them; the split itself is
        scripts/postgres_dialect_gate_shards.py, the planner CI runs.

    python scripts/ops/cloud_build_tests.py summary --ci-dir DIR --sqlite-shards N --pg-shards M
        Reads every shard's recorded exit code and JUnit XML, proves the SQLite shards add up to one
        complete run (scripts/merge_sweep_shards.py, the same check CI's `sweep` job makes), and
        requires zero skips in every Postgres shard (the dialect gate's own rule). Prints one table
        and the failing test ids, and exits non-zero when anything is red, missing or skipped.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
GATE_WORKFLOW = ROOT / ".github" / "workflows" / "postgres-dialect-gate.yml"
SUMMARY_BEGIN = "=== CLOUD BUILD TEST SUMMARY ==="
SUMMARY_END = "=== END SUMMARY ==="


def gate_env() -> Dict[str, str]:
    import yaml

    env = yaml.safe_load(GATE_WORKFLOW.read_text())["env"]
    return {k: str(env[k]) for k in ("DIALECT_GATE_SHARDS", "RIDE_ALONG_SHARD", "RIDE_ALONGS")}


def pg_files(shard: int, manifest: str) -> List[str]:
    env = gate_env()
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "postgres_dialect_gate_shards.py"), "plan",
         "--num-shards", env["DIALECT_GATE_SHARDS"], "--shard", str(shard),
         "--ride-along-shard", env["RIDE_ALONG_SHARD"], "--ride-alongs", " ".join(env["RIDE_ALONGS"].split()),
         "--manifest", manifest],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("files="):
            return line[len("files="):].split()
    raise SystemExit("the planner printed no files= line")


def _junit(path: Path) -> Optional[Dict[str, object]]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = root.findall("testsuite") or ([root] if root.tag == "testsuite" else [])
    counts = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
    failed = []
    for case in root.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is not None:
            message = " ".join(str(bad.get("message") or "").split())[:240]
            failed.append(f"{case.get('classname')}::{case.get('name')}  -- {message}")
    return {**counts, "failed_ids": failed}


def _rc(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def summary(ci_dir: Path, sqlite_shards: int, pg_shards: int) -> int:
    red: List[str] = []
    rows: List[str] = []
    failed_ids: List[str] = []
    for kind, n, xml_of in (
        ("sqlite", sqlite_shards, lambda k: ci_dir / "shards" / f"sweep-shard-{k}.xml"),
        ("postgres", pg_shards, lambda k: ci_dir / f"postgres-{k}.xml"),
    ):
        for k in range(1, n + 1):
            rc = _rc(ci_dir / f"{kind}-{k}.rc")
            j = _junit(xml_of(k))
            if j is None:
                red.append(f"{kind} shard {k}: no readable JUnit XML (rc={rc})")
                rows.append(f"{kind:<8} {k:>2}  rc={rc}  NO RESULT")
                continue
            rows.append(f"{kind:<8} {k:>2}  rc={rc}  tests={j['tests']} failures={j['failures']} "
                        f"errors={j['errors']} skipped={j['skipped']}")
            failed_ids += list(j["failed_ids"])
            if rc != 0:
                red.append(f"{kind} shard {k} exited {rc}")
            if kind == "postgres" and int(j["skipped"]):
                red.append(f"postgres shard {k} skipped {j['skipped']} test(s); the gate allows none")
    sqlite_ok = all(_rc(ci_dir / f"sqlite-{k}.rc") == 0 for k in range(1, sqlite_shards + 1))
    merge = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "merge_sweep_shards.py"), "--shards-dir", str(ci_dir / "shards"),
         "--num-shards", str(sqlite_shards), "--needs-result", "success" if sqlite_ok else "failure",
         "--out", str(ci_dir / "sweep.xml")],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(SUMMARY_BEGIN)
    print("\n".join(rows))
    print("--- sqlite completeness (scripts/merge_sweep_shards.py)")
    print((merge.stdout + merge.stderr).strip()[-3000:])
    if merge.returncode != 0:
        red.append("the SQLite shards do not add up to one complete green run")
    if failed_ids:
        print("--- failing tests")
        print("\n".join(sorted(set(failed_ids))[:200]))
    print("--- verdict:", "GREEN" if not red else "RED")
    for line in red:
        print("  *", line)
    print(SUMMARY_END)
    return 0 if not red else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_files = sub.add_parser("pg-files")
    p_files.add_argument("--shard", type=int, required=True)
    p_files.add_argument("--manifest", required=True)
    p_sum = sub.add_parser("summary")
    p_sum.add_argument("--ci-dir", type=Path, required=True)
    p_sum.add_argument("--sqlite-shards", type=int, required=True)
    p_sum.add_argument("--pg-shards", type=int, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "pg-files":
        print(" ".join(pg_files(args.shard, args.manifest)))
        return 0
    return summary(args.ci_dir, args.sqlite_shards, args.pg_shards)


if __name__ == "__main__":
    raise SystemExit(main())
