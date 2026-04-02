from __future__ import annotations

import argparse
import json
from pathlib import Path

import scripts.smoke_catalog_pivot_v1 as module


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, params=None):
        self.calls.append(("POST", url, headers, json, timeout, params))
        if url.endswith("/v1/pivot/query"):
            return _FakeResponse(
                {
                    "items": [
                        {
                            "product": {"product_key": "prod::1"},
                            "sku": {"sku_key": "sku::1"},
                            "offers": [{"offer_id": "offer::1"}],
                        }
                    ]
                }
            )
        if url.endswith("/v1/pivot/offers/resolve"):
            return _FakeResponse({"offers": [{"offer_id": "offer::1"}]})
        if url.endswith("/v1/pivot/quote"):
            return _FakeResponse({"quote_id": "quote_123"})
        if url.endswith("/admin/migrations/run/058"):
            return _FakeResponse({"mode": "apply-verify", "success": True, "verification": {}})
        if url.endswith("/v1/catalog/connectors/shopify/webhooks"):
            return _FakeResponse({"event_id": "catalog_event_123", "status": "pending"})
        if url.endswith("/v1/catalog/sync/jobs"):
            return _FakeResponse({"job_id": "catalog_job_123", "status": "pending"})
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers, None, timeout, None))
        if url.endswith("/admin/migrations/verify/058"):
            return _FakeResponse(
                {
                    "mode": "verify",
                    "success": True,
                    "verification": {
                        "missing_tables_count": 0,
                        "missing_indexes_count": 0,
                    },
                }
            )
        if url.endswith("/admin/migrations/verify/059"):
            return _FakeResponse(
                {
                    "mode": "verify",
                    "success": True,
                    "verification": {
                        "missing_indexes_count": 0,
                    },
                }
            )
        if url.endswith("/v1/catalog/sync/jobs/catalog_job_123"):
            return _FakeResponse({"job_id": "catalog_job_123", "status": "completed"})
        raise AssertionError(f"unexpected GET {url}")


def test_smoke_catalog_pivot_v1_can_verify_admin_migration(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    args = argparse.Namespace(
        base_url="https://pivot.example",
        merchant_id="merch_1",
        query="vitamin c serum",
        offer_id=None,
        product_key=None,
        sku_key=None,
        skip_pivot_query=False,
        timeout_seconds=20.0,
        header=["Authorization: Bearer test"],
        catalog_migration_verify_smoke=True,
        catalog_migration_run_smoke=False,
        catalog_migration_run_mode="apply-verify",
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        output_json=str(tmp_path / "smoke.json"),
        output_md=str(tmp_path / "smoke.md"),
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    step_names = [step["step"] for step in payload["steps"]]
    assert "pivot_query" in step_names
    assert "pivot_offers_resolve" in step_names
    assert "pivot_quote" in step_names
    assert "admin_catalog_migration_verify_058" in step_names
    assert "admin_catalog_migration_verify_059" in step_names


def test_smoke_catalog_pivot_v1_can_run_migration_and_wait_for_sync_job(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    args = argparse.Namespace(
        base_url="https://pivot.example",
        merchant_id="merch_1",
        query="vitamin c serum",
        offer_id=None,
        product_key=None,
        sku_key=None,
        skip_pivot_query=False,
        timeout_seconds=20.0,
        header=["Authorization: Bearer test"],
        catalog_migration_verify_smoke=True,
        catalog_migration_run_smoke=True,
        catalog_migration_run_mode="apply-verify",
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=True,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=5.0,
        catalog_sync_poll_interval_seconds=0.01,
        output_json=str(tmp_path / "smoke.json"),
        output_md=str(tmp_path / "smoke.md"),
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    step_names = [step["step"] for step in payload["steps"]]
    assert "admin_catalog_migration_run_058" in step_names
    assert "admin_catalog_migration_verify_058" in step_names
    assert "admin_catalog_migration_verify_059" in step_names
    assert "catalog_sync_job_create" in step_names
    assert "catalog_sync_job_final" in step_names


def test_smoke_catalog_pivot_v1_can_skip_pivot_query(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    args = argparse.Namespace(
        base_url="https://pivot.example",
        merchant_id="merch_1",
        query="vitamin c serum",
        offer_id=None,
        product_key=None,
        sku_key=None,
        skip_pivot_query=True,
        timeout_seconds=20.0,
        header=["Authorization: Bearer test"],
        catalog_migration_verify_smoke=False,
        catalog_migration_run_smoke=False,
        catalog_migration_run_mode="apply-verify",
        catalog_webhook_smoke=False,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        output_json=str(tmp_path / "smoke.json"),
        output_md=str(tmp_path / "smoke.md"),
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    step_names = [step["step"] for step in payload["steps"]]
    assert "pivot_query" not in step_names
    assert "pivot_offers_resolve" in step_names
    assert "pivot_quote" in step_names


def test_smoke_catalog_pivot_v1_can_smoke_webhook_ingest(monkeypatch, tmp_path: Path) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(module.requests, "Session", lambda: fake_session)
    args = argparse.Namespace(
        base_url="https://pivot.example",
        merchant_id="merch_1",
        query="vitamin c serum",
        offer_id=None,
        product_key=None,
        sku_key=None,
        skip_pivot_query=False,
        timeout_seconds=20.0,
        header=["Authorization: Bearer test"],
        catalog_migration_verify_smoke=False,
        catalog_migration_run_smoke=False,
        catalog_migration_run_mode="apply-verify",
        catalog_webhook_smoke=True,
        catalog_sync_job_smoke=False,
        catalog_sync_limit=1,
        catalog_sync_wait_seconds=0.0,
        catalog_sync_poll_interval_seconds=2.0,
        output_json=str(tmp_path / "smoke.json"),
        output_md=str(tmp_path / "smoke.md"),
    )
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    step_names = [step["step"] for step in payload["steps"]]
    assert "catalog_webhook_ingest" in step_names
