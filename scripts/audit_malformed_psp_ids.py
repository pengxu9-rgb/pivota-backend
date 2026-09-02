#!/usr/bin/env python3
"""Find -- and optionally repair -- merchant_psps rows whose psp_id is malformed.

WHAT "MALFORMED" MEANS
    `orders` has enforced CHECK check_psp_id_format since migration 006:

        psp_id IS NULL OR psp_id ~* '^psp_[a-z0-9]+_[a-z0-9]{12}$'

    `merchant_psps` -- the table that mints the value -- enforced nothing until
    migration 207, and order creation copies merchant_psps.psp_id straight into
    orders.psp_id. So a row that violates the regex is a merchant whose PSP saves,
    validates, looks connected in the portal, and 500s on EVERY order creation.
    Nothing surfaces the defect between onboarding and the first sale, which is
    why this report exists rather than a dashboard tile.

    Known producer: POST /merchant/onboarding/setup-psp minted an 8-char suffix
    (`psp_stripe_30cc4106`) instead of 12. Fixed in
    routes/employee_store_psp_fixes.py. A second, admin-only path can still take
    a caller-supplied psp_id: POST /admin/psp/connect passes `payload["psp_id"]`
    through when no row matches it. Migration 207's constraint now rejects both.

WHY REPAIR IS SAFE
    `orders.psp_id` is the only other column in the schema that holds a
    merchant_psps.psp_id (verified: no other table declares the column, and there
    are no foreign keys onto it). Its own CHECK has made it impossible for a
    malformed id to have been referenced there, so rewriting the primary key
    cannot orphan an order. The script re-checks that invariant per row before
    touching anything and refuses to rewrite an id that any order references.

USAGE
    python scripts/audit_malformed_psp_ids.py --database-url "$DATABASE_URL"
    python scripts/audit_malformed_psp_ids.py --database-url "$DATABASE_URL" --repair

    Report-only by default. --repair rewrites each malformed psp_id to a fresh
    canonical id from services.merchant_psp_config_service._generate_psp_id, in a
    single transaction, and prints the before/after mapping.

    After the report comes back empty, promote the constraint from NOT VALID:

        ALTER TABLE merchant_psps VALIDATE CONSTRAINT check_merchant_psps_psp_id_format;
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.merchant_psp_config_service import _generate_psp_id  # noqa: E402

# Byte-identical to migration 006 (orders) and migration 207 (merchant_psps).
PSP_ID_FORMAT_REGEX = r"^psp_[a-z0-9]+_[a-z0-9]{12}$"

FIND_SQL = f"""
SELECT psp_id, merchant_id, provider, status, environment, validation_status, connected_at
  FROM merchant_psps
 WHERE psp_id !~* '{PSP_ID_FORMAT_REGEX}'
 ORDER BY connected_at DESC NULLS LAST, psp_id ASC
"""

COUNT_REFERENCING_ORDERS_SQL = "SELECT COUNT(*) FROM orders WHERE psp_id = %s"

REWRITE_SQL = "UPDATE merchant_psps SET psp_id = %s WHERE psp_id = %s"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Defaults to $DATABASE_URL. DIVERGES from 20f4542c, deliberately:
    # scripts/ops/run_oneoff_job.sh is the only way anything reaches production,
    # and it MOUNTS DATABASE_URL as an env var while passing --args literally,
    # with no shell to expand `$DATABASE_URL`. With required=True this script
    # could never be run against the database it exists to audit.
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or "", help="Postgres URL; defaults to $DATABASE_URL. Never point --repair at a DB you cannot restore.")
    parser.add_argument("--repair", action="store_true", help="Rewrite each malformed psp_id to a canonical one.")
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
    conn = _connect_postgres(args.database_url)
    findings: List[Dict[str, Any]] = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(FIND_SQL)
            findings = _rows(cursor)

            if not findings:
                print("No malformed merchant_psps.psp_id rows. "
                      "Safe to run: ALTER TABLE merchant_psps VALIDATE CONSTRAINT check_merchant_psps_psp_id_format;")
            else:
                print(f"{len(findings)} malformed merchant_psps row(s) -- each of these merchants CANNOT create an order:\n")
                for row in findings:
                    print(
                        f"  {row['psp_id']!r:40} merchant={row['merchant_id']} provider={row['provider']} "
                        f"status={row['status']} validation={row['validation_status']} connected_at={row['connected_at']}"
                    )

            if findings and args.repair:
                print("\n--repair: rewriting to canonical ids")
                for row in findings:
                    # The orders CHECK makes this count structurally zero. Verify
                    # rather than assume: a repair that silently orphaned a paid
                    # order would be far worse than a refusal to repair.
                    cursor.execute(COUNT_REFERENCING_ORDERS_SQL, (row["psp_id"],))
                    referencing = int(cursor.fetchone()[0])
                    if referencing:
                        raise SystemExit(
                            f"REFUSING to rewrite {row['psp_id']!r}: {referencing} order(s) reference it. "
                            "That should be impossible under orders.check_psp_id_format -- investigate before repairing."
                        )
                    new_psp_id = _generate_psp_id(str(row["provider"] or "").strip().lower())
                    cursor.execute(REWRITE_SQL, (new_psp_id, row["psp_id"]))
                    row["repaired_psp_id"] = new_psp_id
                    print(f"  {row['psp_id']} -> {new_psp_id}")
                conn.commit()
                print("\nCommitted. Re-run without --repair to confirm the report is empty.")
            elif findings:
                print("\nReport only. Re-run with --repair to rewrite these to canonical ids.")
    finally:
        conn.close()

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(findings, indent=2, default=str), encoding="utf-8")

    # Non-zero so a scheduled run is visibly red while any merchant is broken.
    return 1 if findings and not args.repair else 0


if __name__ == "__main__":
    raise SystemExit(main())
