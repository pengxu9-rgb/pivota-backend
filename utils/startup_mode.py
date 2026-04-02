import os
from typing import Mapping, Optional


TRUTHY_VALUES = {"1", "true", "yes"}


def should_skip_heavy_startup(env: Optional[Mapping[str, str]] = None) -> bool:
    values = env or os.environ
    explicit = values.get("SKIP_HEAVY_STARTUP_INIT")
    if explicit is not None:
        return explicit.strip().lower() in TRUTHY_VALUES

    railway_env = (values.get("RAILWAY_ENVIRONMENT") or "").strip()
    if railway_env:
        return True

    return any(
        bool((values.get(key) or "").strip())
        for key in (
            "RAILWAY_SERVICE_NAME",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_DEPLOYMENT_ID",
        )
    )
