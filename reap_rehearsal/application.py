"""Minimal Reap routes; production main, light schema DDL and schedulers are never loaded."""


def create_app():
    from .guard import require_runtime_receipt, validate_dsn, TARGET

    receipt = require_runtime_receipt()  # before importing routes/db/FastAPI
    import os
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from db.database import database, DATABASE_URL
    from routes.agent_commerce_reap import router
    from routes.agent_internal_auth import router as internal_auth_router

    if DATABASE_URL != os.environ["DATABASE_URL"]:
        raise RuntimeError("runtime_database_configuration_changed")
    validate_dsn(DATABASE_URL, "runtime", TARGET)

    @asynccontextmanager
    async def lifespan(app):
        from .guard import run_guard

        await run_guard("runtime")
        await database.connect()
        try:
            yield
        finally:
            await database.disconnect()

    app = FastAPI(title="Isolated Reap staging preparation", lifespan=lifespan)
    app.include_router(router)
    app.include_router(internal_auth_router)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "rehearsal": "preparation_only",
            "source_base_commit": receipt.get("source_base_commit"),
            "database_guarded": True,
            "create_enabled": False,
            "provider_egress": False,
            "scheduler_started": False,
        }

    return app
