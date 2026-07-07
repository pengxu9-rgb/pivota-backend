#!/usr/bin/env python3
"""Real-Postgres end-to-end test of the partner onboarding invite flow.

Exercises the ACTUAL service/route code against a live Postgres — the kind of
coverage the string-matching fake-DB unit tests cannot give. It catches bugs
that only manifest against a real database: CHECK-constraint violations,
Postgres parameter-type ambiguity (42P08), cross-table read/write mismatches.

Two bugs this harness caught that unit tests missed:
  * revoke() `:param IS NULL` -> 42P08 AmbiguousParameterError.
  * multi-use consume() setting consumed_at while active -> violates
    ck_partner_invite_tokens_consumed_pair (first redemption 500).

Run it against a Postgres that already has the invite-flow migrations applied
(108, 125, 111, 134_partner_invite_tokens, 171, 137, 127_monthly_brand_statements):

    DATABASE_URL=postgresql://user@host:port/db \
      python tests/integration/partner_invite_flow_e2e.py

Exits 0 on success, 1 on any failure. TRUNCATEs the partner tables it uses.
The pytest wrapper (test_partner_invite_flow_pg_integration.py) runs this as a
subprocess when PIVOTA_E2E_PG_URL is set, and skips otherwise.
"""

from __future__ import annotations

import asyncio
import os
import sys

if not (os.getenv("DATABASE_URL") or "").startswith(("postgres://", "postgresql://")):
    print("SKIP: DATABASE_URL must point at a migrated Postgres for this harness")
    sys.exit(2)

os.environ.setdefault("DB_POOL_MIN_SIZE", "1")
os.environ.setdefault("DB_POOL_MAX_SIZE", "3")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))

from db.database import database  # noqa: E402
from routes import admin_partners  # noqa: E402
from routes.admin_partners import PartnerCreateRequest  # noqa: E402
from services import (  # noqa: E402
    partner_invite_email,
    partner_invite_token_service as tok,
)

ADMIN = {"email": "admin@pivota.test", "role": "admin"}
_fails: list[str] = []

_PARTNER_TABLES = (
    "partner_attribution",
    "partner_invite_tokens",
    "partner_contacts",
    "partner_rate_schedules",
    "partner_cohort_targets",
    "monthly_brand_statements",
    "channel_partners",
)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + detail}")
    if not cond:
        _fails.append(name)


async def _reset() -> None:
    await database.execute(
        "TRUNCATE " + ", ".join(_PARTNER_TABLES) + " RESTART IDENTITY CASCADE"
    )


async def main() -> None:
    await database.connect()
    sent: list[dict] = []

    class _R:
        ok = True
        error = None

    partner_invite_email.send_email = lambda **kw: (sent.append(kw) or _R())

    try:
        await _reset()

        # 1. CREATE PARTNER with a contact email
        created = await admin_partners.create_admin_partner(
            PartnerCreateRequest(
                legal_name="E2E Test Partner",
                archetype="agency",
                contact_email="partner-finance@e2e.test",
            ),
            current_admin=ADMIN,
        )
        ok = isinstance(created, dict) and "id" in created
        check("create returns partner dict", ok, repr(created)[:200])
        if not ok:
            return
        pid = int(created["id"])
        check("rate schedule seeded (9 rows)",
              created.get("seeded_rate_schedule_count") == 9,
              str(created.get("seeded_rate_schedule_count")))
        pc = await database.fetch_one(
            "SELECT contact_email FROM partner_contacts WHERE channel_partner_id = :id",
            {"id": pid},
        )
        check("create wrote partner_contacts row",
              pc is not None and pc["contact_email"] == "partner-finance@e2e.test",
              repr(pc))

        # 2. ISSUE reusable invite link
        res = await tok.issue(channel_partner_id=pid, issued_by="admin@pivota.test")
        check("issue returns signup_url + raw_token", bool(res.signup_url and res.raw_token))
        raw, token_id = res.raw_token, res.token_id

        # 3. AUTO-EMAIL reads partner_contacts end to end
        outcome = await partner_invite_email.send_invite_email(
            channel_partner_id=pid, signup_url=res.signup_url, expires_at=res.expires_at,
        )
        check("auto-email sends to the create-time contact",
              outcome.get("email_sent") is True
              and outcome.get("recipient") == "partner-finance@e2e.test",
              repr(outcome))
        check("mailer invoked with the signup_url",
              len(sent) == 1 and res.signup_url in sent[0].get("text_body", ""),
              repr(sent[:1])[:200])
        check("mailer invoked with a from_email (else SES FROM_EMAIL_MISSING)",
              bool((sent[0].get("from_email") or "").strip()) if sent else False,
              repr(sent[:1])[:200])

        # 4. REDEEM by multiple merchants (multi-use) + duplicate
        a1 = await tok.consume(raw_token=raw, merchant_id="merch_e2e_a")
        a2 = await tok.consume(raw_token=raw, merchant_id="merch_e2e_b")
        a3 = await tok.consume(raw_token=raw, merchant_id="merch_e2e_c")
        a2_dup = await tok.consume(raw_token=raw, merchant_id="merch_e2e_b")
        check("3 distinct merchants -> 3 attribution ids", len({a1, a2, a3}) == 3)
        check("duplicate merchant idempotent", a2_dup == a2)
        trow = await database.fetch_one(
            "SELECT status, use_count, consumed_at FROM partner_invite_tokens WHERE id = :id",
            {"id": token_id})
        check("token stays active after redemptions", trow["status"] == "active", repr(trow))
        check("use_count == 3 (dup not counted)", trow["use_count"] == 3, repr(trow))
        check("consumed_at stays null (consumed_pair CHECK)", trow["consumed_at"] is None, repr(trow))
        n_attr = await database.fetch_val(
            "SELECT count(*) FROM partner_attribution WHERE channel_partner_id = :id", {"id": pid})
        check("3 partner_attribution rows", n_attr == 3, str(n_attr))

        # 5. LIST
        listing = await tok.list_for_partner(channel_partner_id=pid)
        check("list returns the token with use_count",
              len(listing) == 1 and listing[0]["use_count"] == 3, repr(listing)[:200])

        # 6. SIGN one attribution
        signed = await admin_partners.sign_partner_attribution(pid, "merch_e2e_a", current_admin=ADMIN)
        check("sign -> status=signed",
              isinstance(signed, dict) and signed.get("status") == "signed", repr(signed)[:200])

        # 7. REVOKE (42P08 regression)
        try:
            await tok.revoke(token_id=token_id, revoked_by="admin@pivota.test", channel_partner_id=pid)
            trow = await database.fetch_one(
                "SELECT status FROM partner_invite_tokens WHERE id = :id", {"id": token_id})
            check("revoke succeeds, token -> revoked", trow["status"] == "revoked", repr(trow))
        except Exception as exc:  # noqa: BLE001
            check("revoke succeeds (no AmbiguousParameterError)", False, f"{type(exc).__name__}: {exc}")

        # 8. Redeem after revoke -> rejected
        try:
            await tok.consume(raw_token=raw, merchant_id="merch_e2e_late")
            check("consume after revoke rejected", False, "no error raised")
        except tok.TokenNotRedeemableError:
            check("consume after revoke -> TokenNotRedeemableError", True)
        except Exception as exc:  # noqa: BLE001
            check("consume after revoke -> TokenNotRedeemableError", False, f"{type(exc).__name__}: {exc}")

        await _reset()
    finally:
        await database.disconnect()

    if _fails:
        print(f"\nRESULT: {len(_fails)} FAILURE(S): {_fails}")
        sys.exit(1)
    print("\nRESULT: ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
