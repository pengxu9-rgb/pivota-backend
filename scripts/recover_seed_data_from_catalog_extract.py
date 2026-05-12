"""Recover external_product_seeds seed_data from catalog-intelligence extracts.

This is a targeted repair bridge for rows whose live seed_data is underfilled
because the stored source URL went stale. It deliberately does not UPDATE
external_product_seeds.seed_data directly. Apply mode calls
services.seed_data_writer.upsert_seed_data(), which records a proposal and
performs the gated merge under the authorized write token.

Example:
    python3 scripts/recover_seed_data_from_catalog_extract.py \\
      --external-product-ids mac:62c89320b830814c,ulta:5311e76277c7efd9 \\
      --url-override 'mac:62c89320b830814c=https://www.maccosmetics.com/...' \\
      --url-override 'ulta:5311e76277c7efd9=https://www.ulta.com/...' \\
      --skip-description-for mac:62c89320b830814c \\
      --proposer pdp_seed_content_recovery_20260512 \\
      --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

# Ensure project root is importable when running via `python3 scripts/...`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services import seed_data_writer  # noqa: E402
from services.seed_data_writer import _coerce_jsonb  # noqa: E402


DEFAULT_EXTRACT_BASE_URL = os.getenv(
    "CATALOG_INTELLIGENCE_BASE_URL",
    "https://pivota-catalog-intelligence-production.up.railway.app",
)


SELECT_SEED_ROWS = """
    SELECT id, external_product_id, market, domain, canonical_url, destination_url,
           title, image_url, price_amount, price_currency, availability, seed_data,
           status, updated_at
    FROM external_product_seeds
    WHERE status = 'active'
      AND external_product_id = ANY(:external_product_ids)
    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
"""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_csv(value: str) -> List[str]:
    return [_clean(item) for item in _clean(value).replace("\n", ",").split(",") if _clean(item)]


def parse_key_value_items(items: Iterable[str]) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for item in items:
      text = _clean(item)
      if not text:
          continue
      if "=" not in text:
          raise ValueError(f"Expected KEY=VALUE item, got: {text}")
      key, value = text.split("=", 1)
      key = _clean(key)
      value = _clean(value)
      if not key or not value:
          raise ValueError(f"Expected non-empty KEY=VALUE item, got: {text}")
      parsed[key] = value
    return parsed


def first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def ensure_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def ensure_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def normalize_image_urls(*values: Any, limit: int = 24) -> List[str]:
    urls: List[str] = []

    def add(candidate: Any) -> None:
        if isinstance(candidate, str):
            text = candidate.strip()
            if text and text not in urls:
                urls.append(text)
            return
        if isinstance(candidate, Mapping):
            add(
                candidate.get("url")
                or candidate.get("src")
                or candidate.get("image_url")
                or candidate.get("imageUrl")
                or candidate.get("href")
            )
            return
        if isinstance(candidate, list):
            for item in candidate:
                add(item)

    for value in values:
        add(value)
    return urls[:limit]


def url_shade_token(value: str) -> str:
    parsed = urlparse(_clean(value))
    query = parse_qs(parsed.query)
    shade = first_string(*(query.get("shade") or []), *(query.get("sku") or []))
    return shade.lower().replace("+", " ").strip()


def find_matching_variant(product: Mapping[str, Any], target_url: str, explicit_shade: str = "") -> Optional[Dict[str, Any]]:
    variants = [item for item in ensure_list(product.get("variants")) if isinstance(item, dict)]
    if not variants:
        return None
    wanted = _clean(explicit_shade).lower() or url_shade_token(target_url)
    if not wanted:
        return None
    wanted_compact = wanted.replace("%20", " ")
    for variant in variants:
        variant_url = first_string(variant.get("url"), variant.get("deep_link"), variant.get("product_url"))
        variant_text = " ".join(
            filter(
                None,
                [
                    variant_url,
                    _clean(variant.get("title")),
                    _clean(variant.get("name")),
                    _clean(variant.get("option_value")),
                    _clean(variant.get("option1")),
                    _clean(variant.get("sku")),
                ],
            )
        ).lower()
        if wanted_compact and wanted_compact in variant_text.replace("%20", " "):
            return dict(variant)
    return None


def build_proposed_seed_data(
    *,
    row: Mapping[str, Any],
    extracted_product: Mapping[str, Any],
    target_url: str,
    proposer: str,
    skip_description: bool = False,
    explicit_shade: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    current = _coerce_jsonb(row.get("seed_data")) or {}
    proposed = dict(current)
    snapshot = ensure_dict(proposed.get("snapshot"))
    matched_variant = find_matching_variant(extracted_product, target_url, explicit_shade)

    destination_url = first_string(
        matched_variant.get("url") if matched_variant else "",
        matched_variant.get("deep_link") if matched_variant else "",
        extracted_product.get("url"),
        extracted_product.get("canonical_url"),
        target_url,
    )
    canonical_url = first_string(
        matched_variant.get("url") if matched_variant else "",
        extracted_product.get("canonical_url"),
        extracted_product.get("url"),
        target_url,
    )
    if matched_variant:
        image_urls = normalize_image_urls(
            matched_variant.get("image_url"),
            matched_variant.get("image_urls"),
            matched_variant.get("images"),
            current.get("image_url"),
            current.get("image_urls"),
        )
        if not image_urls:
            image_urls = normalize_image_urls(
                extracted_product.get("image_url"),
                extracted_product.get("image_urls"),
                extracted_product.get("images"),
                current.get("image_url"),
                current.get("image_urls"),
            )
    else:
        image_urls = normalize_image_urls(
            extracted_product.get("image_url"),
            extracted_product.get("image_urls"),
            extracted_product.get("images"),
            current.get("image_url"),
            current.get("image_urls"),
        )
    description = "" if skip_description else first_string(
        extracted_product.get("description_raw"),
        extracted_product.get("description"),
        extracted_product.get("summary"),
    )
    extracted_title = first_string(extracted_product.get("title"), extracted_product.get("name"))
    title = first_string(row.get("title"), current.get("title"), extracted_title)
    brand = first_string(
        current.get("brand"),
        extracted_product.get("brand"),
        row.get("brand"),
        row.get("domain"),
    )
    variants = ensure_list(extracted_product.get("variants"))
    selected_variant_id = first_string(
        matched_variant.get("variant_id") if matched_variant else "",
        matched_variant.get("id") if matched_variant else "",
        matched_variant.get("sku") if matched_variant else "",
    )

    patch: Dict[str, Any] = {
        "external_product_id": row.get("external_product_id"),
        "product_id": row.get("external_product_id"),
        "title": title,
        "brand": brand,
        "canonical_url": canonical_url,
        "destination_url": destination_url,
        "source_url": target_url,
        "image_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "images": image_urls,
        "variants": variants,
        "price_amount": first_string(
            extracted_product.get("price_amount"),
            extracted_product.get("price"),
            current.get("price_amount"),
            row.get("price_amount"),
        ),
        "price_currency": first_string(
            extracted_product.get("currency"),
            extracted_product.get("price_currency"),
            current.get("price_currency"),
            row.get("price_currency"),
            "USD",
        ),
        "availability": first_string(
            extracted_product.get("availability"),
            current.get("availability"),
            row.get("availability"),
        ),
        "extracted_at": _now_iso(),
        "validated_at": _now_iso(),
        "agent_version": "catalog_extract_seed_recovery_v1",
        "seed_recovery": {
            "proposer": proposer,
            "target_url": target_url,
            "matched_variant_id": selected_variant_id or None,
            "skip_description": bool(skip_description),
        },
    }
    if selected_variant_id:
        patch["selected_variant_id"] = selected_variant_id
        patch["default_variant_id"] = selected_variant_id
    if description:
        patch["description"] = description
        patch["pdp_description_raw"] = description

    proposed.update({k: v for k, v in patch.items() if v not in (None, "")})
    snapshot.update({k: v for k, v in patch.items() if v not in (None, "")})
    proposed["snapshot"] = snapshot

    summary = {
        "target_url": target_url,
        "matched_variant_id": selected_variant_id or None,
        "image_count": len(image_urls),
        "variant_count": len(variants),
        "description_chars": len(description),
        "title": title,
        "destination_url": destination_url,
        "canonical_url": canonical_url,
        "skip_description": bool(skip_description),
    }
    return proposed, summary


async def fetch_seed_rows(external_product_ids: List[str]) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        SELECT_SEED_ROWS,
        {"external_product_ids": external_product_ids},
    )
    return [dict(row) for row in rows or []]


async def extract_product(client: httpx.AsyncClient, *, base_url: str, row: Mapping[str, Any], target_url: str) -> Dict[str, Any]:
    seed_data = _coerce_jsonb(row.get("seed_data")) or {}
    body = {
        "brand": first_string(seed_data.get("brand"), row.get("brand"), row.get("title")),
        "domain": target_url,
        "market": first_string(row.get("market"), "US").upper(),
        "limit": 10,
    }
    response = await client.post(
        f"{base_url.rstrip('/')}/api/extract",
        json=body,
        timeout=90.0,
    )
    response.raise_for_status()
    payload = response.json()
    products = payload.get("products") if isinstance(payload, dict) else None
    product = products[0] if isinstance(products, list) and products else None
    if not isinstance(product, dict):
        diagnostics = payload.get("diagnostics") if isinstance(payload, dict) else None
        raise RuntimeError(f"extractor returned no products; diagnostics={diagnostics!r}")
    return product


def resolve_target_url(row: Mapping[str, Any], overrides: Mapping[str, str]) -> str:
    external_id = _clean(row.get("external_product_id"))
    seed_data = _coerce_jsonb(row.get("seed_data")) or {}
    snapshot = ensure_dict(seed_data.get("snapshot"))
    return first_string(
        overrides.get(external_id),
        snapshot.get("source_url"),
        snapshot.get("destination_url"),
        snapshot.get("canonical_url"),
        seed_data.get("source_url"),
        seed_data.get("destination_url"),
        seed_data.get("canonical_url"),
        row.get("destination_url"),
        row.get("canonical_url"),
    )


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    ids = parse_csv(args.external_product_ids)
    if args.external_product_id:
        ids.insert(0, _clean(args.external_product_id))
    ids = list(dict.fromkeys([item for item in ids if item]))
    if not ids:
        raise SystemExit("--external-product-id or --external-product-ids is required")

    overrides = parse_key_value_items(args.url_override or [])
    shade_by_id = parse_key_value_items(args.shade or [])
    skip_description_ids = set(parse_csv(args.skip_description_for or ""))
    apply = bool(args.apply)
    proposer = _clean(args.proposer) or "catalog_extract_seed_recovery"

    if not getattr(database, "is_connected", False):
        await database.connect()

    outcomes: List[Dict[str, Any]] = []
    try:
        rows = await fetch_seed_rows(ids)
        row_by_external_id = {_clean(row.get("external_product_id")): row for row in rows}
        async with httpx.AsyncClient() as client:
            for external_id in ids:
                row = row_by_external_id.get(external_id)
                if not row:
                    outcomes.append({"external_product_id": external_id, "outcome": "skipped_no_live_row"})
                    continue
                target_url = resolve_target_url(row, overrides)
                if not target_url:
                    outcomes.append({"external_product_id": external_id, "seed_id": row.get("id"), "outcome": "skipped_no_target_url"})
                    continue
                try:
                    extracted = await extract_product(client, base_url=args.base_url, row=row, target_url=target_url)
                    proposed, summary = build_proposed_seed_data(
                        row=row,
                        extracted_product=extracted,
                        target_url=target_url,
                        proposer=proposer,
                        skip_description=external_id in skip_description_ids,
                        explicit_shade=shade_by_id.get(external_id, ""),
                    )
                    if not apply:
                        outcomes.append({
                            "external_product_id": external_id,
                            "seed_id": row.get("id"),
                            "outcome": "dry_run",
                            "proposal_summary": summary,
                        })
                        continue
                    result = await seed_data_writer.upsert_seed_data(
                        seed_id=str(row["id"]),
                        external_product_id=external_id,
                        proposed_seed_data=proposed,
                        proposer=proposer,
                        source="catalog_extract_seed_recovery",
                        notes=f"target_url={target_url}",
                    )
                    outcomes.append({
                        "external_product_id": external_id,
                        "seed_id": row.get("id"),
                        "outcome": result.status,
                        "proposal_id": result.proposal_id,
                        "merged_fields": [d.field for d in result.field_decisions if d.decision == "merge"],
                        "rejected_fields": [d.field for d in result.field_decisions if d.decision == "reject"],
                        "proposal_summary": summary,
                    })
                except Exception as exc:
                    outcomes.append({
                        "external_product_id": external_id,
                        "seed_id": row.get("id"),
                        "outcome": "failed",
                        "error": str(exc),
                    })
                    if args.stop_on_error:
                        raise
    finally:
        await database.disconnect()

    counts: Dict[str, int] = {}
    for item in outcomes:
        counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
    return {
        "apply": apply,
        "proposer": proposer,
        "rows_requested": len(ids),
        "rows_found": len([item for item in outcomes if item.get("seed_id")]),
        "outcome_counts": counts,
        "outcomes": outcomes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external-product-id")
    parser.add_argument("--external-product-ids", default="")
    parser.add_argument("--url-override", action="append", default=[])
    parser.add_argument("--shade", action="append", default=[], help="Optional external_product_id=shade selector")
    parser.add_argument("--skip-description-for", default="")
    parser.add_argument("--proposer", default="catalog_extract_seed_recovery")
    parser.add_argument("--base-url", default=DEFAULT_EXTRACT_BASE_URL)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = asyncio.run(run(args))
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
