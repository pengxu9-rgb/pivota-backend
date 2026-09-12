"""Persist a generated answer, measured receipt and debit in one transaction."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from db.database import database
from services import merchant_credit_balance_service as wallet
from services.measured_llm_pricing import price_deepseek_response


MAX_TEXT_CREDITS = Decimal("1")


class MeteringTemporarilyUnavailable(RuntimeError):
    pass


async def generate_measured_text(*, system_prompt: str, user_message: str):
    from config.settings import settings
    from services.llm_providers.deepseek_probe import _call_deepseek_chat, DeepseekProbeError
    key = (settings.deepseek_api_key or "").strip()
    if not key:
        raise DeepseekProbeError("DeepSeek is not configured")
    started = datetime.now(timezone.utc)
    response = await _call_deepseek_chat(
        api_key=key, base_url=settings.deepseek_api_base_url, model="deepseek-flash",
        system_prompt=system_prompt, user_message=user_message,
        timeout_s=25, enable_web_search=False,
    )
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Incomplete model output cannot be billed")
    content = json.loads(choice["message"]["content"])
    answer = content.get("answer") if isinstance(content, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Empty model output cannot be billed")
    return {"answer": answer.strip()}, price_deepseek_response(response, requested_at=started)


async def run_measured_generation(
    *, merchant_id: str, operation_key: str, operation_type: str,
    generate: Callable[[], Awaitable[tuple[dict, dict]]],
    max_credits: Decimal = MAX_TEXT_CREDITS,
    prepare: Callable[[dict], None] | None = None,
):
    """Concurrent retries reuse the persisted result without invoking the model.

    Charge only from an existing balance; no external card charge is initiated
    inside this transaction. A process crash before commit can repeat provider
    work, but cannot leave a debit without the corresponding saved answer.
    """
    if not max_credits.is_finite() or max_credits < 0 or max_credits > MAX_TEXT_CREDITS:
        raise ValueError("Invalid authorized text-generation cap")
    categories = {"ask": "prompt", "action_draft": "prompt", "report_deck_export": "execution"}
    if operation_type not in categories:
        raise ValueError("Unknown measured operation")
    category = categories[operation_type]
    async with database.transaction():
        await database.execute("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                               {"key": f"measured:{merchant_id}:{operation_key}"})
        saved = await database.fetch_one("SELECT result_jsonb,credits FROM merchant_llm_operations WHERE merchant_id=:m AND operation_key=:k",
                                        {"m": merchant_id, "k": operation_key})
        if saved:
            result = saved["result_jsonb"]
            if isinstance(result, str):
                result = json.loads(result)
            if prepare:
                prepare(result)
            return {**result, "credits_charged": 0, "original_credits": float(saved["credits"]), "replay": True}
        enabled = await database.fetch_val("SELECT enabled FROM merchant_metering_controls WHERE policy_version='measured_text_v1'")
        if enabled is not True:
            raise MeteringTemporarilyUnavailable("Measured billing is being updated; retry shortly")
        await wallet.ensure_row(merchant_id, conn=database)
        await wallet.apply_subscription_allowance(merchant_id, conn=database)
        available = await database.fetch_val("SELECT credits FROM merchant_credit_balance WHERE merchant_id=:m FOR UPDATE", {"m": merchant_id})
        if available < max_credits:
            raise wallet.InsufficientCreditsError(merchant_id, category, max_credits, available)
        result, receipt = await generate()
        if prepare:
            prepare(result)
        amount = Decimal(receipt["credits"])
        if not amount.is_finite() or amount < 0 or amount > max_credits:
            raise ValueError("Measured cost exceeds the authorized cap; no charge")
        await wallet.debit(merchant_id, category, amount, "measured:" + operation_key,
                           usd_cogs=receipt["usd_cogs"], conn=database)
        await database.execute("""INSERT INTO merchant_llm_operations
            (merchant_id,operation_key,operation_type,result_jsonb,usage_jsonb,credits)
            VALUES (:m,:k,:t,CAST(:r AS JSONB),CAST(:u AS JSONB),:c)""",
            {"m": merchant_id, "k": operation_key, "t": operation_type,
             "r": json.dumps(result), "u": json.dumps(receipt), "c": amount})
        return {**result, "credits_charged": float(amount), "replay": False}
