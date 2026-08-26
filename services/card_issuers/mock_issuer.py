"""In-memory issuer for development and tests.

REFUSES TO CONSTRUCT IN PRODUCTION, loudly. A mock issuer in prod would mint rows that look like
real spending instruments backed by nothing — the exact shape of the test-PSP lever, but without
its explicit merchant scoping. The guard is on __init__, not issue(): a misconfigured deployment
should die at the first resolve, not at the first customer.
"""

from __future__ import annotations

import secrets
from typing import Dict

from services.card_issuers import CardIssuer, CardIssuerError, IssuedCard, IssueRequest

_PROD_MARKERS = ("production", "prod")


class MockIssuer:
    name = "mock"

    def __init__(self, env: Dict[str, str]):
        for var in ("PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "ENVIRONMENT", "ENV"):
            if str(env.get(var) or "").strip().lower() in _PROD_MARKERS:
                raise CardIssuerError(
                    "MOCK_ISSUER_IN_PRODUCTION",
                    f"CARD_ISSUER=mock is forbidden where {var} says production",
                )
        self.issued: Dict[str, IssueRequest] = {}  # test hook: what was requested, by ref

    async def issue(self, request: IssueRequest) -> IssuedCard:
        ref = f"mockcard_{secrets.token_hex(8)}"
        self.issued[ref] = request
        return IssuedCard(
            issuer_card_ref=ref,
            reveal_handle=f"https://mock.invalid/reveal/{ref}",
        )
