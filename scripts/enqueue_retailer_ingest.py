"""Queue (brand, retailer) cohorts for the unattended retailer ingest pipeline.

Each JSONL row: {"domain": "k-touch.us", "brand": "3CE", "vendors": ["3CE"],
                 "options": {"lip_title_evidence": true, "only_category": "beauty/makeup/lip"},
                 "priority": 10}
`vendors` is required (a retailer cohort is selected by vendor). Everything else in `options` is
optional: market (ISO alpha-2, default US; US is the only market allowed yet), require_currency
(default and only allowed value: the market's currency, USD for US), category_path, only_category,
only_resolved_category, lip_title_evidence, exclude_handles, max_scan_products, max_products, retailer_name.

Re-enqueueing a cohort that already has an open job is a no-op (reported as `exists`).
Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.

    python -m scripts.enqueue_retailer_ingest --file cohorts.jsonl --source meitu_lip_2026_09
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import retailer_ingest as ledger  # noqa: E402

from services.retailer_ingest.pipeline import _OPTION_TYPES, validate_options  # noqa: E402

# Everything a row may put in `options` (vendors is a top-level row field). One source of truth:
# the pipeline's own validator, which also re-checks every job at execution.
_ALLOWED = set(_OPTION_TYPES) - {"vendors", "accepted_flags"}


# A public DNS hostname only: the drain crawls https://<domain>/products.json from the crawl subnet,
# so an IP literal, a port, a path or "localhost" would point it at something that is not a store.
_HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _row_to_job(row: Dict[str, Any]) -> Dict[str, Any]:
    domain, brand = str(row.get("domain") or "").strip().lower(), str(row.get("brand") or "").strip()
    vendors = [str(v).strip() for v in (row.get("vendors") or []) if str(v).strip()]
    if not domain or not brand or not vendors:
        raise ValueError(f"every row needs domain, brand and vendors: {row}")
    if not _HOSTNAME.match(domain) or re.fullmatch(r"[0-9.]+", domain):
        raise ValueError(f"domain must be a public hostname (no scheme, port, path or IP): {domain!r}")
    notes = (row.get("options") or {}).get("notes")
    if isinstance(notes, str) and len(notes) > 1000:
        raise ValueError("options.notes must be at most 1000 characters")
    options = dict(row.get("options") or {})
    unknown = set(options) - _ALLOWED
    if unknown:
        raise ValueError(f"unknown options {sorted(unknown)} in {row}")
    options["vendors"] = vendors
    validate_options(options)  # the same check the drain runs before any crawl
    # ...and the same payload normalization it runs (source_role values, retailer_name only for a
    # retailer): a row the drain would refuse at crawl time is refused here, by the same function.
    from services.catalog_onboard_worker import normalize_curated_brand_payload
    from services.retailer_ingest.pipeline import _feed_payload, _Stop
    try:
        normalize_curated_brand_payload(_feed_payload({"domain": domain, "brand": brand, "options": options}))
    except _Stop as exc:
        raise ValueError(exc.reason) from None
    if options.get("source_role") == "brand_official":
        from services.retailer_ingest.pipeline import brand_official_domain_flags
        fatal = [f for f in brand_official_domain_flags(domain, [brand]) if f.get("acceptable") is False]
        if fatal:
            raise ValueError(fatal[0]["detail"])
    return {"domain": domain, "brand": brand, "options": options, "priority": int(row.get("priority") or 0)}


def read_rows(path: str) -> List[Dict[str, Any]]:
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                rows.append(_row_to_job(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"line {number}: {exc}") from None
    return rows


async def _main(args: argparse.Namespace) -> int:
    from db.database import database

    jobs = read_rows(args.file) if args.file else [_row_to_job(json.loads(args.row))]
    await database.connect()
    try:
        for job in jobs:
            job_id = await ledger.enqueue_job(domain=job["domain"], brand=job["brand"], options=job["options"],
                                              priority=job["priority"], source=args.source)
            print(json.dumps({"domain": job["domain"], "brand": job["brand"],
                              "result": "queued" if job_id else "exists", "job_id": job_id}))
    finally:
        await database.disconnect()
    return 0


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="JSONL of cohorts")
    g.add_argument("--row", help="one cohort as a JSON object")
    p.add_argument("--source", required=True, help="who/what queued these (recorded on each job)")
    return asyncio.run(_main(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
