import argparse
import asyncio
import json
from typing import Any, Dict, List

from db.database import database
from services.merchant_psp_config_service import (
    build_provider_connect_record,
    evaluate_psp_readiness,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill canonical merchant_psps environment/provider_config/validation fields."
    )
    parser.add_argument("--merchant-id", help="Restrict the backfill to a single merchant_id.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum PSP rows to inspect.")
    parser.add_argument("--apply", action="store_true", help="Persist the normalized values.")
    return parser.parse_args()


def _json_sortable(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


async def _run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        where_clause = ""
        values: Dict[str, Any] = {"limit": args.limit}
        if args.merchant_id:
            where_clause = "WHERE merchant_id = :merchant_id"
            values["merchant_id"] = args.merchant_id

        rows = await database.fetch_all(
            f"""
            SELECT psp_id, merchant_id, provider, api_key, account_id, provider_config,
                   environment, validation_status, validation_error, status
            FROM merchant_psps
            {where_clause}
            ORDER BY connected_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT :limit
            """,
            values,
        )

        changed = 0
        scanned = 0
        for row in rows:
            scanned += 1
            payload = dict(row)
            normalized = build_provider_connect_record(
                payload.get("provider"),
                api_key=payload.get("api_key") or "",
                account_id=payload.get("account_id"),
                provider_config=payload.get("provider_config"),
                environment=payload.get("environment"),
                validation_status=payload.get("validation_status"),
                validation_error=payload.get("validation_error"),
            )
            readiness = evaluate_psp_readiness(
                payload.get("provider"),
                status=payload.get("status"),
                api_key=payload.get("api_key"),
                account_id=payload.get("account_id"),
                provider_config=normalized.get("provider_config"),
                environment=normalized.get("environment"),
                validation_status=normalized.get("validation_status"),
                validation_error=normalized.get("validation_error"),
            )

            row_changed = (
                str(payload.get("environment") or "") != str(normalized["environment"])
                or _json_sortable(payload.get("provider_config")) != _json_sortable(normalized["provider_config"])
                or str(payload.get("validation_status") or "unknown") != str(normalized["validation_status"])
                or str(payload.get("validation_error") or "") != str(normalized.get("validation_error") or "")
            )

            print(
                json.dumps(
                    {
                        "psp_id": payload.get("psp_id"),
                        "merchant_id": payload.get("merchant_id"),
                        "provider": payload.get("provider"),
                        "environment": normalized["environment"],
                        "validation_status": normalized["validation_status"],
                        "live_charge_ready": readiness["live_charge_ready"],
                        "readiness_blockers": readiness["readiness_blockers"],
                        "changed": row_changed,
                    },
                    ensure_ascii=True,
                )
            )

            if row_changed:
                changed += 1
                if args.apply:
                    await database.execute(
                        """
                        UPDATE merchant_psps
                        SET environment = :environment,
                            provider_config = CAST(:provider_config AS jsonb),
                            validation_status = :validation_status,
                            validation_error = :validation_error
                        WHERE psp_id = :psp_id
                        """,
                        {
                            "psp_id": payload["psp_id"],
                            "environment": normalized["environment"],
                            "provider_config": json.dumps(normalized["provider_config"] or {}),
                            "validation_status": normalized["validation_status"],
                            "validation_error": normalized.get("validation_error"),
                        },
                    )

        print(
            json.dumps(
                {
                    "status": "success",
                    "scanned": scanned,
                    "changed": changed,
                    "applied": bool(args.apply),
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        await database.disconnect()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
