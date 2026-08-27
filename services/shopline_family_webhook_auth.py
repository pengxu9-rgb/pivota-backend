from __future__ import annotations

import os
from typing import Any, Dict


def resolve_webhook_secret(platform: str, credentials: Dict[str, Any]) -> str:
    """Prefer a per-store secret, with deployment-level public-app fallbacks."""
    stored = str(
        credentials.get("app_secret") or credentials.get("webhook_secret") or ""
    ).strip()
    if stored:
        return stored
    env_names = (
        ("SHOPLINE_APP_SECRET",)
        if platform == "shopline"
        else ("SHOPLAZZA_CLIENT_SECRET", "SHOPLAZZA_APP_SECRET")
    )
    for name in env_names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""
