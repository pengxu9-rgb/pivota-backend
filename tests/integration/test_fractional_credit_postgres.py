"""Real wallet SQL, migration 222, and isolated local PostgreSQL only."""
import asyncio
from contextvars import Context
from decimal import Decimal
from datetime import timedelta
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_RECOVERY_POSTGRES") != "1", reason="isolated PostgreSQL required")


@pytest.mark.parametrize("scenario", ["expired", "future", "live", "fractional", "concurrent", "rollback", "mixed_refund"])
async def test_credit_contract(scenario):
    from db.database import database
    from services import merchant_credit_balance_service as wallet
    from services.credit_consumption_service import merchant_is_paid_tier
    url = urlparse(os.environ["DATABASE_URL"])
    assert url.hostname in {"127.0.0.1", "localhost"} and url.path == "/recovery_contract_test"
    merchant = "credit-" + uuid4().hex
    await database.connect()
    try:
        await database.execute("INSERT INTO merchant_onboarding (merchant_id) VALUES (:m)", {"m": merchant})
        await database.execute("INSERT INTO merchant_credit_balance (merchant_id,credits,purchased_credits,allowance_credits,plan_tier) VALUES (:m, :n, 1000, :a, :p)",
                               {"m": merchant, "n": 4997 if scenario == "expired" else 1000, "a": 4000 if scenario == "expired" else 0, "p": "starter" if scenario == "expired" else "free"})
        if scenario in {"expired", "future", "live"}:
            plan = await database.fetch_val("INSERT INTO subscription_plans (name,tier_level,monthly_credit_allowance) VALUES ('starter',1,4000) ON CONFLICT(name) DO UPDATE SET monthly_credit_allowance=4000 RETURNING id")
            period = {"expired": (-60, -30), "future": (1, 31), "live": (-1, 29)}[scenario]
            await database.execute("INSERT INTO user_subscriptions (merchant_id,plan_id,status,current_period_start,current_period_end) VALUES (:m,:p,'active',NOW()+CAST(:s AS interval),NOW()+CAST(:e AS interval))",
                                   {"m": merchant, "p": plan, "s": timedelta(days=period[0]), "e": timedelta(days=period[1])})
            first = await wallet.apply_subscription_allowance(merchant)
            second = await wallet.apply_subscription_allowance(merchant)
            assert first["credits"] == second["credits"] == (5000 if scenario == "live" else 1000)
            assert await merchant_is_paid_tier(merchant) is (scenario == "live")
            adjustment = await database.fetch_one("SELECT * FROM merchant_credit_adjustments WHERE merchant_id=:m", {"m": merchant})
            assert bool(adjustment) is (scenario == "expired")
            if adjustment:
                assert adjustment["credits_before"] == 4997 and adjustment["credits_after"] == 1000
        else:
            async def debit(key):
                return await wallet.debit(merchant, "prompt", Decimal("0.0672"), key)
            if scenario == "mixed_refund":
                await database.execute("UPDATE merchant_credit_balance SET credits=1,purchased_credits=0.7 WHERE merchant_id=:m", {"m": merchant})
                debit_result = await wallet.debit(merchant, "audit", 1, "mixed")
                assert debit_result["purchased_credits_debited"] == Decimal("0.7")
                await wallet.credit(merchant, "audit", 1, "mixed-refund", purchased_credits=debit_result["purchased_credits_debited"])
                restored = await wallet.get_balance(merchant)
                assert restored["credits"] == 1 and restored["purchased_credits"] == Decimal("0.7")
                return
            if scenario == "concurrent":
                results = await asyncio.wait_for(asyncio.gather(*(asyncio.create_task(debit("same"), context=Context()) for _ in range(2))), 10)
                assert sorted(r["replay"] for r in results) == [False, True]
                expected = Decimal("999.9328")
            elif scenario == "rollback":
                with pytest.raises(RuntimeError):
                    async with database.transaction():
                        await debit("rollback")
                        raise RuntimeError("fail after debit")
                expected = Decimal("1000")
            else:
                for n in range(10):
                    await debit(str(n))
                expected = Decimal("999.328")
            balance = await wallet.get_balance(merchant)
            assert balance["credits"] == balance["purchased_credits"] == expected
            spent = await database.fetch_val("SELECT COALESCE(SUM(quantity),0) FROM agent_center_usage_events WHERE merchant_id=:m", {"m": merchant})
            assert spent == Decimal("1000") - expected
    finally:
        for table in ("merchant_credit_adjustments", "agent_center_usage_events", "user_subscriptions", "merchant_onboarding"):
            await database.execute(f"DELETE FROM {table} WHERE merchant_id=:m", {"m": merchant})
        await database.disconnect()


@pytest.mark.parametrize("scenario", ["replay", "concurrent", "provider_failure", "over_cap", "save_failure", "render_failure", "disabled"])
async def test_measured_result_and_debit_commit_together(scenario):
    from db.database import database
    from services.merchant_measured_generation import run_measured_generation
    url = urlparse(os.environ["DATABASE_URL"])
    assert url.hostname in {"127.0.0.1", "localhost"} and url.path == "/recovery_contract_test"
    merchant = "measured-" + uuid4().hex
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        if scenario == "provider_failure":
            raise RuntimeError("provider failed")
        return {"answer": "OK"}, {"credits": "2" if scenario == "over_cap" else "0.00552", "usd_cogs": "0.0000345"}

    def prepare(result):
        if scenario == "render_failure":
            raise ValueError("PPT renderer failed")
    async def run():
        return await run_measured_generation(merchant_id=merchant, operation_key="same", operation_type="ask", generate=generate, prepare=prepare)

    await database.connect()
    try:
        await database.execute("INSERT INTO merchant_onboarding (merchant_id) VALUES (:m)", {"m": merchant})
        await database.execute("INSERT INTO merchant_credit_balance (merchant_id,credits,purchased_credits) VALUES (:m,10,10)", {"m": merchant})
        if scenario == "disabled":
            await database.execute("UPDATE merchant_metering_controls SET enabled=FALSE WHERE policy_version='measured_text_v1'")
        if scenario == "concurrent":
            result = await asyncio.wait_for(asyncio.gather(*(asyncio.create_task(run(), context=Context()) for _ in range(2))), 10)
            assert sorted(r["credits_charged"] for r in result) == [0, 0.00552]
        elif scenario == "replay":
            assert (await run())["credits_charged"] == 0.00552
            assert (await run())["credits_charged"] == 0
        elif scenario == "save_failure":
            with pytest.raises(RuntimeError):
                async with database.transaction():
                    await run()
                    raise RuntimeError("failure after saving result")
        else:
            with pytest.raises((RuntimeError, ValueError)):
                await run()
        assert calls == (0 if scenario == "disabled" else 1)
        success = scenario in {"replay", "concurrent"}
        assert await database.fetch_val("SELECT credits FROM merchant_credit_balance WHERE merchant_id=:m", {"m": merchant}) == Decimal("9.99448" if success else "10")
        for table in ("agent_center_usage_events", "merchant_llm_operations"):
            assert await database.fetch_val(f"SELECT COUNT(*) FROM {table} WHERE merchant_id=:m", {"m": merchant}) == int(success)
    finally:
        await database.execute("UPDATE merchant_metering_controls SET enabled=TRUE WHERE policy_version='measured_text_v1'")
        for table in ("merchant_llm_operations", "agent_center_usage_events", "merchant_onboarding"):
            await database.execute(f"DELETE FROM {table} WHERE merchant_id=:m", {"m": merchant})
        await database.disconnect()


@pytest.mark.parametrize('task_fails', [False, True])
async def test_action_route_task_failure_rolls_back_measured_charge(monkeypatch, task_fails):
    from db.database import database
    from db import merchant_tasks
    from routes import merchant_audit_routes as routes
    url = urlparse(os.environ['DATABASE_URL'])
    assert url.hostname in {'127.0.0.1', 'localhost'} and url.path == '/recovery_contract_test'
    merchant = 'action-' + uuid4().hex
    async def fetch(**kw):
        return {'merchant_id': merchant, 'report_jsonb': {'per_sku_reports': []}}
    async def candidates(**kw):
        return []
    async def generate(**kw):
        return {'answer': 'Measured draft'}, {'credits': '0.00552', 'usd_cogs': '0.0000345'}
    async def task(**kw):
        if task_fails:
            return None
        return 'task-created'
    monkeypatch.setattr(routes, 'fetch_audit_run_by_id', fetch)
    monkeypatch.setattr(routes, '_build_ask_context', lambda *a: {'context': 'report'})
    monkeypatch.setattr(routes, 'generate_measured_text', generate)
    monkeypatch.setattr(merchant_tasks, 'find_pending_supersede_candidates', candidates)
    monkeypatch.setattr(merchant_tasks, 'record_task_created', task)
    await database.connect()
    try:
        await database.execute('INSERT INTO merchant_onboarding (merchant_id) VALUES (:m)', {'m': merchant})
        await database.execute('INSERT INTO merchant_credit_balance (merchant_id,credits,purchased_credits) VALUES (:m,10,10)', {'m': merchant})
        body = routes.MerchantAuditActionStartRequest(run_id='run-12345678', headline='Create comparison page')
        if task_fails:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as error:
                await routes.start_merchant_audit_action(body, merchant_id=merchant)
            assert error.value.status_code == 500
        else:
            result = await routes.start_merchant_audit_action(body, merchant_id=merchant)
            assert result['draft'] == 'Measured draft'
            assert result['credits_charged'] == 0.00552
        assert await database.fetch_val('SELECT credits FROM merchant_credit_balance WHERE merchant_id=:m', {'m': merchant}) == Decimal('10' if task_fails else '9.99448')
        for table in ('agent_center_usage_events', 'merchant_llm_operations'):
            assert await database.fetch_val(f'SELECT COUNT(*) FROM {table} WHERE merchant_id=:m', {'m': merchant}) == int(not task_fails)
    finally:
        for table in ('merchant_llm_operations', 'agent_center_usage_events', 'merchant_onboarding'):
            await database.execute(f'DELETE FROM {table} WHERE merchant_id=:m', {'m': merchant})
        await database.disconnect()
