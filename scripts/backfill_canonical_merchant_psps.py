import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.merchant_psp_config_service import (
    build_provider_connect_record,
    build_stripe_connect_provider_config,
    evaluate_psp_readiness,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit or repair canonical merchant_psps drift for active PSP rows."
    )
    parser.add_argument("--merchant-id", help="Restrict the backfill to a single merchant_id.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum PSP rows to inspect.")
    parser.add_argument("--apply", action="store_true", help="Persist the normalized values.")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Inspect inactive rows too. Default behavior only audits active rows.",
    )
    return parser.parse_args()


def _json_sortable(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


async def _run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        conditions: List[str] = []
        values: Dict[str, Any] = {"limit": args.limit}
        if args.merchant_id:
            conditions.append("merchant_id = :merchant_id")
            values["merchant_id"] = args.merchant_id
        if not args.include_inactive:
            conditions.append("status = 'active'")
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

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

        active_counter = Counter()
        for row in rows:
            payload = dict(row)
            if str(payload.get("status") or "").strip().lower() == "active":
                active_counter[(payload.get("merchant_id"), payload.get("provider"))] += 1

        changed = 0
        scanned = 0
        reasons_counter: Counter[str] = Counter()
        for row in rows:
            scanned += 1
            payload = dict(row)

            provider = str(payload.get("provider") or "").strip().lower()
            raw_environment = str(payload.get("environment") or "").strip().lower() or "unknown"
            repaired_provider_config = payload.get("provider_config")
            normalized_preview = build_provider_connect_record(
                provider,
                api_key=payload.get("api_key") or "",
                account_id=payload.get("account_id"),
                provider_config=repaired_provider_config,
                environment=payload.get("environment"),
                validation_status="unknown",
                validation_error=None,
            )

            if provider == "stripe" and raw_environment != normalized_preview["environment"]:
                repaired_provider_config = build_stripe_connect_provider_config(
                    existing_provider_config=payload.get("provider_config"),
                    previous_api_key=payload.get("api_key"),
                    previous_account_id=payload.get("account_id"),
                    previous_environment=payload.get("environment"),
                    next_api_key=payload.get("api_key"),
                    next_account_id=payload.get("account_id"),
                    next_environment=normalized_preview["environment"],
                    mode="payment_intent",
                )

            normalized = build_provider_connect_record(
                provider,
                api_key=payload.get("api_key") or "",
                account_id=payload.get("account_id"),
                provider_config=repaired_provider_config,
                environment=payload.get("environment"),
                validation_status=payload.get("validation_status"),
                validation_error=payload.get("validation_error"),
            )
            readiness = evaluate_psp_readiness(
                provider,
                status=payload.get("status"),
                api_key=payload.get("api_key"),
                account_id=payload.get("account_id"),
                provider_config=normalized.get("provider_config"),
                environment=normalized.get("environment"),
                validation_status=normalized.get("validation_status"),
                validation_error=normalized.get("validation_error"),
            )

            drift_reasons: List[str] = []
            if raw_environment != normalized["environment"]:
                drift_reasons.append("environment_mismatch")
            if (
                provider == "stripe"
                and normalized["environment"] == "live"
                and not normalized["provider_summary"].get("webhook_ready")
            ):
                drift_reasons.append("stripe_live_missing_webhook")
            if active_counter[(payload.get("merchant_id"), provider)] > 1 and str(payload.get("status") or "").strip().lower() == "active":
                drift_reasons.append("duplicate_active_provider")
            if (
                str(payload.get("validation_status") or "").strip().lower() == "valid"
                and not readiness["live_charge_ready"]
            ):
                drift_reasons.append("valid_but_not_live_ready")

            repaired_validation_status = normalized["validation_status"]
            repaired_validation_error = normalized.get("validation_error")
            if drift_reasons:
                repaired_validation_status = "unknown"
                repaired_validation_error = None

            row_changed = (
                str(payload.get("environment") or "") != str(normalized["environment"])
                or _json_sortable(payload.get("provider_config")) != _json_sortable(normalized["provider_config"])
                or bool(drift_reasons)
                or str(payload.get("validation_status") or "unknown") != str(repaired_validation_status)
                or str(payload.get("validation_error") or "") != str(repaired_validation_error or "")
            )

            for reason in drift_reasons:
                reasons_counter[reason] += 1

            print(
                json.dumps(
                    {
                        "psp_id": payload.get("psp_id"),
                        "merchant_id": payload.get("merchant_id"),
                        "provider": provider,
                        "raw_environment": raw_environment,
                        "environment": normalized["environment"],
                        "validation_status": str(payload.get("validation_status") or "unknown").strip().lower() or "unknown",
                        "normalized_validation_status": repaired_validation_status,
                        "live_charge_ready": readiness["live_charge_ready"],
                        "webhook_ready": normalized["provider_summary"].get("webhook_ready"),
                        "readiness_blockers": readiness["readiness_blockers"],
                        "duplicate_active_count": active_counter[(payload.get("merchant_id"), provider)],
                        "drift_reasons": drift_reasons,
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
                            validation_error = :validation_error,
                            last_validated_at = :last_validated_at
                        WHERE psp_id = :psp_id
                        """,
                        {
                            "psp_id": payload["psp_id"],
                            "environment": normalized["environment"],
                            "provider_config": json.dumps(normalized["provider_config"] or {}),
                            "validation_status": repaired_validation_status,
                            "validation_error": repaired_validation_error,
                            "last_validated_at": None,
                        },
                    )

        print(
            json.dumps(
                {
                    "status": "success",
                    "scanned": scanned,
                    "changed": changed,
                    "applied": bool(args.apply),
                    "active_only": not args.include_inactive,
                    "drift_counts": dict(reasons_counter),
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
