"""Split the Postgres Dialect Gate into N file shards, and prove the shards add up.

WHY. The gate ran as ONE pytest process over every tests/test_*_postgres.py file
plus four ride-alongs. On 2026-10-12 that process took 21-23 minutes against the
job's `timeout-minutes: 25`, and run 38322789427 (#2577) was cancelled at 98% with
every test passing: the same commit's re-run took 21m02s for pytest, the cancelled
attempt was still running at 24m30s. Runner speed alone moved it ~15%. Measured from
that run's JUnit (4,814 tests, 185 files): 1,286 s of test time and ~45 s of
collection, against ~35 s for containers, checkout and install. No one fixture or
file dominates -- the slowest file is 6% of the total -- so the time has to be split,
not cut.

HOW A FILE IS ASSIGNED.

    shard = sha256(path) mod N, plus 1

over the path the discover glob emits (`tests/test_x_postgres.py`). A pure function
of the path: adding, removing or renaming a file never moves ANY other file to a
different shard. That stability is the point. Hashing FILES rather than test node
ids (which is what the Backend Test Sweep does, tests/sweep_shard.py) is forced by
how this gate works:

  * each shard keeps the gate's premise -- ONE database shared by every file it runs,
    in the same relative order as the unsharded run -- so a file still meets tables
    an earlier file created narrower (the #2311 lesson). Splitting a file would hand
    half its module-scoped fixtures to a different database;
  * the four ride-alongs run together, after the gate files, in their declared order,
    in ONE shard (RIDE_ALONG_SHARD). test_quality_backfill_partial_update.py ALTERs
    and restores columns and must stay last; that ordering is per process, and only
    one process can honour it.

A position-based split (round-robin, chunks) would reshuffle every file after an
inserted one and give a new file's PR a different set of neighbours for every file
behind it; a cross-file dependency would surface on an unrelated change.

WHAT PROVES THE SPLIT IS COMPLETE. `plan` writes a manifest per shard; `verify`
(run by the final `execute real routes on real Postgres` job, after every shard)
refuses unless every shard job succeeded and left its manifest and JUnit, each
manifest is exactly what `plan` computes from this checkout, the manifests are
disjoint and their union is every gate file plus every present ride-along, NO test
in any shard was skipped, every gate module executed at least one test in the shard
that owns it, and the recovery report ran in its shard without a skip. A missing
shard, a shard that ran a different list, or a gate file in no shard is a red gate.

Stdlib only: the final job installs nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

GATE_GLOB = "tests/test_*_postgres.py"


def shard_of(path: str, num_shards: int) -> int:
    """The 1-based shard `path` belongs to out of `num_shards`."""
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")
    digest = hashlib.sha256(path.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % num_shards + 1


def discover(root: Path) -> List[str]:
    """Every dialect-gate file, as the relative posix path the glob emits, sorted.

    `test_*_postgres.py` only -- the `_postgres_integration.py` files need real data
    and are not dialect gates (see the workflow header).
    """
    return sorted(p.relative_to(root).as_posix() for p in root.glob(GATE_GLOB) if p.is_file())


def module_of(path: str) -> str:
    return Path(path).stem


def plan(gate_files: Sequence[str], ride_alongs: Sequence[str], shard: int,
         num_shards: int, ride_along_shard: int) -> List[str]:
    """The files shard `shard` runs, in run order: its gate files in discovery order,
    then -- in the ride-along shard only -- the ride-alongs in their declared order."""
    if not 1 <= shard <= num_shards:
        raise ValueError(f"shard {shard} is not in 1..{num_shards}")
    if not 1 <= ride_along_shard <= num_shards:
        raise ValueError(f"ride-along shard {ride_along_shard} is not in 1..{num_shards}")
    files = [f for f in gate_files if shard_of(f, num_shards) == shard]
    if shard == ride_along_shard:
        files += list(ride_alongs)
    return files


def present(root: Path, paths: Iterable[str]) -> List[str]:
    """The ride-alongs that exist, order kept -- the unsharded step's `[ -e "$f" ]`."""
    return [p for p in paths if (root / p).is_file()]


# ── plan ──────────────────────────────────────────────────────────────────────


def cmd_plan(args: argparse.Namespace) -> int:
    root = Path(args.root)
    gate_files = discover(root)
    if not gate_files:
        print(f"No {GATE_GLOB} files found.", file=sys.stderr)
        print("This gate has nothing to run, which means it is not gating anything.",
              file=sys.stderr)
        return 1
    ride_alongs = present(root, args.ride_alongs.split())
    files = plan(gate_files, ride_alongs, args.shard, args.num_shards, args.ride_along_shard)
    gate_modules = [module_of(f) for f in files if f in gate_files]
    # A shard with no gate file would pass its own coverage check vacuously at best,
    # and only fail later on an empty GATE_MODULES. Say what happened instead.
    if not gate_modules:
        print(f"shard {args.shard}/{args.num_shards} was assigned no gate file "
              f"({len(gate_files)} discovered). Lower the shard count.", file=sys.stderr)
        return 1

    for f in files:
        print(f"  {f}")
    print(f"shard {args.shard}/{args.num_shards}: {len(gate_modules)} of "
          f"{len(gate_files)} gate file(s), {len(files) - len(gate_modules)} ride-along(s)")

    manifest = {
        "shard": args.shard,
        "num_shards": args.num_shards,
        "files": files,
        "gate_modules": gate_modules,
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2) + "\n")

    out = os.getenv("GITHUB_OUTPUT")
    lines = [f"files={' '.join(files)}", f"gate_modules={' '.join(gate_modules)}"]
    if out:
        with open(out, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))
    return 0


# ── verify ────────────────────────────────────────────────────────────────────


def _find(shards_dir: Path, name: str) -> Optional[Path]:
    """`name` anywhere under the downloaded artifacts; None if absent. More than one
    copy is ambiguous and reported as absent-by-conflict by the caller."""
    hits = sorted(shards_dir.rglob(name))
    return hits[0] if len(hits) == 1 else None


def _junit_counts(path: Path) -> Dict[str, object]:
    root = ET.parse(path).getroot()
    suites = root.findall("testsuite") or ([root] if root.tag == "testsuite" else [])
    cases = list(root.iter("testcase"))
    return {
        "tests": sum(int(s.get("tests", 0)) for s in suites),
        "skipped": sum(int(s.get("skipped", 0)) for s in suites),
        "time": sum(float(s.get("time", 0) or 0) for s in suites),
        "cases": cases,
    }


def verify(shards_dir: Path, root: Path, num_shards: int, ride_alongs: Sequence[str],
           ride_along_shard: int, recovery_shard: int,
           needs_result: Optional[str]) -> List[str]:
    """Every reason the shards do not add up to one complete gate run (empty = ok)."""
    errors: List[str] = []
    if needs_result is not None and needs_result != "success":
        errors.append(f"the shard jobs concluded {needs_result!r}, not 'success' -- at "
                      "least one shard failed, timed out or was cancelled")

    gate_files = discover(root)
    if not gate_files:
        errors.append(f"no {GATE_GLOB} files in this checkout")
        return errors
    ride = present(root, ride_alongs)
    owner: Dict[str, int] = {}

    for k in range(1, num_shards + 1):
        manifest_path = _find(shards_dir, f"shard-{k}.manifest.json")
        junit_path = _find(shards_dir, f"pytest-postgres-shard-{k}.xml")
        if manifest_path is None or junit_path is None:
            errors.append(f"shard {k}: missing or duplicated "
                          f"{'manifest' if manifest_path is None else 'JUnit XML'} -- "
                          "the shard never reported")
            continue
        manifest = json.loads(manifest_path.read_text())
        expected = plan(gate_files, ride, k, num_shards, ride_along_shard)
        if manifest.get("files") != expected:
            errors.append(f"shard {k} ran {manifest.get('files')!r} but this checkout "
                          f"plans {expected!r}")
        for f in manifest.get("files") or []:
            if f in owner:
                errors.append(f"{f} ran in shard {owner[f]} AND shard {k}")
            owner[f] = k

        counts = _junit_counts(junit_path)
        print(f"shard {k}: files={len(manifest.get('files') or [])} "
              f"tests={counts['tests']} skipped={counts['skipped']} "
              f"time={counts['time']:.0f}s")
        if counts["tests"] == 0:
            errors.append(f"shard {k}: pytest collected 0 tests -- the shard ran nothing")
        if counts["skipped"]:
            errors.append(
                f"shard {k}: {counts['skipped']} test(s) SKIPPED. The gate files skip "
                "themselves when DATABASE_URL is not a Postgres URL, so a skip here means "
                "the gate is green while testing nothing.")
        ran = set()
        for case in counts["cases"]:
            classname = case.get("classname") or ""
            filename = case.get("file") or ""
            for module in manifest.get("gate_modules") or []:
                if module in classname or module in filename:
                    ran.add(module)
        missing = [m for m in (manifest.get("gate_modules") or []) if m not in ran]
        if missing:
            errors.append(f"shard {k}: these gate modules executed no tests: "
                          + ", ".join(missing))

        recovery = _find(shards_dir, f"pytest-recovery-postgres-shard-{k}.xml")
        if k == recovery_shard:
            if recovery is None:
                errors.append(f"shard {k}: the recovery report never ran")
            else:
                rc = _junit_counts(recovery)
                if not rc["cases"] or rc["skipped"]:
                    errors.append(f"shard {k}: the recovery report ran {rc['tests']} "
                                  f"test(s) with {rc['skipped']} skipped")
        elif recovery is not None:
            errors.append(f"shard {k}: ran the recovery report, which belongs to "
                          f"shard {recovery_shard}")

    want = set(gate_files) | set(ride)
    dropped = sorted(want - set(owner))
    extra = sorted(set(owner) - want)
    if dropped:
        errors.append("in no shard: " + ", ".join(dropped))
    if extra:
        errors.append("ran but is neither a gate file nor a ride-along: " + ", ".join(extra))
    return errors


def cmd_verify(args: argparse.Namespace) -> int:
    errors = verify(Path(args.shards_dir), Path(args.root), args.num_shards,
                    args.ride_alongs.split(), args.ride_along_shard, args.recovery_shard,
                    args.needs_result)
    if errors:
        for e in errors:
            print(f"::error::{e}")
        return 1
    print(f"all {args.num_shards} shard(s) reported; together they ran every gate file "
          "and ride-along exactly once, with zero skips")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=".")
        p.add_argument("--num-shards", type=int, required=True)
        p.add_argument("--ride-alongs", default="")
        p.add_argument("--ride-along-shard", type=int, required=True)

    p_plan = sub.add_parser("plan", help="list this shard's files; write its manifest")
    common(p_plan)
    p_plan.add_argument("--shard", type=int, required=True)
    p_plan.add_argument("--manifest", required=True)
    p_plan.set_defaults(func=cmd_plan)

    p_verify = sub.add_parser("verify", help="prove the shards add up to one run")
    common(p_verify)
    p_verify.add_argument("--shards-dir", required=True)
    p_verify.add_argument("--recovery-shard", type=int, required=True)
    p_verify.add_argument("--needs-result", default=None)
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
