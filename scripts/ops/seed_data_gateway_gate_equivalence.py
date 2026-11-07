#!/usr/bin/env python3
"""Prove the search seed_data strip changes no gateway gate decision.

The backend's search routes strip internal state from external-seed products' seed_data
(services/seed_data_egress_projection.strip_product_internal_seed_data). The gateway reads that response
and gates on it BEFORE its own egress projection: the retinoid/pregnancy safety net, the fragrance-free,
category, brand and strict-intent hard constraints, the hold contracts, the "X-free" ingredient exclusion,
the canonical price. This script builds products the way the backend builder does (top-level fields plus
seed_data, with internal state at every depth), strips each one with the real backend function, runs the
GATEWAY'S OWN gate functions (scripts/ops/seed_data_gateway_gate_eval.cjs over a PIVOTA-Agent tree) on
the full and the stripped product, and exits 1 unless every decision is identical.

    python scripts/ops/seed_data_gateway_gate_equivalence.py --gateway-dir <PIVOTA-Agent tree>
        [--node node] [--node-require <guard.cjs>] [--json-out out.json]

<PIVOTA-Agent tree>: a `git archive` export of the gateway commit to check against, with node_modules
available (a symlink to a checkout's node_modules is enough). Read-only; no network. Pass a loopback
guard with --node-require to be sure nothing leaves the machine.

It needs node and a gateway tree, so the backend test suite runs it only when
SEED_DATA_GATE_EQUIVALENCE_GATEWAY_DIR is set (tests/test_seed_data_gateway_gate_equivalence.py; skipped
in CI). Cases: the be#2727 review's (scratchpad repro make_cases.py: A/A2 vitamin-A topical via
category_path / catalog_category_path, B fragrance via category_path, C source_unavailable contract, D
non-merch hold, E retinoid in a non-title variant key, F retinoid in a variant title) plus retinoids
named only in seed_data's snapshot, ingredient lists, ingredient_intel, tags and variant options, a
stand-in, holds on the snapshot, a seed-only price, a lipstick, and a JSON-text snapshot.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.seed_data_egress_projection import strip_product_internal_seed_data  # noqa: E402

EVALUATOR = Path(__file__).resolve().with_name("seed_data_gateway_gate_eval.cjs")


def _internal_state() -> Dict[str, Any]:
    return {
        "price_read": {"amount": 1.0, "status": "parsed", "source": "jsonld"},
        "price_read_source": "jsonld",
        "variant_refresh": {"status": "all_re_read"},
        "variant_refresh_status": "all_re_read",
        "canonical_reset": {"reason": "drift"},
        "read_provenance": {"via": "handleless"},
        "read_source": "catalogue",
        "refresh_status": "ok",
        "refreshed_at": "2026-11-06T00:00:00Z",
        "extracted_at": "2026-11-06T00:00:00Z",
        "review_summary": {"rating": 4.5, "count": 12},
        "review_status": "reviewed",
        "audit": {"result": "pass"},
        "manual_overrides": {"price": True},
        "utm_template": "utm_source=pivota&utm_medium={tool}",
        "seller_ref": "seller_1",
        "description_source": "jsonld",
        "image_urls_source": "page_read",
    }


def _with_internal_state(seed_data: Dict[str, Any]) -> Dict[str, Any]:
    """The same blob with internal state at the top, on the snapshot and on every variant."""
    out = copy.deepcopy(seed_data)
    out.update(_internal_state())
    snapshot = out.get("snapshot")
    if isinstance(snapshot, dict):
        snapshot.update({"price_read_source": "jsonld", "variant_refresh_last_write": {"at": "x"}, "read_source": "page"})
        for variant in snapshot.get("variants") or []:
            if isinstance(variant, dict):
                variant["price_read"] = {"status": "parsed"}
    for variant in out.get("variants") or []:
        if isinstance(variant, dict):
            variant["variant_refresh_status"] = "re_read"
    return out


def _product(pid: str, title: str, seed_data: Dict[str, Any], **top: Any) -> Dict[str, Any]:
    """A product as routes.agent_api._build_external_seed_product emits it."""
    product = {
        "id": pid,
        "product_id": pid,
        "merchant_id": "external_seed",
        "merchant_name": "External",
        "platform": "external",
        "platform_product_id": pid,
        "title": title,
        "description": "",
        "product_type": "external",
        "source": "external_seed",
        "external_domain": "shop.example",
        "canonical_url": f"https://shop.example/products/{pid}",
        "destination_url": f"https://shop.example/products/{pid}?utm_source=pivota&pvt_click_id=clk_1",
        "external_url": f"https://shop.example/products/{pid}?utm_source=pivota&pvt_click_id=clk_1",
        "price": 30.0,
        "currency": "USD",
        "availability": "in_stock",
        "in_stock": True,
        "variants": [{"id": f"{pid}:1", "variant_id": "1", "title": "Default", "price": 30.0, "currency": "USD"}],
        "seed_data": seed_data,
    }
    product.update(top)
    return product


def build_cases() -> Dict[str, Dict[str, Any]]:
    """name -> product (full seed_data, internal state included)."""
    hold = {"contract_version": "external_seed.source_unavailable.v1", "status": "source_unavailable"}
    base: Dict[str, Dict[str, Any]] = {
        # --- the be#2727 review's cases ---
        "A_vitaminA_topical_via_category_path": _product(
            "vit-a-caps", "Vitamin A Capsules",
            {"title": "Vitamin A Capsules", "category_path": "beauty/skincare/treat/serum"},
        ),
        "A2_vitaminA_topical_via_catalog_category_path": _product(
            "vit-a-caps2", "Vitamin A Capsules",
            {"title": "Vitamin A Capsules", "catalog_category_path": "beauty/skincare/treat/serum"},
        ),
        "B_fragrance_via_category_path": _product(
            "santal", "Santal Eau de Parfum",
            {"title": "Santal Eau de Parfum", "brand": "Example Brand", "category_path": "beauty/fragrance/unisex"},
            brand="Example Brand", vendor="Example Brand",
        ),
        "C_source_unavailable_contract": _product(
            "gone-1", "Hydrating Gel Cream",
            {"title": "Hydrating Gel Cream", "source_unavailable_v1": hold, "snapshot": {"source_unavailable_v1": hold}},
        ),
        "D_non_merch_hold": _product(
            "gift-1", "Holiday Treat",
            {
                "title": "Holiday Treat",
                "non_merch_terminal_hold_v1": {"status": "non_merch_terminal_hold",
                                               "contract_version": "external_seed.non_merch_terminal_hold.v1"},
                "transaction_readiness_blocker_v1": {"status": "non_merch_terminal_hold"},
            },
        ),
        "E_retinoid_in_variant_option_labels": _product(
            "night-1", "Night Renewal Serum",
            {"title": "Night Renewal Serum", "variants": [
                {"variant_id": "1", "visible_option_labels": ["Retinol 0.5%"],
                 "url": "https://shop.example/products/night-1?variant=1"}]},
        ),
        "F_retinoid_in_variant_title": _product(
            "night-2", "Night Renewal Serum",
            {"title": "Night Renewal Serum", "variants": [{"variant_id": "1", "title": "Retinol 0.5% / 30ml"}]},
        ),
        # --- retinoids named only in seed_data ---
        "G_retinoid_in_snapshot_inci": _product(
            "night-3", "Night Renewal Serum",
            {"title": "Night Renewal Serum", "snapshot": {"inci_list": "Aqua, Glycerin, Retinol"}},
        ),
        "H_retinoid_in_key_ingredients": _product(
            "night-4", "Night Renewal Serum", {"key_ingredients": [{"name": "Retinal"}]},
        ),
        "I_retinoid_in_ingredient_intel": _product(
            "night-5", "Night Renewal Serum",
            {"ingredient_intel": {"actives": [{"name": "Hydroxypinacolone Retinoate"}], "inci_list": "Aqua, Retinol"}},
        ),
        "J_retinoid_in_snapshot_tags": _product(
            "night-6", "Night Renewal Serum", {"snapshot": {"tags": ["tretinoin"]}},
        ),
        "K_retinoid_in_variant_options": _product(
            "night-7", "Night Renewal Serum",
            {"snapshot": {"variants": [{"variant_id": "1", "options": ["Strength", "Retinol 0.3%"]}]}},
        ),
        "L_stand_in_only": _product(
            "bak-1", "Bakuchiol Retinol Alternative Serum",
            {"title": "Bakuchiol Retinol Alternative Serum", "inci_list": "Aqua, Bakuchiol"},
        ),
        "L2_stand_in_title_real_retinol_inci": _product(
            "bak-2", "Bakuchiol Retinol Alternative Serum",
            {"title": "Bakuchiol Retinol Alternative Serum", "inci_list": "Aqua, Bakuchiol, Retinol"},
        ),
        "M_holds_on_snapshot": _product(
            "gone-2", "Hydrating Gel Cream",
            {"snapshot": {"transaction_readiness_blocker_v1": {"status": "non_merch_terminal_hold"},
                          "non_merch_terminal_hold_v1": {"status": "non_merch_terminal_hold"}}},
        ),
        "N_price_only_in_seed_data": _product(
            "price-1", "Hydrating Gel Cream",
            {"price_amount": 24.0, "price_currency": "USD",
             "snapshot": {"price_amount": 24.0, "price_currency": "USD"}},
            price=None, currency=None,
            variants=[{"id": "price-1:1", "variant_id": "1", "title": "Default"}],
        ),
        "O_lipstick_via_category_path": _product(
            "lip-1", "Velvet Matte",
            {"title": "Velvet Matte", "category_path": "beauty/makeup/lips/lipstick", "product_type": "Lipstick"},
        ),
        "P_text_snapshot_with_retinoid": _product(
            "night-8", "Night Renewal Serum",
            {"snapshot": json.dumps({"inci_list": "Aqua, Retinol", "category_path": "beauty/skincare/treat/serum"})},
        ),
        "Q_fragrance_free_skincare_control": _product(
            "gel-1", "Hydrating Gel Cream",
            {"title": "Hydrating Gel Cream", "category_path": "beauty/skincare/moisturize/gel",
             "inci_list": "Aqua, Glycerin"},
        ),
    }
    cases: Dict[str, Dict[str, Any]] = {}
    for name, product in base.items():
        product = copy.deepcopy(product)
        seed = product["seed_data"]
        if isinstance(seed.get("snapshot"), str):
            snap = json.loads(seed["snapshot"])
            snap.update({"price_read_source": "jsonld", "read_source": "page"})
            seed = dict(seed, snapshot=json.dumps(snap))
            seed.update(_internal_state())
            product["seed_data"] = seed
        else:
            product["seed_data"] = _with_internal_state(seed)
        cases[name] = product
    return cases


#: Decisions the FULL product must produce, so a harness that silently evaluates nothing (every gate
#: null/false) cannot pass: each names the gate input the case exists for.
EXPECTED_FULL = {
    "A_vitaminA_topical_via_category_path": ("retinoid_for_safety", True),
    "A2_vitaminA_topical_via_catalog_category_path": ("retinoid_for_safety", True),
    "B_fragrance_via_category_path": ("hard_fragrance_free", ["fragrance_product_for_fragrance_free_query"]),
    "C_source_unavailable_contract": ("known_unservable", True),
    "D_non_merch_hold": ("hard_none", ["source_unavailable_or_non_merchandise"]),
    "E_retinoid_in_variant_option_labels": ("retinoid_for_safety", True),
    "F_retinoid_in_variant_title": ("retinoid_for_safety", True),
    "G_retinoid_in_snapshot_inci": ("retinoid_for_safety", True),
    "H_retinoid_in_key_ingredients": ("retinoid_for_safety", True),
    "I_retinoid_in_ingredient_intel": ("retinoid_for_safety", True),
    "J_retinoid_in_snapshot_tags": ("retinoid_for_safety", True),
    "K_retinoid_in_variant_options": ("retinoid_for_safety", True),
    "L2_stand_in_title_real_retinol_inci": ("retinoid_for_safety", True),
    "M_holds_on_snapshot": ("hard_none", ["source_unavailable_or_non_merchandise"]),
    "N_price_only_in_seed_data": ("canonical_price", {"amount": 24, "currency": "USD"}),
    "O_lipstick_via_category_path": ("category_path_text", "beauty/makeup/lips/lipstick"),
}


def run(gateway_dir: Path, *, node: str = "node", node_require: Optional[List[str]] = None,
        cases: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """-> (gate decisions per case, mismatch descriptions). An empty mismatch list means identical."""
    cases = cases if cases is not None else build_cases()
    payload = {}
    for name, full in cases.items():
        before = copy.deepcopy(full)
        stripped = strip_product_internal_seed_data(full)
        if full != before:
            raise AssertionError(f"{name}: the strip mutated its input")
        payload[name] = {"full": full, "stripped": stripped}
    with tempfile.TemporaryDirectory() as tmp:
        cases_path = Path(tmp) / "cases.json"
        cases_path.write_text(json.dumps(payload), encoding="utf-8")
        cmd = [node]
        for module in node_require or []:
            cmd += ["--require", module]
        cmd += [str(EVALUATOR), str(gateway_dir), str(cases_path)]
        env = dict(os.environ)
        env.setdefault("NODE_ENV", "test")
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"gate evaluator failed ({proc.returncode}): {proc.stderr[-4000:]}")
    line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("{")), None)
    if line is None:
        raise RuntimeError(f"gate evaluator printed no result: {proc.stdout[-2000:]} {proc.stderr[-2000:]}")
    decisions = json.loads(line)
    mismatches: List[str] = []
    for name, (gate, expected) in EXPECTED_FULL.items():
        got = (decisions.get(name) or {}).get("full", {}).get(gate)
        if got != expected:
            mismatches.append(f"{name} / {gate}: the FULL product gave {json.dumps(got)}, expected "
                              f"{json.dumps(expected)} (the case no longer exercises its gate)")
    for name in cases:
        got = decisions.get(name)
        if got is None:
            mismatches.append(f"{name}: no decisions")
            continue
        for gate, full_value in got["full"].items():
            stripped_value = got["stripped"].get(gate)
            if full_value != stripped_value:
                mismatches.append(f"{name} / {gate}: full={json.dumps(full_value)} stripped={json.dumps(stripped_value)}")
    return decisions, mismatches


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway-dir", required=True, type=Path)
    parser.add_argument("--node", default=shutil.which("node") or "node")
    parser.add_argument("--node-require", action="append", default=[])
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    decisions, mismatches = run(args.gateway_dir, node=args.node, node_require=args.node_require)
    if args.json_out:
        args.json_out.write_text(json.dumps({"decisions": decisions, "mismatches": mismatches}, indent=1))
    gates = sorted({g for c in decisions.values() for g in c["full"]})
    print(f"cases={len(decisions)} gates_per_case={len(gates)} mismatches={len(mismatches)}")
    for name, got in decisions.items():
        flagged = [g for g in gates if got["full"].get(g) not in (False, None, [], 1, 0, "")]
        print(f"  {name}: identical={all(got['full'][g] == got['stripped'].get(g) for g in gates)}"
              f" non-default={','.join(flagged[:6])}")
    for line in mismatches:
        print(f"MISMATCH {line}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
