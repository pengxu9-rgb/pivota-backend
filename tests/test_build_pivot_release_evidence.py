from __future__ import annotations

import argparse
import json
from pathlib import Path

import scripts.build_pivot_release_evidence as module


def test_build_pivot_release_evidence_distinguishes_blocking_and_legacy_probe(tmp_path: Path, monkeypatch) -> None:
    release_gate = tmp_path / "release-gate.json"
    smoke = tmp_path / "smoke.json"
    probe = tmp_path / "probe.json"
    output_json = tmp_path / "evidence.json"
    output_md = tmp_path / "evidence.md"

    release_gate.write_text(
        json.dumps({"summary": {"failed_cases": 0, "passed_cases": 4}}),
        encoding="utf-8",
    )
    smoke.write_text(json.dumps({"overall_ok": True}), encoding="utf-8")
    probe.write_text(
        json.dumps(
            {
                "records": [
                    {"http_status": 0, "ok": False, "product_count": None},
                    {"http_status": 401, "ok": False, "product_count": None},
                ]
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        migration=None,
        backfill_verify_json=None,
        release_gate_json=str(release_gate),
        catalog_pivot_smoke_json=str(smoke),
        search_chain_probe_json=str(probe),
        output_json=str(output_json),
        output_md=str(output_md),
        label="test-evidence",
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["blocking_ready"] is True
    assert summary["search_chain_probe_present"] is True
    assert summary["search_chain_probe_records_total"] == 2
    assert summary["search_chain_probe_records_ok"] == 0
    assert summary["search_chain_probe_records_http_200"] == 0
    assert summary["search_chain_probe_legacy_parity_ok"] is False
