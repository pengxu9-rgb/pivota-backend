"""Split the Backend Test Sweep into N deterministic, disjoint, complete shards.

WHY. `deploy prod` waits for the sweep on every main commit, and the sweep ran
as ONE pytest process: 26,182 tests in 31m21s on 2026-09-29 (run 36551976177),
32 minutes of wall time end to end. The work is embarrassingly parallel, so
.github/workflows/backend-test-sweep.yml now runs it as a matrix of shards and a
final `sweep` job merges their JUnit XML and applies the unchanged floors.

HOW A TEST IS ASSIGNED. Each shard collects the WHOLE suite, exactly as the
unsharded job did, then keeps only the items whose node id hashes to it:

    shard = sha256(nodeid) mod N, plus 1

A pure function of the node id, so every process computes the same answer and
nothing is written down that can go stale. It does not depend on test order,
on which other tests exist, or on timing data. A new test lands in exactly one
shard the moment it exists.

WHY THE NODE ID AND NOT THE FILE. CI time is concentrated in a few files: from
the per-line timestamps of that run's log, tests/test_reap_agentic_cart_link.py,
_ledger.py and _purchase.py alone took ~12 of its 30 test minutes. Hashing FILES
into four shards put 276/496/638/411 s of that run's test time in them; hashing
NODE IDS put 462/453/451/455 s. A file split across shards is safe here because
the whole suite was run sharded and passed; a test that needs an EARLIER test
in its own file to have run first is an order dependency, and its fix belongs in
that test, not here.

WHAT PROVES THE SPLIT IS COMPLETE. `--sweep-shard-manifest` writes, per shard,
the node ids it kept, the size and sha256 of the FULL collection it split, and
the collection-level skips (module-level `pytest.skip`) that JUnit records as
testcases in every shard. scripts/merge_sweep_shards.py refuses to produce a
merged sweep.xml unless every shard reported, every shard saw the identical
collection, the kept sets are pairwise disjoint and their union IS that
collection, and each shard's JUnit testcase count matches what it kept. A
missing shard, a nondeterministic parametrize id, or a test run twice is a
red `sweep`, never a quietly smaller count.

Inert unless `--sweep-shard` is passed; loaded with `-p tests.sweep_shard`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Tuple

import pytest

# Collection-level skips seen in this process. A module global rather than a
# stash because pytest_collectreport receives no config; one pytest process is
# one session, so there is nothing to keep apart.
_COLLECT_SKIPS: List[str] = []


def shard_of(nodeid: str, num_shards: int) -> int:
    """The 1-based shard `nodeid` belongs to out of `num_shards`."""
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % num_shards + 1


def collection_digest(nodeids) -> str:
    """Order-independent fingerprint of a collection, shared with the merger."""
    return hashlib.sha256("\n".join(sorted(nodeids)).encode("utf-8")).hexdigest()


def parse_spec(spec: str) -> Tuple[int, int]:
    """`K/N` -> (K, N), 1 <= K <= N. Anything else is a usage error, not shard 1."""
    try:
        k_raw, n_raw = spec.split("/")
        k, n = int(k_raw), int(n_raw)
    except ValueError:
        raise pytest.UsageError(f"--sweep-shard expects K/N, e.g. 2/4; got {spec!r}")
    if not 1 <= k <= n:
        raise pytest.UsageError(f"--sweep-shard {spec!r}: need 1 <= K <= N")
    return k, n


def pytest_addoption(parser):
    group = parser.getgroup("sweep-shard")
    group.addoption(
        "--sweep-shard", default=None, metavar="K/N",
        help="run only the tests whose node id hashes to shard K of N",
    )
    group.addoption(
        "--sweep-shard-manifest", default=None, metavar="PATH",
        help="write the shard's kept node ids and the full collection's fingerprint",
    )


def pytest_configure(config):
    spec = config.getoption("--sweep-shard")
    if spec is not None:
        parse_spec(spec)  # fail at startup, before a 60s collection


def pytest_collectreport(report):
    # A module that calls pytest.skip(allow_module_level=True) yields no items,
    # but JUnit still writes it as a skipped <testcase> -- in EVERY shard, since
    # every shard collects everything. The merger needs the list to count each once.
    if report.skipped:
        _COLLECT_SKIPS.append(report.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    """Runs after --deselect / -k / -m, so it splits exactly what would have run."""
    spec = config.getoption("--sweep-shard")
    if spec is None:
        return
    k, n = parse_spec(spec)
    collected = [item.nodeid for item in items]
    kept, dropped = [], []
    for item in items:
        (kept if shard_of(item.nodeid, n) == k else dropped).append(item)
    items[:] = kept
    if dropped:
        config.hook.pytest_deselected(items=dropped)

    manifest = config.getoption("--sweep-shard-manifest")
    if manifest:
        Path(manifest).write_text(json.dumps({
            "shard": k,
            "num_shards": n,
            "collected": len(collected),
            "collected_digest": collection_digest(collected),
            "kept": [item.nodeid for item in kept],
            "collect_skips": sorted(set(_COLLECT_SKIPS)),
        }, indent=0))
