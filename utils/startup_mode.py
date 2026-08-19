import os
from typing import Mapping, Optional

from config.platform import is_deployed


TRUTHY_VALUES = {"1", "true", "yes"}


def should_skip_heavy_startup(env: Optional[Mapping[str, str]] = None) -> bool:
    values = env or os.environ
    explicit = values.get("SKIP_HEAVY_STARTUP_INIT")
    if explicit is not None:
        return explicit.strip().lower() in TRUTHY_VALUES

    # The question the RAILWAY_ENVIRONMENT / _SERVICE_NAME / _PROJECT_ID /
    # _DEPLOYMENT_ID scan used to ask was "am I on a managed platform" — a
    # deployed service has a healthcheck window that long-running startup DDL
    # blows through. is_deployed() asks it for Cloud Run too, where none of
    # those four variables exist.
    return is_deployed(values)
