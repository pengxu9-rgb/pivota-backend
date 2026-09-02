#!/usr/bin/env python3
"""Find merchants whose PSP provider `orders` will not accept.

WHAT "UNACCEPTED" MEANS
    `orders` carries CHECK check_psp_used_valid_provider. Migration 006 froze its
    list at five names; migration 208 widens it to every provider this code
    actually writes:

        stripe, adyen, checkout, paypal, braintree, antom, protocol_deferred

    Order creation copies merchant_psps.provider straight into orders.psp_used
    (routes/order_routes._resolve_active_order_psp -> db.orders.create_order), so
    an ACTIVE merchant_psps row whose provider is outside that list is a merchant
    whose PSP saved, validated, shows "connected" in the portal, and 500s on EVERY
    order creation. Nothing surfaces it between onboarding and the first sale,
    which is why this is a report rather than a dashboard tile.

    Known producer: POST /merchant/onboarding/setup-psp took `psp_type: str` with
    no allowlist at all and wrote status='active'. Its own capabilities map
    advertised `square`, which has no PSP adapter anywhere in this repo. Fixed in
    routes/employee_store_psp_fixes.py (SETUP_PSP_ALLOWED_PROVIDERS).

WHY THIS DOES NOT REPAIR
    Unlike a malformed psp_id, a wrong PROVIDER cannot be rewritten mechanically:
    the row's api_key, secret_key, account_id and provider_config are all
    provider-shaped credentials. A merchant who "connected Square" does not have
    Stripe credentials hiding in that row, and inventing a provider for them would
    turn a broken checkout into a mis-routed charge. The repair is a conversation
    with the merchant, so this script reports and stops.

    The safe interim action is to DEACTIVATE the row, which this script will do
    with --deactivate: `fetch_active_runtime_merchant_psp` filters on
    status='active', so an inactive row stops being selected and order creation
    fails with the honest 400 "No active PSP configuration found for this
    merchant" instead of a 500 on a database constraint.

USAGE
    python scripts/audit_unaccepted_psp_providers.py --database-url "$DATABASE_URL"
    python scripts/audit_unaccepted_psp_providers.py --database-url "$DATABASE_URL" --deactivate

    Report-only by default. Exits non-zero while any active row is unservable, so
    a scheduled run is visibly red.

    Once the report is empty, promote migration 208's constraint from NOT VALID:

        ALTER TABLE orders VALIDATE CONSTRAINT check_psp_used_valid_provider;
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_208 = REPO_ROOT / "db" / "migrations" / "208_orders_psp_used_valid_provider.sql"


def accepted_providers() -> List[str]:
    """Read the list out of migration 208 rather than restating it here.

    A second copy of the vocabulary is a second thing to forget to update — which
    is the whole shape of the defect this script exists for.
    """
    body = MIGRATION_208.read_text(encoding="utf-8")
    match = re.search(r"psp_used IN \(([^)]*)\)", body, re.DOTALL)
    if not match:
        raise SystemExit(
            f"{MIGRATION_208.name} no longer contains a `psp_used IN (...)` list — "
            "this script cannot state the rule it is auditing against"
        )
    providers = re.findall(r"'([a-z_]+)'", match.group(1))
    if not providers:
        raise SystemExit(f"parsed zero providers out of {MIGRATION_208.name}")
    return providers


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # Defaults to $DATABASE_URL, matching every other ops script in this repo
    # (scripts/catalog_migration_058.py, backfill_pg_singleton.py, ...). Not
    # cosmetic: scripts/ops/run_oneoff_job.sh is how anything runs against
    # production, and it MOUNTS DATABASE_URL as an env var while passing --args
    # literally, with no shell to expand `$DATABASE_URL`. `required=True` made this
    # script impossible to invoke through the only mechanism that reaches the
    # database it audits.
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL") or "",
        help=(
            "Postgres URL; defaults to $DATABASE_URL. "
            "Never point --deactivate at a DB you cannot restore."
        ),
    )
    parser.add_argument(
        "--deactivate",
        action="store_true",
        help=(
            "Set status='inactive' on every unservable ACTIVE row, so order "
            "creation returns an honest 400 instead of a 500. Does NOT guess a "
            "provider."
        ),
    )
    parser.add_argument("--output-json", default=None)
    return parser.parse_args()


def _connect_postgres(database_url: str):
    try:
        import psycopg  # type: ignore

        return psycopg.connect(database_url)
    except Exception:
        import psycopg2  # type: ignore

        return psycopg2.connect(database_url)


def _rows(cursor) -> List[Dict[str, Any]]:
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def main() -> int:
    args = _parse_args()
    if not str(args.database_url or "").strip():
        raise SystemExit(
            "no database URL: pass --database-url or set DATABASE_URL. (Under "
            "scripts/ops/run_oneoff_job.sh, mount it with "
            "SECRETS=DATABASE_URL=DATABASE_URL:latest -- a Cloud Run job inherits nothing.)"
        )
    providers = accepted_providers()
    print(f"orders.psp_used accepts: {', '.join(providers)}\n")

    conn = _connect_postgres(args.database_url)
    findings: List[Dict[str, Any]] = []
    orphaned_orders: List[Dict[str, Any]] = []
    try:
        with conn.cursor() as cursor:
            # LOWER(provider), because persist_canonical_merchant_psp lowercases
            # on write but nothing stopped an older writer from storing 'Stripe'.
            cursor.execute(
                """
                SELECT psp_id, merchant_id, provider, status, environment,
                       validation_status, connected_at
                  FROM merchant_psps
                 WHERE LOWER(TRIM(provider)) <> ALL(%s)
                 ORDER BY (status = 'active') DESC, connected_at DESC NULLS LAST, psp_id ASC
                """,
                (providers,),
            )
            findings = _rows(cursor)

            # Rows already IN orders that the constraint would refuse. Under
            # migration 208's NOT VALID these are not rejected retroactively, so
            # they are exactly what blocks the VALIDATE step named in the header.
            cursor.execute(
                """
                SELECT psp_used, COUNT(*) AS n
                  FROM orders
                 WHERE psp_used IS NOT NULL
                   AND psp_used <> ALL(%s)
                 GROUP BY psp_used
                 ORDER BY n DESC
                """,
                (providers,),
            )
            orphaned_orders = _rows(cursor)

            active = [r for r in findings if str(r.get("status") or "").lower() == "active"]
            inactive = [r for r in findings if r not in active]

            if not findings:
                print("No merchant_psps row carries an unaccepted provider.")
            else:
                if active:
                    print(
                        f"{len(active)} ACTIVE row(s) — each of these merchants CANNOT create an order:\n"
                    )
                    for row in active:
                        print(
                            f"  {row['psp_id']!r:32} merchant={row['merchant_id']} "
                            f"provider={row['provider']!r} env={row['environment']} "
                            f"validation={row['validation_status']} connected_at={row['connected_at']}"
                        )
                if inactive:
                    print(
                        f"\n{len(inactive)} non-active row(s) (not selected at "
                        "checkout; listed so the VALIDATE step below is not a surprise):\n"
                    )
                    for row in inactive:
                        print(
                            f"  {row['psp_id']!r:32} merchant={row['merchant_id']} "
                            f"provider={row['provider']!r} status={row['status']}"
                        )

            if orphaned_orders:
                total = sum(int(r["n"]) for r in orphaned_orders)
                print(
                    f"\n{total} existing orders row(s) hold a psp_used outside the list. "
                    "Migration 208's constraint is NOT VALID, so these are not rejected "
                    "retroactively — but they must be resolved before VALIDATE:\n"
                )
                for row in orphaned_orders:
                    print(f"  psp_used={row['psp_used']!r:24} {row['n']} order(s)")
            else:
                print(
                    "\nNo existing orders row holds an unaccepted psp_used. "
                    "Safe to run: ALTER TABLE orders VALIDATE CONSTRAINT check_psp_used_valid_provider;"
                )

            if active and args.deactivate:
                print("\n--deactivate: taking these rows out of runtime selection")
                for row in active:
                    cursor.execute(
                        "UPDATE merchant_psps SET status = 'inactive' WHERE psp_id = %s",
                        (row["psp_id"],),
                    )
                    row["deactivated"] = True
                    print(f"  {row['psp_id']} -> status=inactive")
                conn.commit()
                print(
                    "\nCommitted. Those merchants now get a 400 'No active PSP "
                    "configuration' instead of a 500. They still need a real PSP."
                )
            elif active:
                print(
                    "\nReport only. No provider is guessed: the row's credentials are "
                    "provider-shaped and inventing one would mis-route a charge. "
                    "Re-run with --deactivate to stop the 500s while you contact the merchant."
                )
    finally:
        conn.close()

    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(
                {"merchant_psps": findings, "orders_by_psp_used": orphaned_orders},
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    # Non-zero while any merchant is broken, so a scheduled run is visibly red.
    unservable = any(str(r.get("status") or "").lower() == "active" for r in findings)
    return 1 if (unservable and not args.deactivate) or orphaned_orders else 0


if __name__ == "__main__":
    raise SystemExit(main())
