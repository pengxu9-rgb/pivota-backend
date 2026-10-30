"""Merge the Backend Test Sweep's shard results into one sweep.xml, or refuse to.

The `sweep` job in .github/workflows/backend-test-sweep.yml runs this, then runs
the floor assert UNCHANGED against the sweep.xml written here. So this file has
one job beyond concatenation: make sure the merged XML describes exactly the run
the unsharded job would have made, or fail loudly. Every check below exists
because the failure it catches would otherwise surface as a green sweep that
tested less than it says.

  1. Every shard reported. The shard jobs' combined result (`needs.<job>.result`)
     must be `success`, and each of shards 1..N must have left both its JUnit XML
     and its manifest. A shard that crashed, timed out, or was never scheduled is
     a red `sweep`; the floors would often miss it, since one shard is ~1/N of
     the suite and the floors carry ~3% of headroom only by accident of timing.
  2. Every shard split the SAME collection. Each manifest records the size and
     sha256 of the full collection before splitting. If they differ — a
     parametrize id built from a memory address, a set's iteration order, an
     environment read at import — the hash partition is a partition of
     different sets, and a test can run twice or never.
  3. The kept sets are pairwise disjoint and their union is that collection:
     every collected test ran in exactly one shard.
  4. Each shard's JUnit testcases are exactly its kept tests plus the
     collection-level skips (by the same name JUnit writes), so a shard cannot
     have run something other than what it claims.

Collection-level skips (a module-level `pytest.skip`) are written as a skipped
<testcase> by EVERY shard, because every shard collects everything. They are
kept once, from the lowest shard, so `collected` and `skipped` in the merged XML
equal what one process reports. Executed counts are unaffected either way.

Stdlib only: the `sweep` job installs nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# The attributes pytest writes on <testsuite> that are counts, and so add up.
_COUNT_ATTRS = ("tests", "errors", "failures", "skipped")


def collection_digest(nodeids) -> str:
    """Must match tests/sweep_shard.py:collection_digest byte for byte."""
    return hashlib.sha256("\n".join(sorted(nodeids)).encode("utf-8")).hexdigest()


def junit_name(nodeid: str) -> Tuple[str, str]:
    """(classname, name) that pytest's junitxml writes for `nodeid`.

    Mirrors _pytest.junitxml.mangle_test_address: the file path becomes a dotted
    module path, `::` separates classes, and a parametrize suffix stays on the
    last part. No junit_prefix is configured in this repo.
    """
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def _suite(root: ET.Element) -> ET.Element:
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise ValueError("no <testsuite> element")
    return suite


def merge(shards_dir: Path, num_shards: int, needs_result: Optional[str],
          out: Path) -> List[str]:
    """Write the merged XML to `out`; return the list of violations (empty = ok)."""
    errors: List[str] = []
    if needs_result is not None and needs_result != "success":
        errors.append(f"the shard jobs concluded {needs_result!r}, not 'success' — at "
                      f"least one shard failed, timed out or was cancelled")

    manifests: Dict[int, dict] = {}
    suites: Dict[int, ET.Element] = {}
    for k in range(1, num_shards + 1):
        man_path = shards_dir / f"sweep-shard-{k}.manifest.json"
        xml_path = shards_dir / f"sweep-shard-{k}.xml"
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"shard {k}/{num_shards} left no readable manifest ({exc})")
            man = None
        try:
            suite = _suite(ET.parse(xml_path).getroot())
        except (OSError, ET.ParseError, ValueError) as exc:
            errors.append(f"shard {k}/{num_shards} left no readable JUnit XML ({exc})")
            suite = None
        if man is not None:
            if man.get("shard") != k or man.get("num_shards") != num_shards:
                errors.append(f"the manifest in shard {k}'s slot says it is shard "
                              f"{man.get('shard')}/{man.get('num_shards')}, "
                              f"expected {k}/{num_shards}")
            manifests[k] = man
        if suite is not None:
            suites[k] = suite

    # 2. One collection, split N ways.
    fingerprints = {(m.get("collected"), m.get("collected_digest")) for m in manifests.values()}
    if len(fingerprints) > 1:
        errors.append("the shards did not collect the same suite, so their split is not a "
                      "partition: " + ", ".join(
                          f"shard {k} collected {m.get('collected')} ({str(m.get('collected_digest'))[:12]})"
                          for k, m in sorted(manifests.items())))

    # 3. Disjoint, and the union is the whole collection.
    owner: Dict[str, int] = {}
    doubled: List[str] = []
    for k, man in sorted(manifests.items()):
        for nodeid in man.get("kept", []):
            if nodeid in owner:
                doubled.append(f"{nodeid} (shards {owner[nodeid]} and {k})")
            else:
                owner[nodeid] = k
    if doubled:
        errors.append(f"{len(doubled)} test(s) ran in more than one shard, e.g. "
                      + "; ".join(doubled[:5]))
    if len(manifests) == num_shards and len(fingerprints) == 1:
        (collected, digest), = fingerprints
        if len(owner) != collected or collection_digest(owner) != digest:
            errors.append(f"the shards ran {len(owner)} distinct tests but each collected "
                          f"{collected}: the union of the shards is not the collection, so "
                          f"some test ran in no shard")

    # 4. Each shard's JUnit is what it kept, plus the collection-level skips.
    skip_names: Set[Tuple[str, str]] = set()
    for k in sorted(set(manifests) & set(suites)):
        man, suite = manifests[k], suites[k]
        shard_skips = {junit_name(n) for n in man.get("collect_skips", [])}
        skip_names |= shard_skips
        expected = {junit_name(n) for n in man.get("kept", [])} | shard_skips
        seen = {(tc.get("classname") or "", tc.get("name") or "")
                for tc in suite.iter("testcase")}
        if seen != expected:
            missing, extra = sorted(expected - seen), sorted(seen - expected)
            errors.append(f"shard {k}: JUnit has {len(seen)} distinct testcases, expected "
                          f"{len(expected)} ({len(man.get('kept', []))} kept + "
                          f"{len(shard_skips)} collection skips); missing {missing[:3]}, "
                          f"unexpected {extra[:3]}")

    # Write whatever is there, so the floor assert that follows can still report.
    totals = {a: 0 for a in _COUNT_ATTRS}
    wall = 0.0
    merged = ET.Element("testsuite", name="pytest")
    written_skips: Set[Tuple[str, str]] = set()
    for k in sorted(suites):
        suite = suites[k]
        for a in _COUNT_ATTRS:
            totals[a] += int(suite.get(a, 0) or 0)
        wall = max(wall, float(suite.get("time", 0) or 0))
        for tc in suite.iter("testcase"):
            key = (tc.get("classname") or "", tc.get("name") or "")
            if key in skip_names and tc.find("skipped") is not None:
                if key in written_skips:
                    totals["tests"] -= 1
                    totals["skipped"] -= 1
                    continue
                written_skips.add(key)
            merged.append(tc)
    for a in _COUNT_ATTRS:
        merged.set(a, str(totals[a]))
    # The slowest shard, which is the wall time the sweep's pytest step cost.
    merged.set("time", f"{wall:.3f}")
    root = ET.Element("testsuites", name="pytest tests")
    root.append(merged)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)

    print(f"merged {len(suites)}/{num_shards} shard(s): tests={totals['tests']} "
          f"skipped={totals['skipped']} failures={totals['failures']} "
          f"errors={totals['errors']} slowest_shard={wall:.0f}s")
    for k, man in sorted(manifests.items()):
        suite = suites.get(k)
        print(f"  shard {k}: kept {len(man.get('kept', []))} of {man.get('collected')} "
              f"collected, junit tests={suite.get('tests') if suite is not None else '-'} "
              f"time={suite.get('time') if suite is not None else '-'}s")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shards-dir", type=Path, required=True)
    ap.add_argument("--num-shards", type=int, required=True)
    ap.add_argument("--needs-result", default=None,
                    help="needs.<shard job>.result; anything but 'success' fails")
    ap.add_argument("--out", type=Path, default=Path("sweep.xml"))
    args = ap.parse_args(argv)
    errors = merge(args.shards_dir, args.num_shards, args.needs_result, args.out)
    for e in errors:
        print(f"::error::{e}")
    if errors:
        print("the sharded sweep is not a complete, single run of the suite — see above")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
