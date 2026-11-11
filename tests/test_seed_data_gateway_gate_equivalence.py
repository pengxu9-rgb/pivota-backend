"""The search seed_data strip changes no gateway gate decision (scripts/ops/seed_data_gateway_gate_equivalence.py).

The equivalence run needs node and a PIVOTA-Agent tree, so it is SKIPPED unless
SEED_DATA_GATE_EQUIVALENCE_GATEWAY_DIR names one (CI does not set it). Run it locally before changing
INTERNAL_STATE_KEYS or the strip:

    git -C <PIVOTA-Agent> archive <commit> | tar -x -C <dir> && ln -s <PIVOTA-Agent>/node_modules <dir>/node_modules
    SEED_DATA_GATE_EQUIVALENCE_GATEWAY_DIR=<dir> [SEED_DATA_GATE_EQUIVALENCE_NODE_REQUIRE=<loopback guard.cjs>] \
        pytest tests/test_seed_data_gateway_gate_equivalence.py

The case-builder checks below always run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from services.seed_data_egress_projection import INTERNAL_STATE_KEYS, strip_product_internal_seed_data

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "seed_data_gateway_gate_equivalence.py"
_spec = importlib.util.spec_from_file_location("seed_data_gateway_gate_equivalence", _SCRIPT)
geq = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(geq)


def test_every_case_carries_internal_state_that_the_strip_removes() -> None:
    cases = geq.build_cases()
    assert len(cases) >= 19
    assert set(geq.EXPECTED_FULL) <= set(cases)
    for name, product in cases.items():
        stored = product["seed_data"]
        assert set(stored) & INTERNAL_STATE_KEYS, name
        stripped = strip_product_internal_seed_data(product)["seed_data"]
        assert not set(stripped) & INTERNAL_STATE_KEYS, name
        assert "INTERNAL" not in json.dumps(stripped)
        assert "utm_source=pivota&utm_medium" not in json.dumps(stripped), name


def test_the_review_cases_are_all_present() -> None:
    """The be#2727 review's repro cases (A, A2, B, C, D, E, F) stay in the battery."""
    names = set(geq.build_cases())
    for prefix in ("A_", "A2_", "B_", "C_", "D_", "E_", "F_"):
        assert any(n.startswith(prefix) for n in names), prefix


_GATEWAY_DIR = os.environ.get("SEED_DATA_GATE_EQUIVALENCE_GATEWAY_DIR", "").strip()
_NODE = shutil.which("node")


@pytest.mark.skipif(
    not _GATEWAY_DIR or not _NODE,
    reason="needs SEED_DATA_GATE_EQUIVALENCE_GATEWAY_DIR (a PIVOTA-Agent tree) and node; CI skips it",
)
def test_the_gateway_gates_decide_the_same_on_full_and_stripped_products() -> None:
    require = [r for r in os.environ.get("SEED_DATA_GATE_EQUIVALENCE_NODE_REQUIRE", "").split(",") if r.strip()]
    decisions, mismatches = geq.run(Path(_GATEWAY_DIR), node=_NODE, node_require=require)
    assert set(decisions) == set(geq.build_cases())
    assert mismatches == []
