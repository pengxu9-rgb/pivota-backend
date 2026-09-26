import pytest


@pytest.mark.asyncio
async def test_update_payment_info_preserves_long_adyen_session_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db import orders as module

    captured = {"params": None, "schema_probe_count": 0}
    long_secret = "A" * 1521

    async def fake_ensure() -> None:
        captured["schema_probe_count"] += 1

    async def fake_execute(query):
        compiled = query.compile()
        captured["params"] = dict(compiled.params)
        return 1

    monkeypatch.setattr(
        module,
        "_ensure_client_secret_storage_allows_long_values",
        fake_ensure,
    )
    monkeypatch.setattr(module.database, "execute", fake_execute)

    ok = await module.update_payment_info(
        order_id="ORD_ADYEN_LONG_SECRET",
        payment_intent_id="adyen_session_test",
        client_secret=long_secret,
        payment_status="awaiting_payment",
        psp_used="adyen",
    )

    assert ok is True
    assert captured["schema_probe_count"] == 1
    assert captured["params"]["client_secret"] == long_secret


@pytest.mark.asyncio
async def test_update_payment_info_keeps_short_secret_without_schema_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db import orders as module

    captured = {"params": None, "schema_probe_count": 0}
    short_secret = "pi_short_secret"

    async def fake_ensure() -> None:
        captured["schema_probe_count"] += 1

    async def fake_execute(query):
        compiled = query.compile()
        captured["params"] = dict(compiled.params)
        return 1

    monkeypatch.setattr(
        module,
        "_ensure_client_secret_storage_allows_long_values",
        fake_ensure,
    )
    monkeypatch.setattr(module.database, "execute", fake_execute)

    ok = await module.update_payment_info(
        order_id="ORD_STRIPE_SHORT_SECRET",
        payment_intent_id="pi_short",
        client_secret=short_secret,
        payment_status="awaiting_payment",
        psp_used="stripe",
    )

    assert ok is True
    assert captured["schema_probe_count"] == 0
    assert captured["params"]["client_secret"] == short_secret


@pytest.mark.asyncio
async def test_a_client_secret_heal_that_timed_out_is_retried_on_a_later_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heal runs under a 500ms lock_timeout, so it can now fail where it used to wait.
    A failed run must not mark the process healed, or it never retries."""
    from db import orders as module

    statements: list = []
    failing = {"on": True}

    async def fake_execute(query):
        statements.append(" ".join(str(query).split()))
        if failing["on"] and "ALTER COLUMN client_secret TYPE TEXT" in str(query):
            raise RuntimeError("canceling statement due to lock timeout")
        return None

    monkeypatch.setattr(module, "IS_POSTGRES", True)
    monkeypatch.setattr(module, "_client_secret_storage_ready", False)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    await module._ensure_client_secret_storage_allows_long_values()
    assert module._client_secret_storage_ready is False
    assert len(statements) == 2
    assert all(s.startswith("DO $") for s in statements), "both statements go out guarded"

    failing["on"] = False
    await module._ensure_client_secret_storage_allows_long_values()
    assert module._client_secret_storage_ready is True
    assert len(statements) == 4

    await module._ensure_client_secret_storage_allows_long_values()
    assert len(statements) == 4, "healed: no further statements this process"
