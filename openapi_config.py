"""Custom OpenAPI schema generator used during FastAPI startup."""
from typing import Any, Dict

from config.settings import resolve_public_api_base_url


def get_custom_openapi_schema() -> Dict[str, Any]:
    """Return the minimal structure expected by main.py.

    This keeps the deployment resilient even if a richer investor-facing
    schema is not available in the repository.
    """

    return {
        "info": {
            "title": "Pivota Infrastructure API",
            "version": "1.0.0",
            "description": "Unified payment infrastructure for merchants, agents, and employees.",
            "contact": {
                "name": "Pivota Support",
                "email": "support@pivota.cc",
            },
        },
        "servers": [
            {"url": resolve_public_api_base_url(), "description": "Production"},
        ],
        "security": [],
        "components": {},
        "tags": [],
    }
