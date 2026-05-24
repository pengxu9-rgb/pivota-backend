from __future__ import annotations

import argparse

import pytest

import scripts.recompute_index_serving_eligibility as module


def _args(**overrides):
    values = {
        "apply": False,
        "confirm": "",
        "limit": 600,
        "batch_size": 500,
        "content_key": "",
        "sample_limit": 20,
        "postcheck": True,
        "postcheck_limit": 600,
        "stream_pages": False,
        "page_retries": 2,
        "start_cursor": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.asyncio
async def test_drive_dry_run_reports_violations_without_apply(monkeypatch):
    apply_calls = []

    async def fake_connect(_db):
        return True

    async def fake_disconnect(_db, _was_connected):
        return None

    async def fake_audit(**kwargs):
        assert kwargs["limit"] == 600
        assert kwargs["batch_size"] == 500
        assert kwargs["content_key"] is None
        return [
            {
                "content_key": "ck_1",
                "expected_blocker_code": "entity_unresolved",
                "input_rows": 1,
                "_expected_state": {"content_key": "ck_1"},
            }
        ]

    async def fake_apply(_violation):
        apply_calls.append(_violation)
        return "recomputed"

    monkeypatch.setattr(module, "_connect_if_needed", fake_connect)
    monkeypatch.setattr(module, "_disconnect_if_needed", fake_disconnect)
    monkeypatch.setattr(module, "audit_serving_contract_violations", fake_audit)
    monkeypatch.setattr(module, "_apply_violation", fake_apply)

    report = await module._drive(_args(), db=object())

    assert report["apply"] is False
    assert report["violations_found"] == 1
    assert report["expected_blocker_counts"] == {"entity_unresolved": 1}
    assert "_expected_state" not in report["samples"][0]
    assert apply_calls == []
    assert report["safety"]["price_or_availability_fallbacks"] == 0


@pytest.mark.asyncio
async def test_drive_apply_requires_confirm_token():
    with pytest.raises(SystemExit):
        await module._drive(_args(apply=True, confirm="WRONG"), db=object())


@pytest.mark.asyncio
async def test_drive_apply_recomputes_and_postchecks(monkeypatch):
    apply_calls = []
    audit_calls = []

    async def fake_connect(_db):
        return True

    async def fake_disconnect(_db, _was_connected):
        return None

    async def fake_audit(**kwargs):
        audit_calls.append(kwargs)
        if len(audit_calls) == 1:
            return [
                {
                    "content_key": "ck_1",
                    "expected_blocker_code": "entity_unresolved",
                    "input_rows": 1,
                },
                {
                    "content_key": "ck_2",
                    "expected_blocker_code": "not_live",
                    "input_rows": 0,
                },
            ]
        return []

    async def fake_apply(violation):
        apply_calls.append(violation["content_key"])
        return (
            "fail_closed_no_catalog_inputs"
            if int(violation.get("input_rows") or 0) <= 0
            else "recomputed"
        )

    monkeypatch.setattr(module, "_connect_if_needed", fake_connect)
    monkeypatch.setattr(module, "_disconnect_if_needed", fake_disconnect)
    monkeypatch.setattr(module, "audit_serving_contract_violations", fake_audit)
    monkeypatch.setattr(module, "_apply_violation", fake_apply)

    report = await module._drive(
        _args(apply=True, confirm=module.CONFIRM_TOKEN),
        db=object(),
    )

    assert apply_calls == ["ck_1", "ck_2"]
    assert report["applied"]["recomputed"] == 1
    assert report["applied"]["fail_closed_no_catalog_inputs"] == 1
    assert report["postcheck"]["remaining_violations"] == 0


@pytest.mark.asyncio
async def test_apply_violation_uses_audit_state_without_recompute(monkeypatch):
    upserts = []
    recomputes = []

    async def fake_upsert(content_key, state):
        upserts.append((content_key, state))

    async def fake_recompute(content_key, *, reason=None):
        recomputes.append((content_key, reason))
        return False

    monkeypatch.setattr(module, "upsert_classified_index_pipeline_state", fake_upsert)
    monkeypatch.setattr(module, "recompute_serving_eligibility", fake_recompute)

    outcome = await module._apply_violation(
        {
            "content_key": "ck_1",
            "input_rows": 1,
            "_expected_state": {
                "content_key": "ck_1",
                "blocker_code": "low_quality",
                "serving_eligible": False,
            },
        }
    )

    assert outcome == "applied_audit_state"
    assert upserts == [
        (
            "ck_1",
            {
                "content_key": "ck_1",
                "blocker_code": "low_quality",
                "serving_eligible": False,
            },
        )
    ]
    assert recomputes == []


@pytest.mark.asyncio
async def test_drive_stream_pages_applies_each_page(monkeypatch):
    connect_calls = []
    disconnect_calls = []
    apply_calls = []
    cursors = []

    class FakeDb:
        is_connected = False

        async def connect(self):
            connect_calls.append(True)
            self.is_connected = True

        async def disconnect(self):
            disconnect_calls.append(True)
            self.is_connected = False

    async def fake_page(**kwargs):
        cursors.append(kwargs["cursor"])
        if kwargs["cursor"] == "":
            return {
                "next_cursor": "ck_1",
                "rows_scanned": 1,
                "done": False,
                "violations": [
                    {
                        "content_key": "ck_1",
                        "expected_blocker_code": "low_quality",
                        "input_rows": 1,
                        "_expected_state": {"content_key": "ck_1"},
                    }
                ],
            }
        return {
            "next_cursor": "ck_2",
            "rows_scanned": 1,
            "done": True,
            "violations": [
                {
                    "content_key": "ck_2",
                    "expected_blocker_code": "no_price",
                    "input_rows": 1,
                    "_expected_state": {"content_key": "ck_2"},
                }
            ],
        }

    async def fake_apply(violation):
        apply_calls.append(violation["content_key"])
        return "applied_audit_state"

    monkeypatch.setattr(module, "audit_serving_contract_violation_page", fake_page)
    monkeypatch.setattr(module, "_apply_violation", fake_apply)

    report = await module._drive(
        _args(
            apply=True,
            confirm=module.CONFIRM_TOKEN,
            stream_pages=True,
            postcheck=False,
        ),
        db=FakeDb(),
    )

    assert cursors == ["", "ck_1"]
    assert apply_calls == ["ck_1", "ck_2"]
    assert report["stream_pages"] is True
    assert report["completed_scan"] is True
    assert report["pages_scanned"] == 2
    assert report["rows_scanned"] == 2
    assert report["violations_found"] == 2
    assert report["expected_blocker_counts"] == {
        "low_quality": 1,
        "no_price": 1,
    }
    assert report["applied"]["applied_audit_state"] == 2
    assert "_expected_state" not in report["samples"][0]
    assert len(connect_calls) == 2
    assert len(disconnect_calls) == 2


@pytest.mark.asyncio
async def test_stream_page_retries_connect_failure(monkeypatch):
    connect_attempts = []
    page_calls = []

    class FakeDb:
        is_connected = False

        async def connect(self):
            connect_attempts.append(True)
            if len(connect_attempts) == 1:
                raise ConnectionError("temporary connect loss")
            self.is_connected = True

        async def disconnect(self):
            self.is_connected = False

    async def fake_page(**kwargs):
        page_calls.append(kwargs["cursor"])
        return {
            "next_cursor": "ck_next",
            "rows_scanned": 1,
            "done": True,
            "violations": [],
        }

    monkeypatch.setattr(module, "audit_serving_contract_violation_page", fake_page)

    result = await module._fetch_and_apply_stream_page(
        _args(stream_pages=True, page_retries=1),
        db=FakeDb(),
        cursor="ck_start",
        remaining_limit=0,
    )

    assert len(connect_attempts) == 2
    assert page_calls == ["ck_start"]
    assert result["page"]["next_cursor"] == "ck_next"


@pytest.mark.asyncio
async def test_drive_stream_pages_honors_start_cursor(monkeypatch):
    seen_cursors = []

    class FakeDb:
        is_connected = False

        async def connect(self):
            self.is_connected = True

        async def disconnect(self):
            self.is_connected = False

    async def fake_page(**kwargs):
        seen_cursors.append(kwargs["cursor"])
        return {
            "next_cursor": "ck_after",
            "rows_scanned": 0,
            "done": True,
            "violations": [],
        }

    monkeypatch.setattr(module, "audit_serving_contract_violation_page", fake_page)

    report = await module._drive(
        _args(stream_pages=True, start_cursor="ck_resume"),
        db=FakeDb(),
    )

    assert seen_cursors == ["ck_resume"]
    assert report["start_cursor"] == "ck_resume"
    assert report["completed_scan"] is True
