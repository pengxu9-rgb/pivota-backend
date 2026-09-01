"""Card issuer adapters — the one seam where Pivota talks to a card-issuing provider.

THE PCI BOUNDARY IS THE TYPE. `IssuedCard` has an issuer reference and a hosted reveal handle
and deliberately NO field that could carry a PAN, CVV, or expiry: the agent obtains credentials
directly from the issuer via the reveal handle, so card data never transits or rests in Pivota.
An adapter that parsed those out of a provider response would have nowhere to put them — that is
the point. Keep it that way; it is the same line the ACP door draws by permanently refusing
delegate_payment.

Issuer selection is fail-closed: no CARD_ISSUER configured means no issuance, not a default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Protocol


@dataclass(frozen=True)
class IssueRequest:
    card_id: str                 # Pivota's id, passed to the issuer as metadata for reconciliation
    amount_cap_minor: int
    currency: str
    merchant_domain: str         # the lock; the issuer enforces it as a merchant restriction
    single_use: bool
    expires_at: datetime
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class IssuedCard:
    issuer_card_ref: str
    reveal_handle: Optional[str]  # issuer-hosted URL/token the AGENT redeems; opaque to Pivota


class CardIssuerError(Exception):
    """`issuer_card_ref` is set ONLY when a card may exist at the issuer despite the failure.

    That happens on exactly one class of failure today: we got a 2xx, extracted a real card id,
    and then refused because the issuer would not confirm the constraints. The card is real; our
    row will say `failed`; and this ref is the only handle anything has to go revoke it. A
    transport error or a non-2xx leaves it None, because no card was minted to orphan.

    Carrying it on the exception rather than only logging it is what lets the caller PERSIST it —
    a log line is not a work queue, and the revocation sweep reads rows.
    """

    def __init__(self, code: str, message: str, *, issuer_card_ref: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.issuer_card_ref = issuer_card_ref


class CardIssuer(Protocol):
    name: str

    async def issue(self, request: IssueRequest) -> IssuedCard: ...

    async def revoke(self, issuer_card_ref: str) -> None:
        """Kill a card at the issuer. Returns normally ONLY on confirmed revocation.

        Raises CardIssuerError otherwise — including when the issuer answered 2xx but did not
        confirm the card is dead. Same rule as issuance: an unconfirmed constraint is not a
        constraint, and an unconfirmed revocation is not a revocation. The sweep leaves such a
        row alone so the next run tries again, which is the correct posture for a card that may
        be spendable and uncapped.
        """
        ...


def resolve_issuer(env: Optional[Dict[str, str]] = None) -> Optional[CardIssuer]:
    """Return the configured issuer, or None (=> issuance unavailable, callers must fail closed).

    Import inside the function so an unconfigured deployment never pays for (or breaks on) an
    adapter it does not use.
    """
    e = env if env is not None else os.environ
    which = str(e.get("CARD_ISSUER") or "").strip().lower()
    if which == "reap":
        from services.card_issuers.reap_issuer import ReapIssuer

        return ReapIssuer(e)
    if which == "mock":
        from services.card_issuers.mock_issuer import MockIssuer

        return MockIssuer(e)
    return None
