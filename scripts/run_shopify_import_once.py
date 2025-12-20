import argparse
import asyncio
import json
import sys
from typing import Optional


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bypass Merchant Portal and run one Shopify catalog import task.\n"
            "This schedules a platform_import_task (source_type=connector, connector=shopify) "
            "and executes it via catalog_import_worker."
        )
    )
    parser.add_argument(
        "--merchant-id",
        help="Merchant ID to import for (required if --task-id not provided).",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        help="Existing platform_import_tasks.id to execute (skips scheduling).",
    )
    args = parser.parse_args(argv)

    if not args.task_id and not args.merchant_id:
        parser.error("Either --task-id or --merchant-id is required.")

    async def _main() -> int:
        from db.database import database
        from jobs.catalog_import_worker import process_import_task_by_id

        await database.connect()
        try:
            if args.task_id:
                task_id = int(args.task_id)
            else:
                from services.platform_import_service import schedule_import_task

                task_id = await schedule_import_task(
                    merchant_id=str(args.merchant_id),
                    source_type="connector",
                    connector="shopify",
                )
                print(f"scheduled_task_id={task_id}", file=sys.stderr)

            result = await process_import_task_by_id(task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        finally:
            await database.disconnect()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
