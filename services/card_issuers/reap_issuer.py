"""Reap adapter — mints a constrained virtual card via Reap's issuing API.

⚠️ WIRE FORMAT NOT YET VERIFIED AGAINST REAP. The request/response mapping below is the
adapter's best-understood shape and is deliberately confined to _build_payload / _parse_response
so that aligning it with Reap's actual API (and their sandbox) is a two-function change. Until
that verification happens, run this rail with CARD_ISSUER=mock outside of Reap-sandbox testing.

TWO RULES THIS FILE MUST KEEP no matter how the wire format moves:

1. THE RESPONSE IS PARSED BY ALLOWLIST AND THEN DROPPED. An issuing API's card object can carry
   the PAN. _parse_response extracts the card id and the hosted reveal handle and returns ONLY
   those; the raw body is never stored, never logged, never attached to an exception. The
   logger here records status codes and our own card_id — nothing from the provider body.

2. FAIL CLOSED ON CONFIG. Missing base URL or key raises at construction; there is no
   "unauthenticated sandbox default".
"""

from __future__ import annotations

from typing import Any, Dict

import httpx

from services.card_issuers import CardIssuerError, IssuedCard, IssueRequest
from utils.logger import logger

_TIMEOUT_SECONDS = 15.0


class ReapIssuer:
    name = "reap"

    def __init__(self, env: Dict[str, str]):
        self.base_url = str(env.get("REAP_API_BASE") or "").strip().rstrip("/")
        self.api_key = str(env.get("REAP_API_KEY") or "").strip()
        self.issue_path = str(env.get("REAP_ISSUE_CARD_PATH") or "/v1/cards").strip()
        if not self.base_url or not self.api_key:
            raise CardIssuerError(
                "REAP_UNCONFIGURED", "REAP_API_BASE and REAP_API_KEY are required for CARD_ISSUER=reap"
            )

    def _build_payload(self, request: IssueRequest) -> Dict[str, Any]:
        # The constraints ARE the product: cap, merchant lock, single use, expiry. Everything
        # else is reconciliation metadata (our card_id comes back on Reap's webhooks).
        return {
            "type": "virtual",
            "single_use": request.single_use,
            "spend_limit": {"amount": request.amount_cap_minor, "currency": request.currency},
            "merchant_restriction": {"domains": [request.merchant_domain]},
            "expires_at": request.expires_at.isoformat(),
            "metadata": {"pivota_card_id": request.card_id, **request.metadata},
        }

    @staticmethod
    def _parse_response(body: Dict[str, Any]) -> IssuedCard:
        card = body.get("card") if isinstance(body.get("card"), dict) else body
        ref = str(card.get("id") or card.get("card_id") or "").strip()
        if not ref:
            raise CardIssuerError("REAP_BAD_RESPONSE", "issuer response carried no card id")
        reveal = card.get("reveal_url") or card.get("secure_details_url") or card.get("reveal_token")
        return IssuedCard(issuer_card_ref=ref, reveal_handle=str(reveal) if reveal else None)

    async def issue(self, request: IssueRequest) -> IssuedCard:
        url = f"{self.base_url}{self.issue_path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    url,
                    json=self._build_payload(request),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as err:
            logger.warning(f"reap issue transport failure card_id={request.card_id}: {type(err).__name__}")
            raise CardIssuerError("REAP_UNREACHABLE", "issuer request failed in transport") from err
        if resp.status_code >= 300:
            # Status only — the body may describe (or contain) card data.
            logger.warning(f"reap issue refused card_id={request.card_id} status={resp.status_code}")
            raise CardIssuerError("REAP_REFUSED", f"issuer returned HTTP {resp.status_code}")
        try:
            return self._parse_response(resp.json())
        except CardIssuerError:
            raise
        except Exception as err:
            raise CardIssuerError("REAP_BAD_RESPONSE", "issuer response was not parseable") from err
