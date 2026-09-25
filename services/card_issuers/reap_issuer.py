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

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from services.card_issuers import CardIssuerError, IssuedCard, IssueRequest
from utils.logger import logger

_TIMEOUT_SECONDS = 15.0


def _as_aware(value: datetime) -> datetime:
    """A naive datetime is read as UTC — the only tz this rail ever mints in (`card_expiry`)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_instant(value: Any) -> Optional[datetime]:
    """An ISO-8601 timestamp string -> an AWARE datetime, or None if it is not one.

    COMPARE INSTANTS, NOT SPELLINGS. `_build_payload` sends `isoformat()` ("...+00:00") and an
    issuer may legitimately echo "...Z", or the same moment at another offset; those are the
    same constraint and must not read as a mismatch. What must NOT be tolerated is a different
    moment, however small — an expiry an hour later is exactly the silent widening this check
    exists to catch, and it looks like a formatting difference in a diff.

    Strings only: an epoch number would have to be guessed as seconds or milliseconds, and this
    file does not resolve ambiguity by guessing on a constraint.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_aware(parsed)


class ReapIssuer:
    name = "reap"

    def __init__(self, env: Dict[str, str]):
        self.base_url = str(env.get("REAP_API_BASE") or "").strip().rstrip("/")
        self.api_key = str(env.get("REAP_API_KEY") or "").strip()
        self.issue_path = str(env.get("REAP_ISSUE_CARD_PATH") or "/v1/cards").strip()
        # `{card_id}` is substituted at call time.
        self.revoke_path = str(
            env.get("REAP_REVOKE_CARD_PATH") or "/v1/cards/{card_id}/cancel"
        ).strip()
        # WHAT IS AND IS NOT ENV-CORRECTABLE ON THE REVOKE PATH — stated exactly, because the
        # comment that used to sit here claimed more than the code delivered. It said the shape
        # was "correctable without a code change" while the verb was hardcoded POST and
        # confirmation demanded a JSON body naming a dead state; neither matches how a delete
        # normally answers, so the very shape most likely to be right was unreachable from env.
        #
        #   CORRECTABLE BY ENV: the path (REAP_REVOKE_CARD_PATH) and now the verb
        #   (REAP_REVOKE_METHOD, POST or DELETE). Reap's own revoke is `DELETE /cards/{id}` ->
        #   204 with no body; the alternative is `POST /cards/{id}/block` -> a Card whose status
        #   is BLOCKED, which the dead-state vocabulary below now accepts.
        #
        #   NOT CORRECTABLE BY ENV, and each needs a code change here: the request sends no JSON
        #   body, auth is a bearer header, and confirmation is either an EMPTY 200/204 on the
        #   DELETE verb or a parseable body naming an affirmative dead state. An empty 200/204
        #   on POST stays UNCONFIRMED on purpose — a `/cancel` that answers nothing has told us
        #   nothing, and this file's rule is that silence is never confirmation.
        self.revoke_method = str(env.get("REAP_REVOKE_METHOD") or "POST").strip().upper()
        if self.revoke_method not in ("POST", "DELETE"):
            logger.warning(
                "REAP_REVOKE_METHOD=%r is not POST or DELETE; falling back to POST",
                self.revoke_method,
            )
            self.revoke_method = "POST"
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

        # EXPIRY IS THE FOURTH CONSTRAINT, and it was the one nothing checked. `_build_payload`
        # sends it, this module's docstring calls it part of "the product", and migration 201
        # declares `expires_at NOT NULL` because an unexpiring cap is not a cap. Unchecked, an
        # issuer that ignored the field minted a card outliving our row's expiry — the silent
        # drop this whole function exists to catch, on the constraint that bounds how LONG the
        # blast radius lasts rather than how large it is. Same tight spelling set as the others.
        observed_expiry = None
        for key in ("expires_at", "expiry"):
            if card.get(key) is not None:
                observed_expiry = card[key]
                break
        parsed_expiry = _parse_instant(observed_expiry)
        if parsed_expiry is None:
            _fail("REAP_CONSTRAINTS_UNCONFIRMED", "expiry")
        if parsed_expiry != _as_aware(request.expires_at):
            _fail("REAP_CONSTRAINTS_MISMATCH", "expiry")

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
        a FAILURE here, deliberately.

        ONE status code counts as confirmation, and only one: an EMPTY 200/204 answered to the
        DELETE verb, when DELETE is what REAP_REVOKE_METHOD configured. That is the shape a
        delete has — there is no body to state a dead state in — and refusing it would make the
        provider's own documented revoke permanently unconfirmable. Every other code, and the
        same empty 204 on POST, still needs a body naming an affirmative dead state.

        Unbounded retry is intended: the sweep will keep trying a card it cannot confirm dead,
        because giving up quietly on a possibly-spendable uncapped card is the worse outcome.
        The alarm is the escalation path, not a retry limit.
        """
        ref = str(issuer_card_ref or "").strip()
        if not ref:
            raise CardIssuerError("REAP_REVOKE_BAD_REQUEST", "no issuer card ref to revoke")
        url = f"{self.base_url}{self.revoke_path.replace('{card_id}', ref)}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.request(
                    self.revoke_method, url, headers={"Authorization": f"Bearer {self.api_key}"}
                )
        except httpx.HTTPError as err:
            raise CardIssuerError("REAP_UNREACHABLE", "revoke request failed in transport") from err
        if resp.status_code >= 300:
            # Includes 404. See the docstring: a wrong path must not read as "already revoked".
            raise CardIssuerError("REAP_REVOKE_REFUSED", f"issuer returned HTTP {resp.status_code}")

        # THE ONE PLACE A STATUS CODE COUNTS AS CONFIRMATION, and it is narrow on purpose. A
        # DELETE that succeeds answers 204 No Content — there is no body to name a dead state in,
        # so demanding one would refuse the correct response for the verb Reap actually
        # documents. It is accepted ONLY when DELETE is what we configured and sent: the same
        # empty 204 on POST /cancel is still silence, and silence is not confirmation.
        empty_body = not (getattr(resp, "content", b"") or b"").strip()
        if self.revoke_method == "DELETE" and empty_body and resp.status_code in (200, 204):
            return

        try:
            body = resp.json()
        except Exception as err:
            raise CardIssuerError("REAP_REVOKE_UNCONFIRMED", "revoke response was not parseable") from err

        card = body.get("card") if isinstance(body.get("card"), dict) else body
        state = card.get("status") if isinstance(card, dict) else None
        # Both spellings: the provider's own docs are not in hand, and "cancelled"/"canceled"
        # is the single most common way for an adapter to miss a confirmation it did receive.
        # `blocked` is the state Reap's `POST /cards/{id}/block` answers with — the alternative
        # shape REAP_REVOKE_METHOD/REAP_REVOKE_CARD_PATH exist to reach.
        if not isinstance(state, str) or state.strip().lower() not in (
            "revoked", "cancelled", "canceled", "terminated", "closed", "blocked"
        ):
            raise CardIssuerError(
                "REAP_REVOKE_UNCONFIRMED", "issuer did not confirm the card is dead"
            )
