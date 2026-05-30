from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def _coerce_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


@pytest.mark.asyncio
async def test_record_probe_run_round_trips_raw_io_and_redacts_request(tmp_path, monkeypatch):
    from databases import Database
    import db.llm_probe_runs as lpr

    test_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'probe_runs.db'}")
    monkeypatch.setattr(lpr, "database", test_db)
    lpr._DDL_READY = False
    await test_db.connect()

    try:
        request_payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "Product description"}],
            "temperature": 0,
            "headers": {
                "Authorization": "Bearer secret-token",
                "content-type": "application/json",
                "x-extra-secret": "drop-me",
            },
            "nested": {
                "api_key": "sk-test",
                "password": "pw",
                "note": "Bearer inline-secret",
            },
        }
        response_payload = {
            "choices": [{"message": {"content": "{\"decision\":\"pass\"}"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "finish_reason": "stop",
        }

        probe_run_id = await lpr.record_probe_run(
            provider="deepseek",
            scan_mode="pdp_copy_review",
            status=lpr.STATUS_SUCCEEDED,
            merchant_id="m-raw",
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.000001"),
            request_payload_jsonb=request_payload,
            response_jsonb=response_payload,
            model="deepseek-chat",
        )
        assert probe_run_id

        row = await test_db.fetch_one(
            """
            SELECT request_payload_jsonb, response_jsonb, model
              FROM llm_probe_runs
             WHERE probe_run_id = :probe_run_id
            """,
            {"probe_run_id": probe_run_id},
        )
        assert row is not None
        stored_request = _coerce_json(row["request_payload_jsonb"])
        stored_response = _coerce_json(row["response_jsonb"])

        assert stored_request["messages"] == request_payload["messages"]
        assert stored_request["headers"] == {"content-type": "application/json"}
        rendered_request = json.dumps(stored_request)
        assert "Authorization" not in rendered_request
        assert "secret-token" not in rendered_request
        assert "sk-test" not in rendered_request
        assert "password" not in rendered_request
        assert "Bearer inline-secret" not in rendered_request
        assert stored_response == response_payload
        assert row["model"] == "deepseek-chat"
    finally:
        lpr._DDL_READY = False
        await test_db.disconnect()
