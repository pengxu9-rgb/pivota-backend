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
