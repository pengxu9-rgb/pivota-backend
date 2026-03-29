from __future__ import annotations

import argparse
import json
from pathlib import Path

import scripts.run_pivot_release_bundle as module


def _result(returncode: int = 0) -> dict:
    return {
        "cmd": ["python3", "fake.py"],
        "returncode": returncode,
        "stdout": "{}",
        "stderr": "",
        "elapsed_ms": 1.0,
    }


def test_run_bundle_builds_expected_artifacts(tmp_path: Path) -> None:
    recorded = []

    def fake_runner(script_path: Path, script_args: list[str]) -> dict:
        recorded.append((script_path.name, list(script_args)))
        output_json = None
        output_md = None
        for index, token in enumerate(script_args):
            if token == "--output-json":
                output_json = Path(script_args[index + 1])
            if token == "--output-md":
                output_md = Path(script_args[index + 1])
        if output_md:
            output_md.parent.mkdir(parents=True, exist_ok=True)
            output_md.write_text("# stub\n", encoding="utf-8")
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            if script_path.name == "catalog_backfill_verify.py":
                merchant_id = script_args[script_args.index("--merchant-id") + 1]
                mode = script_args[script_args.index("--mode") + 1]
                payload = {
                    "merchant_id": merchant_id,
                    "mode": mode,
                    "summary": {
                        "verify": {
                            "catalog_products": 2,
                            "catalog_skus": 3,
                            "catalog_offers": 4,
                            "missing_product_keys_count": 0,
                        }
                    },
                }
            elif script_path.name == "pivot_multi_release_gate.py":
                payload = {"summary": {"failed_cases": 0, "passed_cases": 2}}
            elif script_path.name == "smoke_catalog_pivot_v1.py":
                payload = {"overall_ok": True, "steps": []}
            else:
                payload = {"ok": True}
            output_json.write_text(json.dumps(payload), encoding="utf-8")
        return _result()

    args = argparse.Namespace(
        base_url="https://pivot.example",
        release_gate_base_url=None,
        smoke_base_url=None,
        corpus=str(tmp_path / "corpus.json"),
        merchant_ids=["merch_a", "merch_b"],
        smoke_merchant_id=None,
        label="bundle-test",
        output_dir=str(tmp_path / "out"),
        migration_artifact=str(tmp_path / "migration.json"),
        migration_mode="skip",
        database_url="",
        backfill_platform="shopify",
        backfill_limit=25,
        backfill_include_expired=False,
        smoke_query="vitamin c",
        smoke_offer_id=None,
        smoke_product_key=None,
        smoke_sku_key=None,
        timeout_seconds=10.0,
        catalog_migration_verify_smoke=True,
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=True,
        catalog_sync_limit=2,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        service_side_data_plane_verify=False,
        search_chain_probe=True,
        search_chain_probe_blocking=False,
        probe_agent_base_url="https://agent.example",
        probe_gateway_url="https://gateway.example/api/gateway",
        probe_source="shopping_agent",
        probe_rounds=2,
        probe_limit=12,
        probe_sleep_ms=0,
        probe_queries=["vitamin c", "lip balm"],
        probe_agent_api_key="agent-key",
        probe_gateway_api_key="gateway-key",
        header=["Authorization: Bearer test"],
        smoke_header=[],
        release_gate_default_rollout_mode="shadow",
        skip_backfill_apply=False,
        skip_backfill_verify=False,
        skip_release_gate=False,
        skip_smoke=False,
        skip_evidence=False,
    )
    Path(args.corpus).write_text("[]", encoding="utf-8")
    Path(args.migration_artifact).write_text(json.dumps({"ok": True}), encoding="utf-8")

    report, exit_code = module._run_bundle(args, runner=fake_runner)

    assert exit_code == 0
    assert report["overall_ok"] is True
    assert report["non_blocking_steps_total"] == 1
    assert len(recorded) == 8
    assert (tmp_path / "out" / "catalog-backfill-verify-bundle.json").exists()
    assert (tmp_path / "out" / "pivot-release-evidence.json").exists()
    evidence_call = next(args for name, args in recorded if name == "build_pivot_release_evidence.py")
    smoke_call = next(args for name, args in recorded if name == "smoke_catalog_pivot_v1.py")
    probe_call = next(args for name, args in recorded if name == "search_chain_inventory_probe.py")
    assert "--backfill-verify-json" in evidence_call
    assert "--release-gate-json" in evidence_call
    assert "--catalog-pivot-smoke-json" in evidence_call
    assert "--search-chain-probe-json" in evidence_call
    assert "--catalog-migration-verify-smoke" in smoke_call
    assert "--agent-base-url" in probe_call
    assert "--gateway-url" in probe_call
    assert "--queries" in probe_call


def test_run_bundle_returns_nonzero_when_step_fails(tmp_path: Path) -> None:
    calls = []

    def fake_runner(script_path: Path, script_args: list[str]) -> dict:
        calls.append(script_path.name)
        output_json = None
        output_md = None
        for index, token in enumerate(script_args):
            if token == "--output-json":
                output_json = Path(script_args[index + 1])
            if token == "--output-md":
                output_md = Path(script_args[index + 1])
        if output_md:
            output_md.parent.mkdir(parents=True, exist_ok=True)
            output_md.write_text("# stub\n", encoding="utf-8")
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            output_json.write_text(json.dumps({"overall_ok": False, "summary": {"failed_cases": 1}}), encoding="utf-8")
        if script_path.name == "pivot_multi_release_gate.py":
            return _result(returncode=1)
        return _result()

    args = argparse.Namespace(
        base_url="https://pivot.example",
        release_gate_base_url=None,
        smoke_base_url=None,
        corpus=str(tmp_path / "corpus.json"),
        merchant_ids=["merch_a"],
        smoke_merchant_id="merch_a",
        label="bundle-test",
        output_dir=str(tmp_path / "out"),
        migration_artifact=None,
        migration_mode="skip",
        database_url="",
        backfill_platform=None,
        backfill_limit=10,
        backfill_include_expired=False,
        smoke_query="vitamin c",
        smoke_offer_id=None,
        smoke_product_key=None,
        smoke_sku_key=None,
        timeout_seconds=10.0,
        catalog_migration_verify_smoke=False,
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        service_side_data_plane_verify=False,
        search_chain_probe=False,
        search_chain_probe_blocking=False,
        probe_agent_base_url=None,
        probe_gateway_url=None,
        probe_source="shopping_agent",
        probe_rounds=3,
        probe_limit=24,
        probe_sleep_ms=250,
        probe_queries=None,
        probe_agent_api_key="",
        probe_gateway_api_key="",
        header=[],
        smoke_header=[],
        release_gate_default_rollout_mode="shadow",
        skip_backfill_apply=True,
        skip_backfill_verify=True,
        skip_release_gate=False,
        skip_smoke=False,
        skip_evidence=False,
    )
    Path(args.corpus).write_text("[]", encoding="utf-8")

    report, exit_code = module._run_bundle(args, runner=fake_runner)

    assert exit_code == 1
    assert report["overall_ok"] is False
    assert "pivot_multi_release_gate.py" in calls
    assert "build_pivot_release_evidence.py" in calls


def test_run_bundle_can_run_migration_and_forward_artifact(tmp_path: Path) -> None:
    recorded = []

    def fake_runner(script_path: Path, script_args: list[str]) -> dict:
        recorded.append((script_path.name, list(script_args)))
        output_json = None
        output_md = None
        for index, token in enumerate(script_args):
            if token == "--output-json":
                output_json = Path(script_args[index + 1])
            if token == "--output-md":
                output_md = Path(script_args[index + 1])
        if output_md:
            output_md.parent.mkdir(parents=True, exist_ok=True)
            output_md.write_text("# stub\n", encoding="utf-8")
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            payload = {"success": True, "summary": {"failed_cases": 0, "passed_cases": 1}}
            output_json.write_text(json.dumps(payload), encoding="utf-8")
        return _result()

    args = argparse.Namespace(
        base_url="https://pivot.example",
        release_gate_base_url=None,
        smoke_base_url=None,
        corpus=str(tmp_path / "corpus.json"),
        merchant_ids=["merch_a"],
        smoke_merchant_id="merch_a",
        label="bundle-test",
        output_dir=str(tmp_path / "out"),
        migration_artifact=None,
        migration_mode="apply-verify",
        database_url="sqlite:///" + str(tmp_path / "bundle.sqlite3"),
        backfill_platform=None,
        backfill_limit=10,
        backfill_include_expired=False,
        smoke_query="vitamin c",
        smoke_offer_id=None,
        smoke_product_key=None,
        smoke_sku_key=None,
        timeout_seconds=10.0,
        catalog_migration_verify_smoke=False,
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        service_side_data_plane_verify=False,
        search_chain_probe=False,
        search_chain_probe_blocking=False,
        probe_agent_base_url=None,
        probe_gateway_url=None,
        probe_source="shopping_agent",
        probe_rounds=3,
        probe_limit=24,
        probe_sleep_ms=250,
        probe_queries=None,
        probe_agent_api_key="",
        probe_gateway_api_key="",
        header=[],
        smoke_header=[],
        release_gate_default_rollout_mode="shadow",
        skip_backfill_apply=True,
        skip_backfill_verify=True,
        skip_release_gate=True,
        skip_smoke=True,
        skip_evidence=False,
    )
    Path(args.corpus).write_text("[]", encoding="utf-8")

    report, exit_code = module._run_bundle(args, runner=fake_runner)

    assert exit_code == 0
    assert report["overall_ok"] is True
    assert recorded[0][0] == "catalog_migration_058.py"
    evidence_call = next(script_args for name, script_args in recorded if name == "build_pivot_release_evidence.py")
    migration_arg_index = evidence_call.index("--migration")
    assert evidence_call[migration_arg_index + 1].endswith("catalog-migration-058.json")


def test_run_bundle_service_side_data_plane_verify_skips_local_steps_and_enriches_smoke(tmp_path: Path) -> None:
    recorded = []

    def fake_runner(script_path: Path, script_args: list[str]) -> dict:
        recorded.append((script_path.name, list(script_args)))
        output_json = None
        output_md = None
        for index, token in enumerate(script_args):
            if token == "--output-json":
                output_json = Path(script_args[index + 1])
            if token == "--output-md":
                output_md = Path(script_args[index + 1])
        if output_md:
            output_md.parent.mkdir(parents=True, exist_ok=True)
            output_md.write_text("# stub\n", encoding="utf-8")
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            payload = {"overall_ok": True, "summary": {"failed_cases": 0, "passed_cases": 1}}
            output_json.write_text(json.dumps(payload), encoding="utf-8")
        return _result()

    args = argparse.Namespace(
        base_url="https://pivot.example",
        release_gate_base_url=None,
        smoke_base_url=None,
        corpus=str(tmp_path / "corpus.json"),
        merchant_ids=["merch_a"],
        smoke_merchant_id="merch_a",
        label="bundle-test",
        output_dir=str(tmp_path / "out"),
        migration_artifact=None,
        migration_mode="apply-verify",
        database_url="",
        backfill_platform=None,
        backfill_limit=10,
        backfill_include_expired=False,
        smoke_query="vitamin c",
        smoke_offer_id=None,
        smoke_product_key=None,
        smoke_sku_key=None,
        timeout_seconds=10.0,
        catalog_migration_verify_smoke=False,
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=45.0,
        catalog_sync_poll_interval_seconds=3.0,
        service_side_data_plane_verify=True,
        search_chain_probe=False,
        search_chain_probe_blocking=False,
        probe_agent_base_url=None,
        probe_gateway_url=None,
        probe_source="shopping_agent",
        probe_rounds=3,
        probe_limit=24,
        probe_sleep_ms=250,
        probe_queries=None,
        probe_agent_api_key="",
        probe_gateway_api_key="",
        header=[],
        smoke_header=[],
        release_gate_default_rollout_mode="shadow",
        skip_backfill_apply=False,
        skip_backfill_verify=False,
        skip_release_gate=True,
        skip_smoke=False,
        skip_evidence=True,
    )
    Path(args.corpus).write_text("[]", encoding="utf-8")

    report, exit_code = module._run_bundle(args, runner=fake_runner)

    assert exit_code == 0
    assert report["overall_ok"] is True
    called_scripts = [name for name, _ in recorded]
    assert "catalog_migration_058.py" not in called_scripts
    assert "catalog_backfill_verify.py" not in called_scripts
    smoke_call = next(script_args for name, script_args in recorded if name == "smoke_catalog_pivot_v1.py")
    assert "--skip-pivot-query" in smoke_call
    assert "--catalog-migration-verify-smoke" in smoke_call
    assert "--catalog-sync-job-smoke" in smoke_call
    assert "--catalog-sync-wait-seconds" in smoke_call
    assert "--catalog-sync-poll-interval-seconds" in smoke_call


def test_run_bundle_search_probe_is_non_blocking_by_default(tmp_path: Path) -> None:
    def fake_runner(script_path: Path, script_args: list[str]) -> dict:
        output_json = None
        output_md = None
        for index, token in enumerate(script_args):
            if token == "--output-json":
                output_json = Path(script_args[index + 1])
            if token == "--output-md":
                output_md = Path(script_args[index + 1])
        if output_md:
            output_md.parent.mkdir(parents=True, exist_ok=True)
            output_md.write_text("# stub\n", encoding="utf-8")
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            if script_path.name == "pivot_multi_release_gate.py":
                payload = {"summary": {"failed_cases": 0, "passed_cases": 1}}
            elif script_path.name == "smoke_catalog_pivot_v1.py":
                payload = {"overall_ok": True, "steps": []}
            else:
                payload = {"ok": True}
            output_json.write_text(json.dumps(payload), encoding="utf-8")
        if script_path.name == "search_chain_inventory_probe.py":
            return _result(returncode=1)
        return _result()

    args = argparse.Namespace(
        base_url="https://pivot.example",
        release_gate_base_url=None,
        smoke_base_url=None,
        corpus=str(tmp_path / "corpus.json"),
        merchant_ids=["merch_a"],
        smoke_merchant_id="merch_a",
        label="bundle-test",
        output_dir=str(tmp_path / "out"),
        migration_artifact=None,
        migration_mode="skip",
        database_url="",
        backfill_platform=None,
        backfill_limit=10,
        backfill_include_expired=False,
        smoke_query="vitamin c",
        smoke_offer_id=None,
        smoke_product_key=None,
        smoke_sku_key=None,
        timeout_seconds=10.0,
        catalog_migration_verify_smoke=False,
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        service_side_data_plane_verify=False,
        search_chain_probe=True,
        search_chain_probe_blocking=False,
        probe_agent_base_url="https://agent.example",
        probe_gateway_url="https://gateway.example/api/gateway",
        probe_source="shopping_agent",
        probe_rounds=1,
        probe_limit=5,
        probe_sleep_ms=0,
        probe_queries=["vitamin c"],
        probe_agent_api_key="agent-key",
        probe_gateway_api_key="gateway-key",
        header=[],
        smoke_header=[],
        release_gate_default_rollout_mode="shadow",
        skip_backfill_apply=True,
        skip_backfill_verify=True,
        skip_release_gate=False,
        skip_smoke=False,
        skip_evidence=False,
    )
    Path(args.corpus).write_text("[]", encoding="utf-8")

    report, exit_code = module._run_bundle(args, runner=fake_runner)

    assert exit_code == 0
    assert report["overall_ok"] is True
    probe_step = next(step for step in report["steps"] if step["name"] == "search_chain_inventory_probe")
    assert probe_step["ok"] is False
    assert probe_step["blocking"] is False


def test_run_bundle_can_split_release_gate_and_smoke_base_urls(tmp_path: Path) -> None:
    recorded = []

    def fake_runner(script_path: Path, script_args: list[str]) -> dict:
        recorded.append((script_path.name, list(script_args)))
        output_json = None
        output_md = None
        for index, token in enumerate(script_args):
            if token == "--output-json":
                output_json = Path(script_args[index + 1])
            if token == "--output-md":
                output_md = Path(script_args[index + 1])
        if output_md:
            output_md.parent.mkdir(parents=True, exist_ok=True)
            output_md.write_text("# stub\n", encoding="utf-8")
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            if script_path.name == "pivot_multi_release_gate.py":
                payload = {"summary": {"failed_cases": 0, "passed_cases": 1}}
            elif script_path.name == "smoke_catalog_pivot_v1.py":
                payload = {"overall_ok": True, "steps": []}
            else:
                payload = {"ok": True}
            output_json.write_text(json.dumps(payload), encoding="utf-8")
        return _result()

    args = argparse.Namespace(
        base_url="https://default.example",
        release_gate_base_url="https://public.example",
        smoke_base_url="https://direct.example",
        corpus=str(tmp_path / "corpus.json"),
        merchant_ids=["merch_a"],
        smoke_merchant_id="merch_a",
        label="bundle-test",
        output_dir=str(tmp_path / "out"),
        migration_artifact=None,
        migration_mode="skip",
        database_url="",
        backfill_platform=None,
        backfill_limit=10,
        backfill_include_expired=False,
        smoke_query="vitamin c",
        smoke_offer_id=None,
        smoke_product_key=None,
        smoke_sku_key=None,
        timeout_seconds=10.0,
        catalog_migration_verify_smoke=False,
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        service_side_data_plane_verify=False,
        search_chain_probe=False,
        search_chain_probe_blocking=False,
        probe_agent_base_url=None,
        probe_gateway_url=None,
        probe_source="shopping_agent",
        probe_rounds=1,
        probe_limit=5,
        probe_sleep_ms=0,
        probe_queries=None,
        probe_agent_api_key="",
        probe_gateway_api_key="",
        header=["X-Test: shared"],
        smoke_header=["Authorization: Bearer smoke"],
        release_gate_default_rollout_mode="shadow",
        skip_backfill_apply=True,
        skip_backfill_verify=True,
        skip_release_gate=False,
        skip_smoke=False,
        skip_evidence=True,
    )
    Path(args.corpus).write_text("[]", encoding="utf-8")

    report, exit_code = module._run_bundle(args, runner=fake_runner)

    assert exit_code == 0
    assert report["release_gate_base_url"] == "https://public.example"
    assert report["smoke_base_url"] == "https://direct.example"
    gate_call = next(script_args for name, script_args in recorded if name == "pivot_multi_release_gate.py")
    smoke_call = next(script_args for name, script_args in recorded if name == "smoke_catalog_pivot_v1.py")
    assert gate_call[gate_call.index("--base-url") + 1] == "https://public.example"
    assert smoke_call[smoke_call.index("--base-url") + 1] == "https://direct.example"
    assert "Authorization: Bearer smoke" in smoke_call


def test_run_bundle_can_include_beauty_ranking_audit_and_compare(tmp_path: Path) -> None:
    recorded = []

    def fake_runner(script_path: Path, script_args: list[str]) -> dict:
        recorded.append((script_path.name, list(script_args)))
        output_json = None
        output_md = None
        for index, token in enumerate(script_args):
            if token == "--output-json":
                output_json = Path(script_args[index + 1])
            if token == "--output-md":
                output_md = Path(script_args[index + 1])
        if output_md:
            output_md.parent.mkdir(parents=True, exist_ok=True)
            output_md.write_text("# stub\n", encoding="utf-8")
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            if script_path.name == "pivot_multi_release_gate.py":
                payload = {"summary": {"failed_cases": 0, "passed_cases": 4}}
            elif script_path.name == "smoke_catalog_pivot_v1.py":
                payload = {"overall_ok": True, "steps": []}
            elif script_path.name == "beauty_ranking_audit.py":
                payload = {
                    "summary": {
                        "case_count": 10,
                        "gateway_top1_matches": 7,
                        "gateway_top1_evaluable": 10,
                    }
                }
            elif script_path.name == "compare_beauty_ranking_audit.py":
                payload = {
                    "summary": {
                        "top1_match_delta": 4,
                        "improved_query_count": 4,
                        "regressed_query_count": 0,
                    }
                }
            else:
                payload = {"ok": True}
            output_json.write_text(json.dumps(payload), encoding="utf-8")
        return _result()

    before_json = tmp_path / "beauty-before.json"
    before_json.write_text(json.dumps({"summary": {"gateway_top1_matches": 3}}), encoding="utf-8")

    args = argparse.Namespace(
        base_url="https://default.example",
        release_gate_base_url="https://public.example",
        smoke_base_url="https://direct.example",
        corpus=str(tmp_path / "corpus.json"),
        merchant_ids=["merch_a"],
        smoke_merchant_id="merch_a",
        label="bundle-test",
        output_dir=str(tmp_path / "out"),
        migration_artifact=None,
        migration_mode="skip",
        database_url="postgresql://bundle-db",
        backfill_platform=None,
        backfill_limit=10,
        backfill_include_expired=False,
        smoke_query="vitamin c",
        smoke_offer_id=None,
        smoke_product_key=None,
        smoke_sku_key=None,
        timeout_seconds=10.0,
        catalog_migration_verify_smoke=False,
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        service_side_data_plane_verify=False,
        search_chain_probe=False,
        search_chain_probe_blocking=False,
        probe_agent_base_url=None,
        probe_gateway_url=None,
        probe_source="shopping_agent",
        probe_rounds=1,
        probe_limit=5,
        probe_sleep_ms=0,
        probe_queries=None,
        probe_agent_api_key="",
        probe_gateway_api_key="",
        beauty_ranking_audit=True,
        beauty_ranking_audit_blocking=True,
        beauty_ranking_audit_compare_before_json=str(before_json),
        beauty_ranking_audit_compare_before_label="before",
        beauty_ranking_audit_compare_after_label="after",
        beauty_ranking_audit_compare_blocking=False,
        beauty_ranking_audit_corpus=None,
        beauty_ranking_audit_market="US",
        beauty_ranking_audit_limit=25,
        beauty_ranking_audit_gateway_base_url=None,
        beauty_ranking_audit_pivot_base_url=None,
        beauty_ranking_audit_database_url=None,
        beauty_ranking_audit_db_mode="sync",
        beauty_ranking_audit_seed_fetch_mode="fast",
        beauty_ranking_audit_timeout_seconds=6.0,
        beauty_ranking_audit_stage_a_timeout_seconds=0.9,
        beauty_ranking_audit_stage_b_timeout_seconds=1.6,
        beauty_ranking_audit_header=["X-Audit: shared"],
        beauty_ranking_audit_gateway_header=["Authorization: Bearer gateway"],
        beauty_ranking_audit_pivot_header=["Authorization: Bearer pivot"],
        header=["X-Test: shared"],
        smoke_header=["Authorization: Bearer smoke"],
        release_gate_default_rollout_mode="shadow",
        skip_backfill_apply=True,
        skip_backfill_verify=True,
        skip_release_gate=False,
        skip_smoke=False,
        skip_evidence=False,
    )
    Path(args.corpus).write_text("[]", encoding="utf-8")

    report, exit_code = module._run_bundle(args, runner=fake_runner)

    assert exit_code == 0
    assert report["overall_ok"] is True
    called_scripts = [name for name, _ in recorded]
    assert "beauty_ranking_audit.py" in called_scripts
    assert "compare_beauty_ranking_audit.py" in called_scripts

    audit_step = next(step for step in report["steps"] if step["name"] == "beauty_ranking_audit")
    compare_step = next(step for step in report["steps"] if step["name"] == "compare_beauty_ranking_audit")
    assert audit_step["blocking"] is True
    assert compare_step["blocking"] is False

    audit_call = next(script_args for name, script_args in recorded if name == "beauty_ranking_audit.py")
    compare_call = next(script_args for name, script_args in recorded if name == "compare_beauty_ranking_audit.py")
    evidence_call = next(script_args for name, script_args in recorded if name == "build_pivot_release_evidence.py")
    assert audit_call[audit_call.index("--gateway-base-url") + 1] == "https://public.example"
    assert audit_call[audit_call.index("--pivot-base-url") + 1] == "https://direct.example"
    assert audit_call[audit_call.index("--database-url") + 1] == "postgresql://bundle-db"
    assert "--market" in audit_call
    assert "Authorization: Bearer gateway" in audit_call
    assert "Authorization: Bearer pivot" in audit_call
    assert compare_call[compare_call.index("--before-json") + 1] == str(before_json)
    assert "--beauty-ranking-audit-json" in evidence_call
    assert "--beauty-ranking-audit-compare-json" in evidence_call
