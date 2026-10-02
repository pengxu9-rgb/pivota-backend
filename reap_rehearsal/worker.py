"""A single source poller invocation, no scheduler or provider egress."""


async def tick():
    from .guard import require_runtime_receipt, run_guard

    receipt = require_runtime_receipt()  # must precede every application import
    if receipt.get("fixture_stage") != "prepared":
        from .guard import reject

        reject("owned_tick_requires_next_phase_review")
    from dataclasses import asdict
    from db.database import database
    from jobs.reap_agentic_purchase_poll import run_reap_agentic_purchase_poll

    await run_guard("runtime")
    await database.connect()
    try:
        # Exactly one call; never installs scheduler. Credentials absent, gates0, DB-only sockets.
        return {
            "preparation_only": True,
            "scheduler_started": False,
            "report": asdict(await run_reap_agentic_purchase_poll(worker_id="rehearsal-preparation-once")),
        }
    finally:
        await database.disconnect()
