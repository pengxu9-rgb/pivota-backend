from __future__ import annotations

import asyncio
import copy
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse

from db.database import database
from services.external_seed_audit import (
    ensure_json_object,
    normalize_non_empty_string,
    normalize_seed_variants,
    normalize_url_like,
)
from services.pci_kb_scope_review import fetch_pci_kb_runtime_evidence_rows_by_source_refs_sync
from services.reviews_service import build_product_key

_COSMETIC_SHADE_HINTS = ("foundation", "lipstick", "blush", "gloss")


def _normalize_source_ref_aliases(value: Any) -> List[str]:
    normalized_url = normalize_url_like(value)
    if not normalized_url:
        return []
    try:
        parsed = urlparse(normalized_url)
    except Exception:
        return []

    host = (parsed.netloc or "").strip().lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    if not host:
        return []

    aliases: List[str] = []

    def add(alias: str) -> None:
        if alias and alias not in aliases:
            aliases.append(alias)

    host_aliases = [host]
    if host.startswith("www."):
        host_aliases.append(host[len("www."):])
    else:
        host_aliases.append(f"www.{host}")
    for host_alias in host_aliases:
        base_alias = f"{host_alias}{path}"
        add(base_alias)

    query = parse_qs(parsed.query, keep_blank_values=False)
    variant_id = normalize_non_empty_string((query.get("variant") or [""])[0])
    if variant_id:
        for host_alias in host_aliases:
            add(f"{host_alias}{path}?variant={variant_id}")

    if parsed.query:
        for host_alias in host_aliases:
            add(f"{host_alias}{path}?{parsed.query}")

    for host_alias in host_aliases:
        cleaned_url = urlunparse(
            (
                parsed.scheme.lower() or "https",
                host_alias,
                path,
                "",
                parsed.query,
                "",
            )
        )
        add(cleaned_url)
    return aliases


def _normalize_reviewed_ingredient_labels(kb_row: Dict[str, Any]) -> List[str]:
    labels: List[str] = []

    def add(value: Any) -> None:
        text = normalize_non_empty_string(value)
        if text and text not in labels:
            labels.append(text)

    raw_json = kb_row.get("inci_list_json")
    parsed_json = raw_json
    if isinstance(raw_json, str):
        try:
            parsed_json = json.loads(raw_json)
        except Exception:
            parsed_json = raw_json

    if isinstance(parsed_json, list):
        for item in parsed_json:
            add(item)

    if not labels:
        for token in str(kb_row.get("inci_list") or "").split(";"):
            add(token)

    return labels


def _seed_row_source_ref_aliases(seed_row: Dict[str, Any]) -> List[str]:
    aliases: List[str] = []

    def add_all(value: Any) -> None:
        for alias in _normalize_source_ref_aliases(value):
            if alias not in aliases:
                aliases.append(alias)

    seed_data = ensure_json_object(seed_row.get("seed_data"))
    snapshot = ensure_json_object(seed_data.get("snapshot"))

    for candidate in (
        seed_row.get("canonical_url"),
        seed_row.get("destination_url"),
        snapshot.get("canonical_url"),
        snapshot.get("destination_url"),
        seed_row.get("source_ref"),
    ):
        add_all(candidate)

    for variant in normalize_seed_variants(seed_data, seed_row):
        add_all(variant.get("url"))

    return aliases


def _matches_attached_variant(seed_variant: Dict[str, Any], attached_variant_id: str) -> bool:
    target = normalize_non_empty_string(attached_variant_id)
    if not target or target == "∅":
        return False
    variant_id = normalize_non_empty_string(seed_variant.get("variant_id") or seed_variant.get("id"))
    variant_sku = normalize_non_empty_string(seed_variant.get("sku"))
    return target in {variant_id, variant_sku}


def _extract_seed_variant_shade_value(seed_row: Dict[str, Any]) -> str:
    attached_variant_id = normalize_non_empty_string(seed_row.get("attached_variant_id"))
    if not attached_variant_id or attached_variant_id == "∅":
        return ""

    seed_data = ensure_json_object(seed_row.get("seed_data"))
    snapshot = ensure_json_object(seed_data.get("snapshot"))
    source_variants = normalize_seed_variants(seed_data, seed_row)
    matched_variant = next(
        (variant for variant in source_variants if _matches_attached_variant(variant, attached_variant_id)),
        None,
    )
    if not isinstance(matched_variant, dict):
        return ""

    option_name = normalize_non_empty_string(matched_variant.get("option_name")).lower()
    option_value = normalize_non_empty_string(matched_variant.get("option_value"))
    if option_name and any(hint in option_name for hint in ("shade", "tone", "color", "colour")) and option_value:
        return option_value

    context = " ".join(
        [
            normalize_non_empty_string(seed_row.get("title")),
            normalize_non_empty_string(seed_row.get("product_name")),
            normalize_non_empty_string(seed_row.get("category")),
            normalize_non_empty_string(snapshot.get("category")),
            normalize_non_empty_string(snapshot.get("title")),
        ]
    ).lower()
    if context and any(hint in context for hint in _COSMETIC_SHADE_HINTS):
        return option_value or normalize_non_empty_string(matched_variant.get("title"))
    return ""


def build_attached_seed_runtime_evidence_by_product_key(
    *,
    seed_rows: Iterable[Dict[str, Any]],
    kb_rows: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    kb_rows = [dict(row) for row in kb_rows or []]
    kb_by_alias: Dict[str, List[Dict[str, Any]]] = {}
    for kb_row in kb_rows:
        for alias in _normalize_source_ref_aliases(kb_row.get("source_ref")):
            kb_by_alias.setdefault(alias, []).append(kb_row)

    evidence_by_key: Dict[str, Dict[str, Any]] = {}
    for seed_row_raw in seed_rows or []:
        seed_row = dict(seed_row_raw)
        product_key = normalize_non_empty_string(seed_row.get("attached_product_key"))
        if not product_key:
            continue

        entry = evidence_by_key.setdefault(
            product_key,
            {"reviewed_ingredient_ids": [], "variant_shades": {}},
        )
        seen_kb_keys: set[Tuple[str, str]] = set()
        ingredient_labels: List[str] = entry["reviewed_ingredient_ids"]
        for alias in _seed_row_source_ref_aliases(seed_row):
            for kb_row in kb_by_alias.get(alias) or []:
                key = (
                    normalize_non_empty_string(kb_row.get("sku_key")),
                    normalize_non_empty_string(kb_row.get("source_ref")),
                )
                if key in seen_kb_keys:
                    continue
                seen_kb_keys.add(key)
                for label in _normalize_reviewed_ingredient_labels(kb_row):
                    if label not in ingredient_labels:
                        ingredient_labels.append(label)

        shade_value = _extract_seed_variant_shade_value(seed_row)
        attached_variant_id = normalize_non_empty_string(seed_row.get("attached_variant_id"))
        if shade_value and attached_variant_id and attached_variant_id != "∅":
            variant_shades = entry["variant_shades"]
            shades = variant_shades.setdefault(attached_variant_id, [])
            if shade_value not in shades:
                shades.append(shade_value)

    return evidence_by_key


def apply_attached_seed_runtime_evidence_to_product_data(
    *,
    product_data: Dict[str, Any],
    evidence: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    if not isinstance(product_data, dict) or not evidence:
        return product_data, False

    next_data = copy.deepcopy(product_data)
    changed = False

    reviewed_ingredient_ids = [
        normalize_non_empty_string(value)
        for value in (evidence.get("reviewed_ingredient_ids") or [])
        if normalize_non_empty_string(value)
    ]
    if reviewed_ingredient_ids:
        platform_metadata = (
            copy.deepcopy(next_data.get("platform_metadata"))
            if isinstance(next_data.get("platform_metadata"), dict)
            else {}
        )
        existing = platform_metadata.get("reviewed_ingredient_ids")
        existing_list = existing if isinstance(existing, list) else ([existing] if existing else [])
        merged = [normalize_non_empty_string(item) for item in existing_list if normalize_non_empty_string(item)]
        for label in reviewed_ingredient_ids:
            if label not in merged:
                merged.append(label)
        if merged != existing_list:
            platform_metadata["reviewed_ingredient_ids"] = merged
            next_data["platform_metadata"] = platform_metadata
            changed = True

    variant_shades = evidence.get("variant_shades") or {}
    variants = next_data.get("variants")
    if isinstance(variants, list) and isinstance(variant_shades, dict):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            variant_id = normalize_non_empty_string(
                variant.get("variant_id") or variant.get("id") or variant.get("sku")
            )
            shade_values = [
                normalize_non_empty_string(item)
                for item in (variant_shades.get(variant_id) or [])
                if normalize_non_empty_string(item)
            ]
            if not shade_values:
                continue

            variant_platform_metadata = (
                copy.deepcopy(variant.get("platform_metadata"))
                if isinstance(variant.get("platform_metadata"), dict)
                else {}
            )
            primary_shade = shade_values[0]
            variant_changed = False
            if variant_platform_metadata.get("shade_name_text") != primary_shade:
                variant_platform_metadata["shade_name_text"] = primary_shade
                changed = True
                variant_changed = True
            if primary_shade.isdigit() and variant_platform_metadata.get("shade_code") != primary_shade:
                variant_platform_metadata["shade_code"] = primary_shade
                changed = True
                variant_changed = True
            if variant_changed:
                variant["platform_metadata"] = variant_platform_metadata

    return next_data, changed


async def hydrate_product_payloads_from_attached_seed_runtime_evidence(
    *,
    merchant_id: str,
    platform: str,
    product_payloads: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not product_payloads:
        return product_payloads

    product_keys: Dict[str, int] = {}
    for index, payload in enumerate(product_payloads):
        platform_product_id = normalize_non_empty_string(payload.get("product_id") or payload.get("id"))
        if not platform_product_id:
            continue
        product_keys[build_product_key(merchant_id=merchant_id, platform=platform, platform_product_id=platform_product_id)] = index

    if not product_keys:
        return product_payloads

    try:
        seed_rows = await database.fetch_all(
            """
            SELECT id, title, canonical_url, destination_url, category, seed_data,
                   attached_product_key, attached_variant_id
            FROM external_product_seeds
            WHERE status = 'active'
              AND attached_product_key IS NOT NULL
              AND attached_product_key LIKE :product_key_prefix
            """,
            {"product_key_prefix": f"{merchant_id}|{platform}|%"},
        )
    except Exception:
        return product_payloads

    grouped_seed_rows: Dict[str, List[Dict[str, Any]]] = {}
    source_refs: List[str] = []
    for row in seed_rows or []:
        seed_row = dict(row)
        product_key = normalize_non_empty_string(seed_row.get("attached_product_key"))
        if product_key not in product_keys:
            continue
        grouped_seed_rows.setdefault(product_key, []).append(seed_row)
        for alias in _seed_row_source_ref_aliases(seed_row):
            if alias not in source_refs:
                source_refs.append(alias)

    if not grouped_seed_rows:
        return product_payloads

    kb_rows: List[Dict[str, Any]] = []
    if source_refs:
        try:
            kb_rows = await asyncio.to_thread(
                fetch_pci_kb_runtime_evidence_rows_by_source_refs_sync,
                source_refs,
            )
        except RuntimeError:
            kb_rows = []

    evidence_by_key = build_attached_seed_runtime_evidence_by_product_key(
        seed_rows=[row for rows in grouped_seed_rows.values() for row in rows],
        kb_rows=kb_rows,
    )

    hydrated_payloads: List[Dict[str, Any]] = list(product_payloads)
    for product_key, index in product_keys.items():
        evidence = evidence_by_key.get(product_key)
        if not evidence:
            continue
        hydrated_payload, changed = apply_attached_seed_runtime_evidence_to_product_data(
            product_data=hydrated_payloads[index],
            evidence=evidence,
        )
        if changed:
            hydrated_payloads[index] = hydrated_payload

    return hydrated_payloads
