"""Sandbox catalog copy: prod's catalog tables -> a GCS object -> the partner sandbox's database.
One way. Design: claude.ai artifact N72GzumjqaW8h7SCE96jt7, build step 6.

    python -m jobs.sandbox_catalog_copy export   # runs in PROD (PIVOTA_ENV=production)
    python -m jobs.sandbox_catalog_copy import   # runs in the SANDBOX (PIVOTA_ENV=sandbox)

Both read CATALOG_COPY_GCS_URI (gs://<bucket>/<prefix>, e.g. gs://pivota-sandbox-catalog-drop/catalog)
and DATABASE_URL. Neither side ever holds the other's database credential: prod writes objects it
cannot read back, the sandbox reads objects and writes only its own database.

EXPORT (prod). One REPEATABLE READ, READ ONLY transaction, so every table comes from the same
snapshot and the job is incapable of writing to prod's database. Per table it reads prod's own
column list, removes the columns the spec drops, REFUSES if any remaining column name looks like
personal data or a secret (see RISKY_COLUMN_TOKENS) and the spec has not classified it, then
streams ``COPY (SELECT ...) TO STDOUT (FORMAT csv)`` into ``<prefix>/<stamp>/<table>.csv.gz``.
``<prefix>/<stamp>/_MANIFEST.json`` (columns, row counts, sha256 per file, the commit) is written
LAST, so a stamp without a manifest is an unfinished export and is never imported. Objects are
created with ifGenerationMatch=0: an export never overwrites anything.

IMPORT (sandbox). Refuses unless PIVOTA_ENV=sandbox and config.sandbox_guard passes (which also
refuses a DATABASE_URL naming a prod host, project or IP). Picks the newest stamp that has a
manifest, re-applies the export's column rules to the manifest (it does not trust the export),
checks every file's sha256 and row count, then in ONE transaction: stages each table, deletes the
sandbox rows the new copy no longer has (children first), upserts the rest (parents first), bumps
serial sequences, and checks each table's row count equals the manifest's. Any mismatch rolls the
whole import back and the previous copy stays.

PRICES keep their values. Freshness is the gateway's call (decision 4: copied prices are treated as
fresh in the sandbox and every answer is marked sandbox:true).

THE TABLE LIST is ``CATALOG_TABLES`` with a reason per table; ``EXCLUDED_TABLES`` names, with a
reason, the tables the serving path reads that are NOT copied. docs/sandbox/catalog_copy.md is
the human-readable version.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as _dt
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("pivota.sandbox_catalog_copy")

FORMAT_VERSION = 1
MANIFEST_NAME = "_MANIFEST.json"
_STAMP_RE = re.compile(r"^\d{8}T\d{4}Z$")


class CopyRefused(RuntimeError):
    """The copy will not run, or will not accept what it was given. The message never holds data."""


# ---------------------------------------------------------------------------------------------
# What is copied
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    name: str
    why: str
    #: Columns never copied: personal data, secrets, staff identities, free text.
    drop: Tuple[str, ...] = ()
    #: When set, ONLY these columns are copied (restricted tables: merchants and stores).
    keep: Optional[Tuple[str, ...]] = None
    #: column -> SQL literal selected IN PLACE of the column's value (placeholders for NOT NULL
    #: personal columns, and safety overrides such as realtime_enabled=false).
    replace: Mapping[str, str] = field(default_factory=dict)
    #: column -> why a column whose NAME looks risky is catalog data.
    allow_risky: Mapping[str, str] = field(default_factory=dict)
    #: Table created at runtime by a schema guard; absent in prod = recorded, not fatal.
    optional: bool = False


CATALOG_TABLES: Tuple[TableSpec, ...] = (
    # --- the catalog itself ---------------------------------------------------------------
    TableSpec("catalog_merchants", "sellers: name, platform, indexable flag; search joins it on every lane"),
    TableSpec("catalog_products", "products: titles, brands, images, markets"),
    TableSpec("catalog_skus", "variants: options, GTINs; get_product and the Reap quote read them"),
    TableSpec("catalog_offers", "offers: price, currency, stock per seller; get_offers and the Reap quote"),
    TableSpec("catalog_payment_incentives", "payment-method incentives shown on offers",
              allow_risky={"card_network": "the card scheme an incentive applies to (visa, ...), not a card"}),
    TableSpec("catalog_offer_incentive_links", "which incentive applies to which offer"),
    TableSpec("catalog_incentive_rules", "incentive eligibility rules"),
    TableSpec("beauty_product_profiles", "beauty attributes search filters on"),
    TableSpec("beauty_sku_ingredients", "ingredient lists per SKU",
              allow_risky={"concentration_notes_json": "ingredient concentration notes from the product page"}),
    TableSpec("beauty_usage_guides", "usage guidance on the product detail"),
    TableSpec("beauty_shades", "shade data on the product detail"),
    TableSpec("beauty_content_assets", "product content assets (image/video URLs)"),
    TableSpec("beauty_compatibility_rules", "product compatibility rules"),
    TableSpec("external_product_seeds", "reviewed external listings: search, offers and the Reap quote read them",
              drop=("notes", "created_by_employee_id")),
    TableSpec("products_cache", "the product cache search and get_product read"),
    TableSpec("product_group_members", "product grouping get_offers expands"),
    TableSpec("identity_resolution_proposals", "applied identity merges get_offers honours",
              drop=("decided_by",), optional=True),
    TableSpec("catalog_row_trust", "the serving verdict (serving_decision='public') the gateway's catalog, "
              "seed-fastpath and canonical-PDP lanes INNER JOIN; without it the sandbox serves nothing"),
    TableSpec("agent_pdp_view", "the denormalised product view search serves",
              allow_risky={"refreshed_by_proposal_id": "the identity proposal that refreshed the row, not a person"}),
    TableSpec("index_pipeline_state", "search index readiness per product"),
    TableSpec("x402_exchange_rates", "FX reference rates for budget conversion", optional=True),
    TableSpec("product_quality_snapshot", "ranking quality scores"),
    TableSpec("product_enrichment", "ranking enrichment", drop=("updated_by_employee_id", "updated_by_email"),
              optional=True),
    TableSpec("brand_claims", "verified-brand status on sellers",
              drop=("challenge_token", "proof_ref", "created_by_user_id"), optional=True),
    # --- read directly by the gateway's serving lanes (list from the sandbox gateway session,
    #     gateway-db-tables-read.md, 25 Nov). "optional" where the table is created by the
    #     gateway's own migrations or has no DDL in either repo: absent in prod = recorded. -----
    TableSpec("content_canonical_election", "which listing is canonical per content key (search, PDP route ids)"),
    TableSpec("catalog_source_quarantine", "quarantined sources the serving lanes keep hidden",
              drop=("created_by", "revoked_by")),
    TableSpec("merchant_product_overlay", "approved PDP content overlays", drop=("approved_by",)),
    TableSpec("product_evidence", "product claims and required disclaimers per market"),
    TableSpec("pdp_identity_listing", "the PDP identity graph's listings (search, discovery, recommendations)",
              optional=True, allow_risky={"matched_by_rule": "the matcher rule's name, not a person"}),
    TableSpec("pdp_identity_override", "identity graph overrides", drop=("created_by",), optional=True),
    TableSpec("product_beauty_attributes", "beauty attributes on PDP and recommendations", drop=("audit_notes",),
              optional=True),
    TableSpec("product_relationship_edges", "product relationship graph edges", optional=True),
    TableSpec("relationship_candidate_labels", "relationship graph labels", optional=True),
    TableSpec("relgraph_reviewed_ingredient_evidence", "reviewed ingredient evidence for the graph", optional=True),
    TableSpec("relgraph_reviewed_seller_evidence", "reviewed seller evidence for the graph", optional=True),
    TableSpec("aurora_product_intel_kb", "product intelligence KB the serving index reads", optional=True),
    TableSpec("aurora_dupe_kb", "dupe/alternative KB", drop=("verified_by",), optional=True),
    TableSpec("aurora_ingredient_research_kb", "ingredient research KB", optional=True),
    TableSpec("aurora_travel_context_kb", "travel-context product KB (destinations, not travellers)", optional=True),
    TableSpec("taxonomy_view", "category taxonomy views", optional=True),
    TableSpec("taxonomy_view_category", "categories per taxonomy view", optional=True),
    TableSpec("canonical_category", "canonical categories", optional=True,
              allow_risky={"replaced_by_id": "the replacing category's id, not a person"}),
    TableSpec("category_localization", "category names per locale", optional=True),
    TableSpec("merchant_category_mapping", "merchant category -> canonical category", drop=("updated_by",),
              optional=True),
    TableSpec("ops_category_override", "category pins/hides set by ops", drop=("updated_by",), optional=True),
    TableSpec("brands", "brands the relationship graph recalls", optional=True),
    # --- purchase readiness (Reap path) ---------------------------------------------------
    TableSpec("merchant_purchasability", "whether a merchant can take an agent purchase", optional=True,
              allow_risky={"card_available": "whether the store accepts cards, not a card"}),
    TableSpec("tierb_cart_link_eligibility", "cart-link eligibility the Reap purchase checks", optional=True),
    TableSpec("reap_agentic_eligibility", "the reviewed Reap merchant allowlist", optional=True),
    TableSpec("enrichment_cart_variant_proofs", "variant price witnesses the Reap quote checks", optional=True),
    # --- restricted: merchants and stores (secrets and contact data live on these rows) ---
    TableSpec(
        "merchant_onboarding",
        "merchant ids, names and domains search and seller identity join; nothing else",
        keep=("id", "merchant_id", "business_name", "store_url", "operating_mode", "website", "region",
              "status", "psp_connected", "psp_type", "mcp_connected", "mcp_platform", "mcp_shop_domain",
              "created_at", "updated_at", "verified_at", "is_test", "contact_email"),
        # contact_email is NOT NULL; a placeholder, never the merchant's address.
        replace={"contact_email": "'sandbox-merchant@example.invalid'"},
    ),
    TableSpec(
        "merchant_stores",
        "store ids, platforms and domains; never the store credential",
        keep=("store_id", "merchant_id", "platform", "name", "domain", "status", "connected_at",
              "last_sync", "product_count", "is_primary", "created_at", "realtime_enabled",
              "query_ttl_seconds"),
        # The live-fetch path reads realtime_enabled + api_key; neither works in the sandbox.
        replace={"realtime_enabled": "false"},
    ),
)

#: Read on the serving path, NOT copied. A test asserts none of these appear in CATALOG_TABLES.
EXCLUDED_TABLES: Mapping[str, str] = {
    "orders": "buyer orders (search personalisation reads them; the sandbox has its own, empty)",
    "order_events": "order history",
    "agents": "partner agents and key hashes (step 7 syncs test identities separately)",
    "agent_identity_issuers": "partner issuers (step 7)",
    "buyer_identity_links": "buyers",
    "shop_users": "buyers",
    "reap_agentic_buyer_refs": "buyers",
    "reap_agentic_purchases": "purchase ledger",
    "agent_purchase_consents": "consents",
    "agent_purchase_ledger": "ledger",
    "a2a_rail_ledger": "ledger",
    "commerce_attribution_edges": "attribution (buyer-linked)",
    "conversion_click_claims": "attribution (buyer-linked)",
    "merchant_psps": "PSP secret keys",
    "merchant_tasks": "internal ops queue",
    "creator_picks": "creators are real people",
    "product_reviews": "reviewer text and names",
    "media_assets": "uploads",
    "commissions": "money",
    # Read by the gateway directly, NOT copied (gateway-db-tables-read.md):
    "pdp_identity_review_queue": "a review workflow with free-text reviewer notes; serving reads the graph, not the queue",
    "reco_label_suggestions": "carries session_id / request_id (linkable to users)",
    "creator_category_override": "keyed by creator_id; creators are real people",
    "external_seed_corrections": "an ops audit ledger (before/after payloads), not serving data",
    "external_offers_cache": "a cache of LIVE store reads; the sandbox never live-reads",
    "external_offer_snapshots": "read only by the ops index inspector",
    "products_cache_embeddings": "pgvector embeddings: size and an extension dependency; the vector lane is a follow-up",
    "products_cache_embeddings_fallback": "as products_cache_embeddings",
    "aurora_users": "consumer app users", "aurora_user_profiles": "consumer profiles",
    "aurora_account_profiles": "consumer profiles", "aurora_account_skin_logs": "consumer skin logs",
    "aurora_auth_challenges": "consumer auth", "aurora_auth_sessions": "consumer auth",
    "aurora_identity_links": "consumer identities", "aurora_activity_events": "consumer activity",
    "aurora_activity_details": "consumer activity", "aurora_skin_logs": "consumer skin logs",
    "aurora_skin_diagnosis_artifacts": "consumer diagnoses", "aurora_routine_versions": "consumer routines",
    "aurora_ingredient_plans": "consumer plans", "aurora_user_experiment_logs": "consumer experiments",
    "service_bookings": "bookings (people)", "service_booking_notifications_outbox": "booking notifications (people)",
    "service_providers": "service providers and their contacts", "service_listings": "the services feature, not in v0",
    "product_to_service_taxonomy": "the services feature, not in v0",
    # The gateway's runtime tables start EMPTY in the sandbox; they must exist (gateway migrations).
    "commerce_kv": "gateway runtime state", "gateway_caller_limit_counters": "gateway runtime state",
    "gateway_caller_limit_salts": "gateway runtime secret salts", "external_seed_image_cache_jobs": "gateway job queue",
    "relationship_graph_anchor_attempts": "gateway runtime state",
}

#: A column whose name has one of these tokens (split on "_") is refused unless dropped or
#: classified in allow_risky. "_by" anywhere (created_by, decided_by_*) is staff identity.
RISKY_COLUMN_TOKENS = frozenset({
    "email", "phone", "password", "passwd", "secret", "token", "tokens", "credential", "credentials",
    "kyc", "ssn", "address", "customer", "buyer", "employee", "iban", "contact", "notes", "note",
    "by", "ip", "dob", "birthday",
})
RISKY_COLUMN_PAIRS = frozenset({("api", "key"), ("private", "key"), ("user", "id"), ("first", "name"),
                                ("last", "name"), ("full", "name"), ("account", "number")})


def column_is_risky(column: str) -> bool:
    parts = [p for p in column.lower().split("_") if p]
    if any(p in RISKY_COLUMN_TOKENS for p in parts):
        return True
    return any(pair in RISKY_COLUMN_PAIRS for pair in zip(parts, parts[1:]))


def plan_columns(spec: TableSpec, existing: Sequence[str]) -> List[Tuple[str, str]]:
    """``[(column, select_expression)]`` to copy from a table whose columns are ``existing``.

    Raises CopyRefused when a kept column does not exist, or a remaining column looks risky and is
    neither dropped, replaced nor classified. The order follows ``existing`` (the table's own).
    """
    if spec.keep is not None:
        # A kept column the source lacks (is_test, realtime_enabled arrived by migration) is simply
        # not copied; the sandbox column's default applies.
        chosen = [c for c in existing if c in spec.keep]
    else:
        chosen = [c for c in existing if c not in spec.drop]
    out: List[Tuple[str, str]] = []
    for column in chosen:
        if column in spec.replace:
            out.append((column, "%s AS %s" % (spec.replace[column], _q(column))))
            continue
        if column_is_risky(column) and column not in spec.allow_risky:
            raise CopyRefused(
                "table %s column %s looks like personal data or a secret; drop it or classify it "
                "in CATALOG_TABLES before copying" % (spec.name, column)
            )
        out.append((column, _q(column)))
    if not out:
        raise CopyRefused("table %s: no columns left to copy" % spec.name)
    return out


def _q(ident: str) -> str:
    return '"%s"' % ident.replace('"', '""')


def _spec_by_name(specs: Iterable[TableSpec]) -> Dict[str, TableSpec]:
    return {s.name: s for s in specs}


# ---------------------------------------------------------------------------------------------
# Object store: GCS in the jobs, a directory in the tests
# ---------------------------------------------------------------------------------------------


class LocalDirStore:
    """A directory standing in for a bucket prefix. Same no-overwrite rule as the GCS writer."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, name: str, data: bytes) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "xb") as handle:  # x: never overwrite, as ifGenerationMatch=0 on GCS
                handle.write(data)
        except FileExistsError:
            raise CopyRefused("object already exists; an export never overwrites (%s)" % name) from None

    def get(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def list(self, prefix: str = "") -> List[str]:
        base = self.root
        if not base.exists():
            return []
        names = [str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()]
        return sorted(n for n in names if n.startswith(prefix))


class GcsStore:  # pragma: no cover - exercised only on Cloud Run
    """The GCS JSON API with the Cloud Run service account's token (metadata server).

    No client library: google-cloud-storage is not a dependency of this service. Writes use
    ifGenerationMatch=0 (create only), which is also all the exporter's IAM role allows.
    """

    _TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(self, uri: str):
        if not uri.startswith("gs://"):
            raise CopyRefused("CATALOG_COPY_GCS_URI must start with gs://")
        bucket, _, prefix = uri[5:].partition("/")
        if not bucket:
            raise CopyRefused("CATALOG_COPY_GCS_URI names no bucket")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _name(self, name: str) -> str:
        return "%s/%s" % (self.prefix, name) if self.prefix else name

    @staticmethod
    def _ok(response, what: str) -> None:
        """A refusal naming the operation and the HTTP status, never a traceback or a body."""
        if response.status_code != 200:
            raise CopyRefused("GCS answered HTTP %d for %s" % (response.status_code, what))

    def _token(self) -> str:
        import httpx

        response = httpx.get(self._TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=10)
        self._ok(response, "the metadata token")
        return response.json()["access_token"]

    def put(self, name: str, data: bytes) -> None:
        import httpx

        response = httpx.post(
            "https://storage.googleapis.com/upload/storage/v1/b/%s/o" % self.bucket,
            params={"uploadType": "media", "name": self._name(name), "ifGenerationMatch": "0"},
            content=data,
            headers={"Authorization": "Bearer " + self._token(), "Content-Type": "application/octet-stream"},
            timeout=300,
        )
        if response.status_code == 412:
            raise CopyRefused("object already exists; an export never overwrites (%s)" % name)
        self._ok(response, "create %s" % name)

    def get(self, name: str) -> bytes:
        import urllib.parse

        import httpx

        response = httpx.get(
            "https://storage.googleapis.com/storage/v1/b/%s/o/%s"
            % (self.bucket, urllib.parse.quote(self._name(name), safe="")),
            params={"alt": "media"},
            headers={"Authorization": "Bearer " + self._token()},
            timeout=300,
        )
        self._ok(response, "read %s" % name)
        return response.content

    def list(self, prefix: str = "") -> List[str]:
        import httpx

        names: List[str] = []
        token = None
        full = self._name(prefix)
        while True:
            params = {"prefix": full, "fields": "items(name),nextPageToken"}
            if token:
                params["pageToken"] = token
            response = httpx.get(
                "https://storage.googleapis.com/storage/v1/b/%s/o" % self.bucket,
                params=params, headers={"Authorization": "Bearer " + self._token()}, timeout=60,
            )
            self._ok(response, "list")
            body = response.json()
            for item in body.get("items", []):
                n = item["name"]
                names.append(n[len(self.prefix) + 1:] if self.prefix else n)
            token = body.get("nextPageToken")
            if not token:
                return sorted(names)


# ---------------------------------------------------------------------------------------------
# Database helpers (asyncpg connections; `schema` is "public" in the jobs)
# ---------------------------------------------------------------------------------------------


async def _table_columns(conn, schema: str, table: str, *, copyable_only: bool = True) -> List[str]:
    rows = await conn.fetch(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_schema = $1 AND table_name = $2
           AND ($3::bool IS FALSE OR is_generated = 'NEVER')
         ORDER BY ordinal_position
        """,
        schema, table, copyable_only,
    )
    return [r["column_name"] for r in rows]


_EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _count_csv_records(raw: bytes) -> int:
    return sum(1 for _ in csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))


def _gzip(raw: bytes) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as handle:
        handle.write(raw)
    return out.getvalue()


# ---------------------------------------------------------------------------------------------
# Export (prod)
# ---------------------------------------------------------------------------------------------


def utc_stamp(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%MZ")


async def export_catalog(conn, store, *, stamp: str, commit: Optional[str] = None,
                         specs: Sequence[TableSpec] = CATALOG_TABLES, schema: str = "public",
                         statement_timeout: str = "15min") -> Dict[str, Any]:
    if not _STAMP_RE.match(stamp):
        raise CopyRefused("stamp must look like YYYYmmddTHHMMZ")
    tables: List[Dict[str, Any]] = []
    absent: List[str] = []
    files: List[Tuple[str, bytes]] = []
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '%s'" % statement_timeout.replace("'", ""))
        for spec in specs:
            existing = await _table_columns(conn, schema, spec.name)
            if not existing:
                if spec.optional:
                    absent.append(spec.name)
                    continue
                raise CopyRefused("table %s does not exist in the source" % spec.name)
            plan = plan_columns(spec, existing)
            query = "SELECT %s FROM %s.%s" % (", ".join(e for _, e in plan), _q(schema), _q(spec.name))
            buf = io.BytesIO()
            await conn.copy_from_query(query, output=buf, format="csv")
            raw = buf.getvalue()
            rows = await conn.fetchval("SELECT count(*) FROM %s.%s" % (_q(schema), _q(spec.name)))
            if _count_csv_records(raw) != rows:
                raise CopyRefused("table %s: CSV record count differs from the snapshot's" % spec.name)
            data = _gzip(raw)
            fname = "%s/%s.csv.gz" % (stamp, spec.name)
            files.append((fname, data))
            tables.append({
                "name": spec.name,
                "columns": [c for c, _ in plan],
                "rows": int(rows),
                "file": fname,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                # A review signal only (JSON blobs cannot be checked by column name): how many
                # email-shaped strings the copied values hold. Brand support addresses count too.
                "email_like_values": len(_EMAIL_RE.findall(raw)),
            })
    for fname, data in files:
        store.put(fname, data)
    manifest = {
        "format_version": FORMAT_VERSION,
        "stamp": stamp,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "commit": commit,
        "tables": tables,
        "absent_optional_tables": absent,
    }
    store.put("%s/%s" % (stamp, MANIFEST_NAME), json.dumps(manifest, indent=1, sort_keys=True).encode())
    return manifest


# ---------------------------------------------------------------------------------------------
# Import (sandbox)
# ---------------------------------------------------------------------------------------------


def newest_complete_stamp(store) -> Optional[str]:
    stamps = set()
    for name in store.list(""):
        head, _, tail = name.partition("/")
        if tail == MANIFEST_NAME and _STAMP_RE.match(head):
            stamps.add(head)
    return max(stamps) if stamps else None


def validate_manifest(manifest: Mapping[str, Any], specs: Sequence[TableSpec] = CATALOG_TABLES) -> None:
    """Re-apply the export's rules. The importer does not trust what the exporter wrote."""
    if manifest.get("format_version") != FORMAT_VERSION:
        raise CopyRefused("manifest format_version is not %d" % FORMAT_VERSION)
    by_name = _spec_by_name(specs)
    seen = set()
    for entry in manifest.get("tables") or []:
        name = entry.get("name")
        spec = by_name.get(name)
        if spec is None:
            raise CopyRefused("manifest carries table %s, which is not in CATALOG_TABLES" % name)
        if name in seen:
            raise CopyRefused("manifest lists table %s twice" % name)
        seen.add(name)
        columns = list(entry.get("columns") or [])
        if not columns:
            raise CopyRefused("manifest table %s has no columns" % name)
        for column in columns:
            if column in spec.drop:
                raise CopyRefused("manifest table %s carries dropped column %s" % (name, column))
            if spec.keep is not None and column not in spec.keep:
                raise CopyRefused("manifest table %s carries column %s outside its keep list" % (name, column))
            if column_is_risky(column) and column not in spec.allow_risky and column not in spec.replace:
                raise CopyRefused("manifest table %s carries risky column %s" % (name, column))
    absent = set(manifest.get("absent_optional_tables") or [])
    for spec in specs:
        if spec.name not in seen and not (spec.optional and spec.name in absent):
            raise CopyRefused("manifest is missing table %s" % spec.name)


async def _fk_order(conn, schema: str, names: Sequence[str]) -> List[str]:
    """``names`` ordered parents first, by the target database's own foreign keys."""
    rows = await conn.fetch(
        """
        SELECT child.relname AS child, parent.relname AS parent
          FROM pg_constraint c
          JOIN pg_class child ON child.oid = c.conrelid
          JOIN pg_class parent ON parent.oid = c.confrelid
          JOIN pg_namespace n ON n.oid = child.relnamespace
         WHERE c.contype = 'f' AND n.nspname = $1
        """,
        schema,
    )
    wanted = set(names)
    deps = {n: set() for n in names}
    for r in rows:
        if r["child"] in wanted and r["parent"] in wanted and r["child"] != r["parent"]:
            deps[r["child"]].add(r["parent"])
    ordered: List[str] = []
    pending = list(names)
    while pending:
        ready = [n for n in pending if deps[n] <= set(ordered)]
        if not ready:
            raise CopyRefused("foreign-key cycle among copied tables: %s" % ",".join(sorted(pending)))
        for n in ready:
            ordered.append(n)
            pending.remove(n)
    return ordered


async def _primary_key(conn, schema: str, table: str) -> List[str]:
    rows = await conn.fetch(
        """
        SELECT a.attname
          FROM pg_index i
          JOIN pg_class t ON t.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
          JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
         WHERE i.indisprimary AND n.nspname = $1 AND t.relname = $2
         ORDER BY k.ord
        """,
        schema, table,
    )
    return [r["attname"] for r in rows]


async def _bump_sequences(conn, schema: str, table: str) -> None:
    rows = await conn.fetch(
        """
        SELECT column_name, pg_get_serial_sequence(quote_ident($1) || '.' || quote_ident($2), column_name) AS seq
          FROM information_schema.columns
         WHERE table_schema = $1 AND table_name = $2
        """,
        schema, table,
    )
    for r in rows:
        if r["seq"]:
            await conn.execute(
                "SELECT setval($1::regclass, GREATEST((SELECT COALESCE(max(%s), 0) FROM %s.%s), 1))"
                % (_q(r["column_name"]), _q(schema), _q(table)),
                r["seq"],
            )


async def import_catalog(conn, store, *, stamp: Optional[str] = None,
                         specs: Sequence[TableSpec] = CATALOG_TABLES, schema: str = "public") -> Dict[str, Any]:
    stamp = stamp or newest_complete_stamp(store)
    if not stamp:
        raise CopyRefused("no complete export (no stamp with a manifest) in the store")
    manifest = json.loads(store.get("%s/%s" % (stamp, MANIFEST_NAME)).decode("utf-8"))
    if manifest.get("stamp") != stamp:
        raise CopyRefused("manifest stamp does not match its folder")
    validate_manifest(manifest, specs)

    # Download and verify everything before touching the database.
    payloads: Dict[str, bytes] = {}
    entries = {e["name"]: e for e in manifest["tables"]}
    for name, entry in entries.items():
        if entry.get("file") != "%s/%s.csv.gz" % (stamp, name):
            raise CopyRefused("manifest table %s points outside its stamp" % name)
        data = store.get(entry["file"])
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise CopyRefused("table %s: file sha256 does not match the manifest" % name)
        raw = gzip.decompress(data)
        if _count_csv_records(raw) != entry.get("rows"):
            raise CopyRefused("table %s: file row count does not match the manifest" % name)
        payloads[name] = raw

    order = await _fk_order(conn, schema, list(entries))
    report: Dict[str, Any] = {"stamp": stamp, "commit": manifest.get("commit"), "tables": {}}
    async with conn.transaction():
        staged: Dict[str, str] = {}
        for name in order:
            columns = entries[name]["columns"]
            target = await _table_columns(conn, schema, name, copyable_only=False)
            if not target:
                raise CopyRefused("table %s does not exist in the sandbox database" % name)
            missing = [c for c in columns if c not in target]
            if missing:
                raise CopyRefused("sandbox table %s lacks column(s) %s" % (name, ",".join(missing)))
            stage = "_sandbox_copy_stage_%d" % len(staged)
            await conn.execute(
                "CREATE TEMP TABLE %s ON COMMIT DROP AS SELECT %s FROM %s.%s WITH NO DATA"
                % (_q(stage), ", ".join(_q(c) for c in columns), _q(schema), _q(name))
            )
            await conn.copy_to_table(stage, source=io.BytesIO(payloads[name]), columns=columns, format="csv")
            # Re-apply the spec's replacements HERE too: a placeholder the export was meant to
            # write (contact_email) or a safety override (realtime_enabled=false) holds even if
            # the export did not apply it.
            spec = _spec_by_name(specs)[name]
            for column, expr in spec.replace.items():
                if column in columns:
                    await conn.execute("UPDATE %s SET %s = %s" % (_q(stage), _q(column), expr))
            staged[name] = stage
        # Delete what the new copy no longer has, children first.
        for name in reversed(order):
            pk = await _primary_key(conn, schema, name)
            columns = entries[name]["columns"]
            stage = staged[name]
            if pk and all(c in columns for c in pk):
                match = " AND ".join("s.%s = t.%s" % (_q(c), _q(c)) for c in pk)
                deleted = await conn.execute(
                    "DELETE FROM %s.%s t WHERE NOT EXISTS (SELECT 1 FROM %s s WHERE %s)"
                    % (_q(schema), _q(name), _q(stage), match)
                )
            else:
                deleted = await conn.execute("DELETE FROM %s.%s" % (_q(schema), _q(name)))
            report["tables"][name] = {"deleted": int(deleted.split()[-1])}
        # Upsert, parents first.
        for name in order:
            pk = await _primary_key(conn, schema, name)
            columns = entries[name]["columns"]
            col_sql = ", ".join(_q(c) for c in columns)
            sql = "INSERT INTO %s.%s (%s) OVERRIDING SYSTEM VALUE SELECT %s FROM %s" % (
                _q(schema), _q(name), col_sql, col_sql, _q(staged[name]))
            if pk and all(c in columns for c in pk):
                rest = [c for c in columns if c not in pk]
                conflict = ", ".join(_q(c) for c in pk)
                if rest:
                    sql += " ON CONFLICT (%s) DO UPDATE SET %s" % (
                        conflict, ", ".join("%s = EXCLUDED.%s" % (_q(c), _q(c)) for c in rest))
                else:
                    sql += " ON CONFLICT (%s) DO NOTHING" % conflict
            written = await conn.execute(sql)
            await _bump_sequences(conn, schema, name)
            count = await conn.fetchval("SELECT count(*) FROM %s.%s" % (_q(schema), _q(name)))
            if count != entries[name]["rows"]:
                raise CopyRefused("table %s: %d rows after import, manifest says %d"
                                  % (name, count, entries[name]["rows"]))
            report["tables"][name].update({"written": int(written.split()[-1]), "rows": int(count)})
    return report


# ---------------------------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------------------------


def _store_from_env(env: Mapping[str, str]):
    uri = (env.get("CATALOG_COPY_GCS_URI") or "").strip()
    if not uri:
        raise CopyRefused("CATALOG_COPY_GCS_URI is not set")
    return GcsStore(uri)


async def _connect(env: Mapping[str, str]):
    import asyncpg

    dsn = (env.get("DATABASE_URL") or "").strip()
    if not dsn:
        raise CopyRefused("DATABASE_URL is not set")
    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))


def check_export_environment(env: Mapping[str, str]) -> None:
    from config.platform import is_production, is_sandbox

    if is_sandbox(env):
        raise CopyRefused("the export runs in prod, never in the sandbox")
    if not is_production(env):
        raise CopyRefused("the export runs only where PIVOTA_ENV resolves to production")


def check_import_environment(env: Mapping[str, str]) -> None:
    """The import is destructive on its target (delete-missing with the FKs' cascades, placeholder
    overwrites), so beyond the guard's refusal of prod names the target must POSITIVELY be the
    sandbox's database (config.sandbox_identity)."""
    from config.platform import is_sandbox
    from config.sandbox_guard import enforce_sandbox_guard
    from config.sandbox_identity import sandbox_database_refusal

    if not is_sandbox(env):
        raise CopyRefused("the import runs only in the sandbox (PIVOTA_ENV=sandbox)")
    enforce_sandbox_guard(env)
    refusal = sandbox_database_refusal(env)
    if refusal:
        raise CopyRefused(refusal)


async def _run(mode: str, env: Mapping[str, str]) -> Dict[str, Any]:
    from config.platform import commit_sha

    if mode == "export":
        check_export_environment(env)
    else:
        check_import_environment(env)
    store = _store_from_env(env)
    conn = await _connect(env)
    try:
        if mode == "export":
            manifest = await export_catalog(conn, store, stamp=utc_stamp(), commit=commit_sha(env))
            return {"stamp": manifest["stamp"],
                    "tables": {t["name"]: t["rows"] for t in manifest["tables"]},
                    "absent_optional_tables": manifest["absent_optional_tables"]}
        return await import_catalog(conn, store)
    finally:
        await conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.sandbox_catalog_copy")
    parser.add_argument("mode", choices=("export", "import"))
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args.mode, os.environ))
    except CopyRefused as exc:
        print("sandbox_catalog_copy %s REFUSED: %s" % (args.mode, exc), file=sys.stderr)
        return 2
    print("sandbox_catalog_copy %s DONE %s" % (args.mode, json.dumps(result, sort_keys=True)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
