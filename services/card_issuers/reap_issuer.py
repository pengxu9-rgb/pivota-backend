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
        # `{card_id}` is substituted at call time. Configurable for the same reason issue_path
        # is: the real shape is unverified and must be correctable without a code change.
        self.revoke_path = str(
            env.get("REAP_REVOKE_CARD_PATH") or "/v1/cards/{card_id}/cancel"
        ).strip()
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

    @staticmethod
    def _verify_constraints(body: Dict[str, Any], request: IssueRequest) -> None:
        """Refuse unless the issuer CONFIRMS the constraints we asked for.

        WHY THIS EXISTS. Without it, "constrain-at-mint" is an assumption, not a mechanism. A
        REST API that does not recognise a field name normally ignores it and answers 2xx — so a
        wrong spelling of `spend_limit` or `merchant_restriction` produces a successful mint of
        an UNCAPPED, UNLOCKED card, while we write `amount_cap_minor` into `agent_issued_cards`
        and hand the agent a working reveal handle. Every alarm downstream is keyed on that cap,
        so nothing would ever fire. The wire format here is explicitly UNVERIFIED against Reap
        (see the module docstring), which is exactly the condition under which a silent
        constraint drop is likely rather than hypothetical.

        SO THIS FAILS CLOSED IN BOTH DIRECTIONS — contradicted AND merely unconfirmed. An
        issuer that does not echo the constraints leaves us unable to state the one fact this
        rail is built on, and "we could not tell" must not read as "it is capped". During
        sandbox verification this is a feature: the first real Reap call reports precisely which
        constraint it would not confirm, instead of appearing to work.

        ⚠️ A REFUSAL HERE DOES NOT UN-MINT THE CARD. We are past a 2xx, so the card may exist at
        the issuer — possibly the uncapped one this check is about. So the error raised out of
        `issue()` CARRIES the ref, the caller persists it on the failed row, and
        jobs/agent_card_revocation_sweep.py kills it. That path only works because the ref is on
        the row: a log line is not a work queue.

        NOTHING FROM THE BODY IS ECHOED into the message or the log — rule 1 of this module. That
        includes the observed amounts: a mis-mapped field could put a PAN where an integer was
        expected, and digits are exactly what we would be tempted to print. The constraint NAME
        plus our own expected value is enough to act on.
        """
        card = body.get("card") if isinstance(body.get("card"), dict) else body

        def _fail(code: str, constraint: str) -> None:
            raise CardIssuerError(code, f"issuer did not confirm the {constraint} we requested")

        # Spelling sets are deliberately TIGHT — the same tolerance `_parse_response` allows for
        # `id`/`card_id`, and no more. A wide search is how a check starts matching a field that
        # means something else and reporting a confirmation nobody made.
        limit = None
        for key in ("spend_limit", "spending_limit", "limit"):
            if isinstance(card.get(key), dict):
                limit = card[key]
                break
        if limit is None:
            _fail("REAP_CONSTRAINTS_UNCONFIRMED", "spend cap")

        # STRICT equality against the integer we sent. A decimal string ("23.00") is NOT coerced:
        # whether that means 23 or 2300 is exactly the ambiguity that must not be resolved by
        # guessing on a spending cap.
        observed = limit.get("amount")
        if isinstance(observed, bool) or not isinstance(observed, int):
            if isinstance(observed, str) and observed.strip().isdigit():
                observed = int(observed.strip())
            else:
                _fail("REAP_CONSTRAINTS_UNCONFIRMED", "spend cap")
        if observed != request.amount_cap_minor:
            _fail("REAP_CONSTRAINTS_MISMATCH", "spend cap")

        observed_currency = limit.get("currency")
        if not isinstance(observed_currency, str) or not observed_currency.strip():
            _fail("REAP_CONSTRAINTS_UNCONFIRMED", "cap currency")
        if observed_currency.strip().upper() != str(request.currency).strip().upper():
            _fail("REAP_CONSTRAINTS_MISMATCH", "cap currency")

        # The merchant lock is what bounds the blast radius to ONE merchant. An unlocked card
        # carrying our cap is still a card an agent can spend anywhere.
        restriction = None
        for key in ("merchant_restriction", "merchant_restrictions"):
            if isinstance(card.get(key), dict):
                restriction = card[key]
                break
        if restriction is None:
            _fail("REAP_CONSTRAINTS_UNCONFIRMED", "merchant restriction")
        domains = restriction.get("domains")
        if not isinstance(domains, list) or not domains:
            _fail("REAP_CONSTRAINTS_UNCONFIRMED", "merchant restriction")
        wanted = str(request.merchant_domain).strip().lower()
        if [d for d in domains if isinstance(d, str) and d.strip().lower() == wanted] == []:
            _fail("REAP_CONSTRAINTS_MISMATCH", "merchant restriction")
        # A lock that ALSO admits other merchants is not the lock we asked for.
        if len(domains) != 1:
            _fail("REAP_CONSTRAINTS_MISMATCH", "merchant restriction")

        # Only asserted when we ASKED for single-use: a card we never scoped that way has no
        # confirmation to give, and demanding one would refuse a correct response.
        if request.single_use and card.get("single_use") is not True:
            _fail("REAP_CONSTRAINTS_UNCONFIRMED", "single-use scope")

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
            body = resp.json()
            issued = self._parse_response(body)
        except CardIssuerError:
            raise
        except Exception as err:
            raise CardIssuerError("REAP_BAD_RESPONSE", "issuer response was not parseable") from err

        # PARSE FIRST, VERIFY SECOND — deliberately in this order. A card that fails verification
        # may exist at the issuer, and `issuer_card_ref` is the only handle anything has to go
        # revoke it. The caller marks its row failed without storing the ref, so if we refused
        # before extracting it the orphan would be unreachable. The ref is already persisted on
        # the success path, so logging it here reveals nothing new.
        try:
            self._verify_constraints(body, request)
        except CardIssuerError as err:
            logger.error(
                "reap issue CONSTRAINTS UNCONFIRMED card_id=%s issuer_card_ref=%s code=%s — "
                "a card may exist at the issuer with constraints we could not confirm; revoke it",
                request.card_id,
                issued.issuer_card_ref,
                err.code,
            )
            # Re-raised CARRYING the ref so the caller can persist it. The log above is for a
            # human; this is for the revocation sweep, which reads rows and not logs.
            raise CardIssuerError(
                err.code, str(err), issuer_card_ref=issued.issuer_card_ref
            ) from err
        return issued

    async def revoke(self, issuer_card_ref: str) -> None:
        """Kill a card at the issuer, and refuse to claim success without confirmation.

        ⚠️ WIRE FORMAT UNVERIFIED, same as issuance — and the failure mode here is nastier. If
        `revoke_path` is wrong, every call 404s. Treating 404 as "already gone, nothing to do"
        would then mark the entire orphan backlog revoked while not one card had been killed:
        a silent success that erases the very evidence the sweep exists to act on. So a 404 is
        a FAILURE here, deliberately, and no status code alone is ever taken as confirmation.

        Only an affirmative dead-state in the body counts. Unbounded retry is intended: the
        sweep will keep trying a card it cannot confirm dead, because giving up quietly on a
        possibly-spendable uncapped card is the worse outcome. The alarm is the escalation path,
        not a retry limit.
        """
        ref = str(issuer_card_ref or "").strip()
        if not ref:
            raise CardIssuerError("REAP_REVOKE_BAD_REQUEST", "no issuer card ref to revoke")
        url = f"{self.base_url}{self.revoke_path.replace('{card_id}', ref)}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as err:
            raise CardIssuerError("REAP_UNREACHABLE", "revoke request failed in transport") from err
        if resp.status_code >= 300:
            # Includes 404. See the docstring: a wrong path must not read as "already revoked".
            raise CardIssuerError("REAP_REVOKE_REFUSED", f"issuer returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except Exception as err:
            raise CardIssuerError("REAP_REVOKE_UNCONFIRMED", "revoke response was not parseable") from err

        card = body.get("card") if isinstance(body.get("card"), dict) else body
        state = card.get("status") if isinstance(card, dict) else None
        # Both spellings: the provider's own docs are not in hand, and "cancelled"/"canceled"
        # is the single most common way for an adapter to miss a confirmation it did receive.
        if not isinstance(state, str) or state.strip().lower() not in (
            "revoked", "cancelled", "canceled", "terminated", "closed"
        ):
            raise CardIssuerError(
                "REAP_REVOKE_UNCONFIRMED", "issuer did not confirm the card is dead"
            )
