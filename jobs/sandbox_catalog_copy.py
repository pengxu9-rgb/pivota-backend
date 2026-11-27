"""Sandbox catalog copy: prod's catalog tables -> a GCS object -> the partner sandbox's database.
One way. Design: claude.ai artifact N72GzumjqaW8h7SCE96jt7, build step 6.

    python -m jobs.sandbox_catalog_copy export   # runs in PROD (PIVOTA_ENV=production)
    python -m jobs.sandbox_catalog_copy import   # runs in the SANDBOX (PIVOTA_ENV=sandbox)

Both read CATALOG_COPY_GCS_URI (gs://<bucket>/<prefix>, e.g. gs://pivota-sandbox-catalog-drop/catalog)
and DATABASE_URL. Neither side ever holds the other's database credential: prod writes objects it
cannot read back, the sandbox reads objects and writes only its own database.

EXPORT (prod). One REPEATABLE READ, READ ONLY transaction, so every table comes from the same
snapshot and the job is incapable of writing to prod's database. Per table it reads prod's own
column list and types, removes the columns the spec drops, REFUSES if any remaining column is not
classified in jobs/sandbox_catalog_columns.py (S3: fail closed) or its name looks like personal data
or a secret (see RISKY_COLUMN_TOKENS) and the spec has not allowed it, then runs
``COPY (SELECT ...) TO STDOUT (FORMAT csv)`` and streams it (S5): whole records are scrubbed in
Python (every JSON and text column: person keys, email/phone-shaped values), checked again (the
export checks its own output), gzipped and sent as an unfinished upload of
``<prefix>/<stamp>/<table>.csv.gz``. No table is held in memory. statement_timeout, lock_timeout and
idle_in_transaction_session_timeout are SET LOCAL, and a client-side deadline terminates the session.
Only when every table has passed are the uploads committed.
``<prefix>/<stamp>/_MANIFEST.json`` (columns, row counts, sha256 per file, the commit) is written
LAST, so a stamp without a manifest is an unfinished export and is never imported. Objects are
created with ifGenerationMatch=0: an export never overwrites anything.

IMPORT (sandbox). Refuses unless PIVOTA_ENV=sandbox and config.sandbox_guard passes (which also
refuses a DATABASE_URL naming a prod host, project or IP). Picks the newest stamp that has a
manifest, re-applies the export's column rules to the manifest (it does not trust the export),
checks every file's sha256 and row count and every value against the scrub in one streamed read,
then in ONE transaction: stages each table (streamed again from the same object generation, its
sha256 re-checked), deletes the
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
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("pivota.sandbox_catalog_copy")

#: 2: every column classified (jobs/sandbox_catalog_columns.py) and JSON columns scrubbed (S3). A format-1
#: export predates the scrub, so the importer refuses it.
FORMAT_VERSION = 2
MANIFEST_NAME = "_MANIFEST.json"
#: Streaming (S5): no table is ever held in memory. The export and the import hold at most one CSV
#: record (refused past MAX_RECORD_BYTES), one inflate step, one read chunk and one upload chunk.
MAX_RECORD_BYTES = 256 << 20
INFLATE_STEP_BYTES = 1 << 20
READ_CHUNK_BYTES = 1 << 20
#: A multiple of GCS's 256 KiB resumable-upload quantum.
UPLOAD_CHUNK_BYTES = 8 << 20
#: Prod's load (S5). Each limit is a SET LOCAL in the export's own transaction, so it ends with it.
#: lock_timeout keeps the export from queueing behind a migration's lock (and everything behind
#: it); idle_in_transaction ends a stalled job's snapshot; the deadline terminates the session.
EXPORT_STATEMENT_TIMEOUT = "20min"
EXPORT_LOCK_TIMEOUT = "5s"
EXPORT_IDLE_IN_TRANSACTION_TIMEOUT = "2min"
EXPORT_DEADLINE_SECONDS = 45 * 60
#: PostgreSQL 14+: while a statement runs, the server checks this often that the client is still
#: there, so a session the deadline terminated stops even inside a long scan that sends nothing.
EXPORT_CLIENT_CHECK_INTERVAL = "5s"
MAX_EXPORT_DEADLINE_SECONDS = 2 * 60 * 60
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
    #: A SQL predicate: only matching rows are copied (the export's SELECT and its count both use it).
    where: Optional[str] = None


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
              drop=("created_by", "revoked_by"),
              # ops' free-text reason: a placeholder keeps a NOT NULL column satisfied.
              replace={"reason": "'withheld in the sandbox copy'"}),
    TableSpec("merchant_product_overlay", "approved PDP content overlays", drop=("approved_by",)),
    TableSpec("product_evidence", "product claims and required disclaimers per market"),
    TableSpec("pdp_identity_listing", "the PDP identity graph's listings (search, discovery, recommendations)",
              optional=True, allow_risky={"matched_by_rule": "the matcher rule's name, not a person"}),
    TableSpec("pdp_identity_override", "identity graph overrides", drop=("created_by",), optional=True),
    TableSpec("product_beauty_attributes", "beauty attributes on PDP and recommendations", drop=("audit_notes",),
              optional=True),
    TableSpec("relationship_candidate_labels", "relationship graph labels", optional=True),
    TableSpec("relgraph_reviewed_ingredient_evidence", "reviewed ingredient evidence for the graph", optional=True),
    TableSpec("relgraph_reviewed_seller_evidence", "reviewed seller evidence for the graph", optional=True),
    TableSpec("aurora_product_intel_kb", "product intelligence KB the serving index reads", optional=True),
    TableSpec("aurora_dupe_kb", "dupe/alternative KB", drop=("verified_by",), optional=True),
    TableSpec("aurora_ingredient_research_kb", "ingredient research KB", optional=True,
              # query_norm is built from a user's Aurora ingredient query: only finished rows, and only
              # short ones (an ingredient name, not a message), are copied; the value scrub applies too.
              where="status = 'ready' AND char_length(query_norm) <= 64"),
    TableSpec("aurora_travel_context_kb", "travel-context product KB (destinations, not travellers)", optional=True),
    TableSpec("taxonomy_view", "category taxonomy views", optional=True),
    TableSpec("taxonomy_view_category", "categories per taxonomy view", optional=True),
    TableSpec("canonical_category", "canonical categories", optional=True,
              allow_risky={"replaced_by_id": "the replacing category's id, not a person"}),
    TableSpec("category_localization", "category names per locale", optional=True),
    TableSpec("merchant_category_mapping", "merchant category -> canonical category", drop=("updated_by",),
              optional=True),
    TableSpec("ops_category_override", "category pins/hides set by ops", drop=("updated_by",), optional=True,
              replace={"reason": "'withheld in the sandbox copy'"}),
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
    "product_relationship_edges": "a VIEW over relationship_candidate_labels (gateway migration 046/051), which is "
                                  "copied; the view arrives with the gateway's migrations, and an import cannot write a view",
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

#: NEVER copied, whatever a spec or a manifest names: identity, keys, issuers, orders, buyers, consents,
#: ledgers and money. Besides personal data, a copied prod `agents`/`api_keys` row would make a prod key
#: authenticate in the sandbox, and prod's gateway agent is a trusted voucher there (routes/agent_auth.py).
#: An explicit list AND a name pattern (a new `*_tokens` or `buyer_*` table is refused without an edit).
#: Checked when this module loads (a spec naming one cannot exist), by the export before it reads, and
#: by the importer on the manifest.
NEVER_COPY_TABLES = frozenset({
    "agents", "api_keys", "agent_api_keys", "agent_identity_issuers", "agent_oauth_clients",
    "agent_issued_cards", "ap2_trusted_issuers", "orders", "order_events", "buyer_identity_links",
    "shop_users", "shop_user_memberships", "shop_login_otps", "reap_agentic_buyer_refs",
    "reap_agentic_purchases", "agent_purchase_consents", "agent_purchase_ledger", "agent_purchase_events",
    "a2a_rail_ledger", "merchant_psps", "commissions", "users", "employees",
})
_NEVER_COPY_NAME = re.compile(
    r"(^|_)(agents|api_keys?|keys|secrets?|tokens?|credentials?|auth|issuers?|buyers?|orders?|consents?|"
    r"ledgers?|purchases|sessions?|users?|oauth|otps?|cards?|psps?|wallets?|payouts?|refunds?|"
    r"commissions?|bookings?|vault)(_|$)"
)


def never_copy(name: str) -> bool:
    return name in NEVER_COPY_TABLES or bool(_NEVER_COPY_NAME.search(name or ""))


def _assert_no_never_copy(names: Iterable[str], where: str) -> None:
    bad = sorted(n for n in names if never_copy(n))
    if bad:
        raise CopyRefused("%s names table(s) that are never copied: %s" % (where, ",".join(bad)))


#: A column whose name has one of these tokens (split on "_") is refused unless dropped or
#: classified in allow_risky. "_by" anywhere (created_by, decided_by_*) is staff identity.
RISKY_COLUMN_TOKENS = frozenset({
    "email", "emails", "phone", "phones", "password", "passwords", "passwd", "secret", "secrets", "token",
    "tokens", "credential", "credentials", "kyc", "ssn", "address", "addresses", "customer", "customers",
    "buyer", "buyers", "employee", "employees", "iban", "contact", "contacts", "notes", "note", "by", "ip",
    "dob", "birthday",
    # S3 (5d8e3aa73 review): plurals above, and auth material, people, documents, cards, raw payloads.
    "cookie", "cookies", "authorization", "auth", "bearer", "jwt", "session", "sessions", "apikey",
    "username", "usernames", "reviewer", "reviewers", "actor", "actors", "passport", "cvv", "cvc", "pan",
    "headers", "comment", "comments", "remark", "remarks", "memo",
})
RISKY_COLUMN_PAIRS = frozenset({
    ("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key"), ("signing", "key"),
    ("user", "id"), ("user", "name"), ("user", "agent"), ("session", "id"), ("first", "name"),
    ("last", "name"), ("full", "name"), ("account", "number"), ("card", "number"), ("tax", "id"),
    ("national", "id"), ("id", "number"), ("webhook", "url"), ("raw", "payload"),
})


_assert_no_never_copy((s.name for s in CATALOG_TABLES), "CATALOG_TABLES")


def column_is_risky(column: str) -> bool:
    parts = [p for p in column.lower().split("_") if p]
    if any(p in RISKY_COLUMN_TOKENS for p in parts):
        return True
    return any(pair in RISKY_COLUMN_PAIRS for pair in zip(parts, parts[1:]))


# --- The scrub (S3, review SF3/SF4): person keys, and email/phone values, in Python ------------------
#: A JSON key (matched whole, any case, at any depth) that names a person, a secret, an identifier of
#: a request, or free text. Under it, a string becomes "redacted" (a string: the gateway serves an
#: evidence claim only with a string reviewed_by) and ANY other value, objects and arrays included,
#: becomes null (SF4: a whole reviewer_decisions array goes, the export does not refuse on it).
#: Broad on purpose: a missed person costs more than a lost tag. A JSON column whose content is product
#: data may keep a few named keys (jobs/sandbox_catalog_columns.py: "name", "handle" of a product).
_PERSON_KEY_ALTERNATION = "|".join((
    r"[a-z0-9_-]*_?by(?:_[a-z0-9_-]+)?",               # reviewed_by, createdBy, signed_by_name
    r"[a-z0-9_-]*mail[a-z0-9_-]*",                     # email, emailAddress, E-mail, mail
    r"[a-z0-9_-]*address[a-z0-9_-]*",                  # address, shipping_address, ip_address
    r"[a-z0-9_-]*phone[a-z0-9_-]*", r"mobile[a-z0-9_-]*", r"tel",
    r"pass_?words?", r"passwd", r"pwd", r"[a-z0-9_-]*secrets?", r"[a-z0-9_-]*tokens?",
    r"api_?keys?", r"access_?keys?", r"private_?keys?", r"signing_?keys?", r"secret_?keys?",
    r"credentials?", r"authorization", r"auth", r"bearer", r"jwt", r"cookies?", r"set_?cookie",
    r"sessions?", r"session_?ids?", r"request_?ids?", r"trace_?ids?", r"span_?ids?", r"correlation_?ids?",
    r"users?", r"user_?ids?", r"user_?names?", r"user_?agent",
    r"customers?", r"customer_?[a-z0-9_]+", r"buyers?", r"buyer_?[a-z0-9_]+", r"employees?",
    r"employee_?[a-z0-9_]+", r"staff", r"staff_?[a-z0-9_]+", r"reviewers?", r"reviewer_?[a-z0-9_]+",
    r"actors?", r"actor_?ids?", r"operators?", r"operator_?ids?", r"owners?", r"owner_?(?:id|email|name)",
    r"authors?", r"author_?(?:id|email|name)", r"contacts?", r"contact_?[a-z0-9_]+",
    r"names?", r"first_?name", r"last_?name", r"full_?name", r"given_?name", r"family_?name", r"handles?",
    r"street", r"postal_?code", r"zip_?code", r"ip", r"remote_?addr", r"x_?forwarded_?for",
    r"card_?numbers?", r"pan", r"cvv", r"cvc", r"iban", r"account_?numbers?", r"ssn", r"dob", r"birthday",
    r"birth_?date", r"[a-z0-9_-]*_?notes?", r"[a-z0-9_-]*_?comments?", r"remarks?", r"memo",
    r"reasons?", r"rationales?", r"raw_?payload", r"headers", r"webhook_?url", r"webhook_?secret",
))
PERSON_KEY_RE = re.compile(r"(?:%s)\Z" % _PERSON_KEY_ALTERNATION, re.IGNORECASE)
REDACTED = "redacted"
#: Email- and phone-shaped substrings, redacted in every text and JSON string value (SF3: a person's
#: address under a catalog-named key such as note2). The email check is a GATE on every column: after
#: the scrub the export carries none, or it refuses.
#: An EMAIL is local@domain.tld where the "tld" is not an image/file extension: `hero@2x.png` and
#: Shopify's `_480x480@2x.jpg` are image names (review V1), and refusing on them would stop every
#: export with a retina image URL.
_EMAIL_CANDIDATE_RE = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+)([A-Za-z]{2,63})(?![A-Za-z0-9-])")
_NOT_A_TLD = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "bmp", "tif", "tiff", "ico", "heic",
                        "mp4", "webm", "mov", "pdf", "css", "js"})
#: A PHONE needs context (review V2: SKUs, GTINs, years, sizes and prices are digit groups too):
#: an international number written with "+", or a number right after a phone word (tel/phone/call/
#: mobile/fax/whatsapp/contact and their Korean, Japanese and Chinese forms). A bare digit group is
#: never a phone here.
_PHONE_INTL_RE = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .()/-]{0,2}\d){6,14}(?!\w)")
_PHONE_WORDS = r"(?:(?<![A-Za-z])(?:tel|tele|phone|telephone|call|mobile|cell|fax|whatsapp|contact)(?![A-Za-z])|전화|연락처|휴대폰|電話|電話番号|联系电话|电话|手机)"
_PHONE_CONTEXT_RE = re.compile(
    r"(" + _PHONE_WORDS + r"[\s:：.#-]{0,4}(?:(?:no|number|num|#)\.?[\s:：#-]{0,3})?(?:us\s+)?(?:at\s+|on\s+)?)"
    r"(\+?\(?\d[\d ().-]{5,18}\d)", re.IGNORECASE)
REDACTED_TEXT = "[redacted]"


def _is_email(match: "re.Match") -> bool:
    return match.group(2).lower() not in _NOT_A_TLD


def _redact_once(text: str) -> str:
    text = _EMAIL_CANDIDATE_RE.sub(lambda m: REDACTED_TEXT if _is_email(m) else m.group(0), text)
    text = _PHONE_INTL_RE.sub(REDACTED_TEXT, text)
    return _PHONE_CONTEXT_RE.sub(lambda m: m.group(1) + REDACTED_TEXT, text)


def redact_text(text: str) -> str:
    """Email/phone-shaped parts -> [redacted]; repeated until nothing changes (review V5: a redaction
    can expose the next match, as in +395809844+0577444923)."""
    for _ in range(8):
        new = _redact_once(text)
        if new == text:
            return text
        text = new
    return text


def has_email(text: str) -> bool:
    return any(_is_email(m) for m in _EMAIL_CANDIDATE_RE.finditer(text))


def _text_hits(text: str) -> bool:
    return has_email(text) or bool(_PHONE_INTL_RE.search(text) or _PHONE_CONTEXT_RE.search(text))


class _RawNumber:
    """A JSON number kept as its own text, so a rewritten value keeps 2.50 as 2.50."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


class _DuplicateKeys(dict):
    """A JSON object whose source text repeated a key (review V4): json.loads keeps the last, so the
    cell is always rewritten from the parsed value, never copied as its original bytes."""


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            out = _DuplicateKeys(out)
        out[key] = value
    return out


def _has_duplicates(value: Any) -> bool:
    if isinstance(value, _DuplicateKeys):
        return True
    if isinstance(value, dict):
        return any(_has_duplicates(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_duplicates(v) for v in value)
    return False


def _loads(cell: str) -> Any:
    return json.loads(cell, parse_float=_RawNumber, parse_int=_RawNumber, object_pairs_hook=_pairs)


def _dumps(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join("%s: %s" % (_dumps(str(k)), _dumps(v)) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_dumps(v) for v in value) + "]"
    if isinstance(value, _RawNumber):
        return value.text
    if isinstance(value, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        return json.dumps(value, ensure_ascii=True)  # a lone surrogate stays an escape (review V5)
    return json.dumps(value, ensure_ascii=False)


#: Where a product-data column's PRODUCT_KEYS (name, handle) are kept (review V7): the top-level
#: object, the items of a top-level array, and the items under these option-like keys. Anywhere else
#: (reviews[].name, seller.handle) they are redacted like in every other column.
_PRODUCT_SCOPE_KEYS = frozenset({"options", "variants", "option_values", "values", "breadcrumb",
                                 "itemlistelement", "items"})


def _key_out(key: Any, taken: set) -> str:
    text = str(key)
    if _text_hits(text):  # review V3: an email or phone as an object KEY
        text = redact_text(text)
        base, n = text, 2
        while text in taken:
            text, n = "%s#%d" % (base, n), n + 1
    return text


def _scrub(value: Any, keep: set, scope: bool) -> Tuple[Any, bool]:
    if isinstance(value, dict):
        out, changed = {}, isinstance(value, _DuplicateKeys)
        for key, inner in value.items():
            new_key = _key_out(key, set(out))
            changed = changed or new_key != key
            if PERSON_KEY_RE.match(str(key)) and not (scope and str(key) in keep):
                new = inner if inner is None or inner == REDACTED else (REDACTED if isinstance(inner, str) else None)
                changed = changed or new is not inner
                out[new_key] = new
            else:
                new, c = _scrub(inner, keep, scope and str(key).lower() in _PRODUCT_SCOPE_KEYS)
                out[new_key] = new
                changed = changed or c
        return out, changed
    if isinstance(value, list):
        pairs = [_scrub(v, keep, scope) for v in value]
        return [p[0] for p in pairs], any(p[1] for p in pairs)
    if isinstance(value, str):
        new = redact_text(value)
        return new, new != value
    return value, False


def scrub_json(value: Any, keep_keys: Iterable[str] = ()) -> Tuple[Any, bool]:
    """(scrubbed value, changed). Person keys: a string -> "redacted", anything else -> null; every
    other string, and every key, has its email/phone-shaped parts redacted."""
    return _scrub(value, set(keep_keys), True)


def _person_values(value: Any, keep: set, scope: bool, path: str) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            here = "%s.%s" % (path, key)
            if _text_hits(str(key)):
                found.append("%s.<key>" % path)
            if PERSON_KEY_RE.match(str(key)) and not (scope and str(key) in keep):
                if inner is not None and inner != REDACTED:
                    found.append(here)
            else:
                found.extend(_person_values(inner, keep, scope and str(key).lower() in _PRODUCT_SCOPE_KEYS, here))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found.extend(_person_values(inner, keep, scope, "%s[%d]" % (path, index)))
    elif isinstance(value, str) and _text_hits(value):
        found.append(path)
    return found


def person_values(value: Any, keep_keys: Iterable[str] = (), path: str = "$") -> List[str]:
    """JSON paths where a person key still holds something other than null or "redacted", or a key or
    string still holds an email- or phone-shaped part. Paths only, never values."""
    return _person_values(value, set(keep_keys), True, path)


def _csv_field(value: Optional[str]) -> str:
    return "" if value is None else '"%s"' % value.replace('"', '""')


class _TableScrub:
    """One table's scrub (S3), applied to a block of whole CSV records at a time, so neither the export
    nor the import ever holds a table in memory (S5). scrub_csv and assert_scrubbed are the whole-file
    forms of the same two passes."""

    def __init__(self, table: str, columns: Sequence[str]):
        from jobs.sandbox_catalog_columns import column_class, json_keep_keys

        csv.field_size_limit(sys.maxsize)  # a product payload can exceed csv's 128 KiB default
        self.table = table
        self.columns = list(columns)
        self.classes = [column_class(table, c) for c in self.columns]
        self.keeps = [json_keep_keys(table, c) for c in self.columns]

    def _rows(self, text: str):
        for row in csv.reader(io.StringIO(text, newline="")):
            if len(row) != len(self.columns):
                raise CopyRefused("table %s: a CSV record has %d fields, expected %d"
                                  % (self.table, len(row), len(self.columns)))
            yield row

    def scrub(self, text: str, null_token: str) -> Tuple[str, int]:
        """Re-write ``COPY ... (FORMAT csv, NULL null_token)`` records with every JSON and text column
        scrubbed: standard CSV (NULL = an empty unquoted field, every other value quoted), which
        ``COPY FROM (FORMAT csv)`` reads back exactly. Returns the text and its record count."""
        from jobs.sandbox_catalog_columns import JSON as _JSON, TEXT as _TEXT

        out: List[str] = []
        for row in self._rows(text):
            cells: List[Optional[str]] = [None if v == null_token else v for v in row]
            for i, cell in enumerate(cells):
                if cell is None:
                    continue
                if self.classes[i] == _JSON:
                    try:
                        value = _loads(cell)
                    except ValueError:
                        raise CopyRefused("table %s column %s holds a value that is not JSON"
                                          % (self.table, self.columns[i])) from None
                    new, changed = scrub_json(value, self.keeps[i])
                    if changed:  # a repeated key counts as a change (_scrub), so it is always rewritten
                        cells[i] = _dumps(new)
                elif self.classes[i] == _TEXT:
                    cells[i] = redact_text(cell)
            out.append(",".join(_csv_field(c) for c in cells) + "\n")
        return "".join(out), len(out)

    def check(self, text: str) -> int:
        """Refuse when the records still name a person: a person key holding a value or an
        email/phone-shaped string in a JSON column, an email/phone in a text column, or an email in ANY
        other column. A REPLACED column holds the spec's own placeholder and is skipped. Names the
        table, the column and the JSON path, never the value. Returns the record count."""
        from jobs.sandbox_catalog_columns import JSON as _JSON, REPLACED as _REPLACED, TEXT as _TEXT

        records = 0
        for row in self._rows(text):
            records += 1
            for i, cell in enumerate(row):
                if cell == "" or self.classes[i] == _REPLACED:
                    continue
                if self.classes[i] == _JSON:
                    try:
                        value = _loads(cell)
                    except ValueError:
                        raise CopyRefused("table %s column %s holds a value that is not JSON"
                                          % (self.table, self.columns[i])) from None
                    if _has_duplicates(value):
                        raise CopyRefused("table %s column %s holds a JSON object with a repeated key"
                                          % (self.table, self.columns[i]))
                    hits = person_values(value, self.keeps[i])
                    if hits:
                        raise CopyRefused("table %s column %s: a person key or contact detail survived the "
                                          "scrub at %s" % (self.table, self.columns[i], hits[0]))
                elif self.classes[i] == _TEXT and _text_hits(cell):
                    raise CopyRefused("table %s column %s: an email- or phone-shaped value survived the scrub"
                                      % (self.table, self.columns[i]))
                elif has_email(cell):
                    raise CopyRefused("table %s column %s holds an email-shaped value"
                                      % (self.table, self.columns[i]))
        return records


def scrub_csv(table: str, raw: bytes, null_token: str, columns: Sequence[str]) -> bytes:
    """The whole-file form of _TableScrub.scrub."""
    return _TableScrub(table, columns).scrub(raw.decode("utf-8"), null_token)[0].encode("utf-8")


def assert_scrubbed(table: str, raw: bytes, columns: Sequence[str]) -> int:
    """The whole-file form of _TableScrub.check. Returns the email-shaped values found (0, or it
    raised): the manifest's email_like_values."""
    _TableScrub(table, columns).check(raw.decode("utf-8"))
    return 0


class RecordSplitter:
    """Cuts a CSV byte stream (PostgreSQL's COPY csv output) into blocks of whole records (S5).

    A record ends at a newline outside quotes. Inside a quoted field PostgreSQL writes a quote as two
    quotes, so the parity of the quotes seen so far says whether a newline is inside a field. Newline
    and quote are ASCII and never occur inside a UTF-8 multi-byte character, so a cut never splits one.
    Holds at most one unfinished record, and refuses one longer than ``max_record`` bytes."""

    def __init__(self, table: str, max_record: Optional[int] = None):
        self.table = table
        self.max_record = MAX_RECORD_BYTES if max_record is None else max_record
        self._tail = bytearray()
        self._odd = False  # the quote parity of _tail

    def _bounded(self) -> None:
        if len(self._tail) > self.max_record:
            raise CopyRefused("table %s: a CSV record is longer than %d bytes" % (self.table, self.max_record))

    def feed(self, data: bytes) -> bytes:
        """Every whole record completed by ``data`` (b"" while none is), in order."""
        odd, start, cut = self._odd, 0, -1
        while True:
            nl = data.find(b"\n", start)
            if nl < 0:
                break
            if data.count(b'"', start, nl) & 1:
                odd = not odd
            if not odd:
                cut = nl
            start = nl + 1
        if data.count(b'"', start) & 1:
            odd = not odd
        self._odd = odd
        if cut < 0:
            self._tail += data
            self._bounded()
            return b""
        block = bytes(self._tail) + data[: cut + 1] if self._tail else bytes(data[: cut + 1])
        self._tail = bytearray(data[cut + 1:])
        self._bounded()
        return block

    def finish(self) -> None:
        """COPY ends every record with a newline: anything left over is a cut-off stream."""
        if self._tail:
            raise CopyRefused("table %s: the CSV stream ends inside a record" % self.table)


def _inflated(chunks: Iterable[bytes], table: str) -> Iterable[bytes]:
    """gunzip a stream of gzip chunks, at most INFLATE_STEP_BYTES out per step, so a small file that
    inflates hugely is still processed in bounded pieces. One gzip member, nothing after it."""
    d = zlib.decompressobj(31)
    for chunk in chunks:
        data = chunk
        while data and not d.eof:
            try:
                out = d.decompress(data, INFLATE_STEP_BYTES)
            except zlib.error:
                raise CopyRefused("table %s: the file is not valid gzip" % table) from None
            if out:
                yield out
            # Output zlib still holds when this chunk is used up comes out with the next chunk's
            # first call; the stream always ends with its 8-byte trailer, so none is left behind.
            data = d.unconsumed_tail
        if d.eof and (d.unused_data or data):
            raise CopyRefused("table %s: the file carries data after its gzip stream" % table)
    if not d.eof:
        raise CopyRefused("table %s: the file's gzip stream is cut off" % table)


def json_columns_of(spec: TableSpec, columns: Sequence[str]) -> List[str]:
    from jobs.sandbox_catalog_columns import JSON as _JSON, column_class

    return [c for c in columns if column_class(spec.name, c) == _JSON]


def plan_columns(spec: TableSpec, existing: Sequence[str],
                 types: Optional[Mapping[str, str]] = None) -> List[Tuple[str, str]]:
    """``[(column, select_expression)]`` to copy from a table whose columns are ``existing``.

    Raises CopyRefused when a remaining column is not classified in jobs/sandbox_catalog_columns.py
    (fail closed, S3), when a JSON-typed column is not classified JSON (so it would skip the scrub) or
    the other way round, or when a column looks risky by name and is neither dropped, replaced nor
    allowed in the spec. ``types`` maps column -> PostgreSQL type name (json, jsonb, text, ...); the
    export always passes it. The order follows ``existing`` (the table's own).
    """
    from jobs.sandbox_catalog_columns import JSON as _JSON, column_class

    types = dict(types or {})
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
        cls = column_class(spec.name, column)
        if cls is None:
            raise CopyRefused(
                "table %s column %s is not classified in jobs/sandbox_catalog_columns.py; classify "
                "or drop it before copying" % (spec.name, column)
            )
        data_type = types.get(column)
        is_json_type = data_type in ("json", "jsonb")
        if data_type is not None and data_type.startswith("unsupported:") and cls == _JSON:
            raise CopyRefused("table %s column %s is a JSON container type (%s); review it before copying"
                              % (spec.name, column, data_type[len("unsupported:"):]))
        if cls == _JSON or is_json_type:
            if cls != _JSON:
                raise CopyRefused("table %s column %s is %s but not classified JSON (it would skip the "
                                  "scrub)" % (spec.name, column, data_type))
            if data_type is None:
                raise CopyRefused("table %s column %s is classified JSON but its type is unknown"
                                  % (spec.name, column))
            if data_type not in ("json", "jsonb"):
                raise CopyRefused("table %s column %s is classified JSON but its type is %s"
                                  % (spec.name, column, data_type))
            out.append((column, _q(column)))  # scrubbed in Python before upload (scrub_csv)
            continue
        if data_type is not None and data_type.startswith("unsupported:"):
            raise CopyRefused("table %s column %s is a JSON container type (%s); review it before copying"
                              % (spec.name, column, data_type[len("unsupported:"):]))
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


class _LocalWriter:
    """A file written under ``<root>/.uploading/`` and linked into place on commit. Like a GCS resumable
    upload: nothing exists under its name until commit, and commit never overwrites."""

    def __init__(self, store: "LocalDirStore", name: str):
        import tempfile

        self.final = store.root / name
        parts = store.root / LocalDirStore.UPLOADING
        parts.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(dir=parts)
        self.part = Path(part)
        self.handle = os.fdopen(fd, "wb")
        self.name = name

    def write(self, data: bytes) -> None:
        self.handle.write(data)

    def flush(self) -> None:
        self.handle.flush()

    def commit(self) -> None:
        self.handle.close()
        self.final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(self.part, self.final)  # fails if the name exists, as ifGenerationMatch=0 on GCS
        except FileExistsError:
            raise CopyRefused("object already exists; an export never overwrites (%s)" % self.name) from None
        finally:
            self.part.unlink(missing_ok=True)

    def abort(self) -> None:
        if not self.handle.closed:
            self.handle.close()
        self.part.unlink(missing_ok=True)


class LocalDirStore:
    """A directory standing in for a bucket prefix. Same no-overwrite rule as the GCS writer."""

    UPLOADING = ".uploading"

    def __init__(self, root: Path):
        self.root = Path(root)

    def open_writer(self, name: str) -> _LocalWriter:
        return _LocalWriter(self, name)

    def put(self, name: str, data: bytes) -> None:
        _put(self, name, data)

    def get(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def generation(self, name: str) -> Optional[str]:
        """A directory has no generations; the import's second sha256 check covers a changed file."""
        return None

    def open_reader(self, name: str, generation: Optional[str]) -> Iterable[bytes]:
        with open(self.root / name, "rb") as handle:
            while True:
                chunk = handle.read(READ_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk

    def list(self, prefix: str = "") -> List[str]:
        base = self.root
        if not base.exists():
            return []
        names = [str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()]
        return sorted(n for n in names if n.startswith(prefix) and not n.startswith(self.UPLOADING + "/"))


def _put(store, name: str, data: bytes) -> None:
    writer = store.open_writer(name)
    try:
        writer.write(data)
        writer.commit()
    except BaseException:
        writer.abort()
        raise


class _GcsUpload:  # pragma: no cover - exercised only on Cloud Run (unit-tested with a fake httpx)
    """A GCS resumable upload created with ifGenerationMatch=0. The object exists only once commit()
    sends the last piece, so an export that refuses part-way creates nothing for that file. Holds at
    most UPLOAD_CHUNK_BYTES; flush() sends every whole 256 KiB piece and keeps less than one.
    The session URL is a capability: it is never logged or put in a refusal."""

    QUANTUM = 256 << 10

    def __init__(self, store: "GcsStore", name: str):
        self.store, self.name = store, name
        self.buffer = bytearray()
        self.offset = 0
        response = store._call(
            "post", "create %s" % name,
            "https://storage.googleapis.com/upload/storage/v1/b/%s/o" % store.bucket,
            params={"uploadType": "resumable", "name": store._name(name), "ifGenerationMatch": "0"},
            content=b"",
            headers={"Authorization": "Bearer " + store._token(),
                     "X-Upload-Content-Type": "application/octet-stream"},
            timeout=60,
        )
        if response.status_code == 412:
            raise CopyRefused("object already exists; an export never overwrites (%s)" % name)
        store._ok(response, "create %s" % name)
        self.session = response.headers.get("location") or ""
        if not self.session.startswith("https://storage.googleapis.com/"):
            raise CopyRefused("GCS gave no upload session for %s" % name)

    def _send(self, piece: bytes, total: Optional[int]) -> None:
        what = "upload %s" % self.name
        if piece:
            span = "bytes %d-%d/%s" % (self.offset, self.offset + len(piece) - 1, "*" if total is None else total)
        else:
            span = "bytes */%d" % total
        response = self.store._call("put", what, self.session, content=piece,
                                    headers={"Content-Range": span}, timeout=300)
        if total is None:
            # GCS answers 308 with the persisted range; anything short of all we sent is a refusal.
            expected = "bytes=0-%d" % (self.offset + len(piece) - 1)
            if response.status_code != 308 or response.headers.get("range") != expected:
                raise CopyRefused("GCS answered HTTP %d for %s" % (response.status_code, what))
        else:
            if response.status_code == 412:
                raise CopyRefused("object already exists; an export never overwrites (%s)" % self.name)
            if response.status_code not in (200, 201):
                raise CopyRefused("GCS answered HTTP %d for %s" % (response.status_code, what))
        self.offset += len(piece)

    def _send_whole_pieces(self, keep_below: int) -> None:
        while len(self.buffer) >= keep_below:
            size = (len(self.buffer) // self.QUANTUM) * self.QUANTUM
            size = min(size, UPLOAD_CHUNK_BYTES)
            if size == 0:
                return
            self._send(bytes(self.buffer[:size]), None)
            del self.buffer[:size]

    def write(self, data: bytes) -> None:
        self.buffer += data
        self._send_whole_pieces(UPLOAD_CHUNK_BYTES)

    def flush(self) -> None:
        self._send_whole_pieces(self.QUANTUM)

    def commit(self) -> None:
        self._send(bytes(self.buffer), self.offset + len(self.buffer))
        self.buffer = bytearray()

    def abort(self) -> None:
        import httpx

        try:
            httpx.delete(self.session, timeout=10)  # GCS answers 499: the session is cancelled
        except httpx.HTTPError:
            pass  # an unfinished session creates nothing and expires on its own


class GcsStore:  # pragma: no cover - exercised only on Cloud Run
    """The GCS JSON API with the Cloud Run service account's token (metadata server).

    No client library: google-cloud-storage is not a dependency of this service. Writes use
    ifGenerationMatch=0 (create only), which is also all the exporter's IAM role allows.
    """

    _TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(self, uri: str):
        if not uri.startswith("gs://"):
            raise CopyRefused("CATALOG_COPY_GCS_URI must start with gs://")
        from config.sandbox_identity import SANDBOX_DROP_BUCKET

        bucket, _, prefix = uri[5:].partition("/")
        if bucket != SANDBOX_DROP_BUCKET:
            raise CopyRefused("the GCS URI must name the sandbox drop bucket %s" % SANDBOX_DROP_BUCKET)
        if ".." in prefix.split("/"):
            raise CopyRefused("the GCS URI prefix may not contain '..'")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _name(self, name: str) -> str:
        return "%s/%s" % (self.prefix, name) if self.prefix else name

    @staticmethod
    def _call(method, what: str, *args, **kwargs):
        """httpx.<method>(...), with every transport failure a refusal naming the operation only."""
        import httpx

        try:
            return getattr(httpx, method)(*args, **kwargs)
        except httpx.HTTPError as exc:
            raise CopyRefused("GCS unreachable for %s (%s)" % (what, type(exc).__name__)) from None

    @staticmethod
    def _ok(response, what: str) -> None:
        """A refusal naming the operation and the HTTP status, never a traceback or a body."""
        if response.status_code != 200:
            raise CopyRefused("GCS answered HTTP %d for %s" % (response.status_code, what))

    def _token(self) -> str:
        response = self._call("get", "the metadata token", self._TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=10)
        self._ok(response, "the metadata token")
        return response.json()["access_token"]

    def open_writer(self, name: str) -> _GcsUpload:
        return _GcsUpload(self, name)

    def put(self, name: str, data: bytes) -> None:
        _put(self, name, data)

    def _object_url(self, name: str) -> str:
        import urllib.parse

        return "https://storage.googleapis.com/storage/v1/b/%s/o/%s" % (
            self.bucket, urllib.parse.quote(self._name(name), safe=""))

    def get(self, name: str) -> bytes:
        response = self._call(
            "get", "read %s" % name, self._object_url(name),
            params={"alt": "media"},
            headers={"Authorization": "Bearer " + self._token()},
            timeout=300,
        )
        self._ok(response, "read %s" % name)
        return response.content

    def generation(self, name: str) -> str:
        """The object's generation, so the import's load reads the very bytes its check read."""
        response = self._call(
            "get", "stat %s" % name, self._object_url(name),
            params={"fields": "generation"},
            headers={"Authorization": "Bearer " + self._token()},
            timeout=60,
        )
        self._ok(response, "stat %s" % name)
        return str(response.json()["generation"])

    def open_reader(self, name: str, generation: Optional[str]) -> Iterable[bytes]:
        import httpx

        params = {"alt": "media"}
        if generation is not None:
            params["generation"] = generation
        try:
            with httpx.stream("GET", self._object_url(name), params=params,
                              headers={"Authorization": "Bearer " + self._token()}, timeout=300) as response:
                self._ok(response, "read %s" % name)
                for chunk in response.iter_bytes(READ_CHUNK_BYTES):
                    yield chunk
        except httpx.HTTPError as exc:
            raise CopyRefused("GCS unreachable for read %s (%s)" % (name, type(exc).__name__)) from None

    def list(self, prefix: str = "") -> List[str]:
        names: List[str] = []
        token = None
        full = self._name(prefix)
        while True:
            params = {"prefix": full, "fields": "items(name),nextPageToken"}
            if token:
                params["pageToken"] = token
            response = self._call(
                "get", "list",
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


async def _table_column_types(conn, schema: str, table: str) -> Dict[str, str]:
    """column -> its type name as PostgreSQL spells it (json, jsonb, text, integer, ...). An array of
    JSON, or a domain over JSON (or over a JSON array), is ``unsupported:<type>``: plan_columns refuses
    it rather than copy it past the scrub (review nit)."""
    rows = await conn.fetch(
        """
        SELECT a.attname AS column_name, t.typname AS type_name, t.typtype AS type_kind,
               bt.typname AS base_name, et.typname AS elem_name, bet.typname AS base_elem_name
          FROM pg_catalog.pg_attribute a
          JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
          LEFT JOIN pg_catalog.pg_type bt ON bt.oid = t.typbasetype AND t.typtype = 'd'
          LEFT JOIN pg_catalog.pg_type et ON et.oid = t.typelem AND t.typlen = -1
          LEFT JOIN pg_catalog.pg_type bet ON bet.oid = bt.typelem AND bt.typlen = -1
         WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped
        """,
        schema, table,
    )
    out: Dict[str, str] = {}
    for r in rows:
        name = r["type_name"]
        around = {r["base_name"], r["elem_name"], r["base_elem_name"]} & {"json", "jsonb"}
        out[r["column_name"]] = "unsupported:%s" % name if around else name
    return out


# ---------------------------------------------------------------------------------------------
# Export (prod)
# ---------------------------------------------------------------------------------------------


def utc_stamp(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%MZ")


class _ExportSink:
    """One table's COPY output, streamed (S5): whole records -> the scrub -> its own check -> gzip ->
    the store's writer. Holds one unfinished record, the gzip window and one upload chunk at most.

    It never raises inside the COPY. asyncpg 0.31 leaves the connection's reading paused when a sink
    raises while the server is still sending, and the transaction's ROLLBACK then waits forever. So a
    refusal is kept, the rest of the table's output is dropped unread, and the export raises it as
    soon as the COPY returns. statement_timeout and the deadline bound that COPY."""

    def __init__(self, table: str, columns: Sequence[str], null_token: str, writer):
        self.scrub = _TableScrub(table, columns)
        self.split = RecordSplitter(table)
        self.null_token = null_token
        self.zip = zlib.compressobj(6, zlib.DEFLATED, 31)  # gzip framing, mtime 0
        self.writer = writer
        self.sha = hashlib.sha256()
        self.size = 0
        self.records = 0
        self.error: Optional[Exception] = None

    async def __call__(self, chunk: bytes) -> None:
        if self.error is not None:
            return
        try:
            block = self.split.feed(bytes(chunk))
            if block:
                text, records = self.scrub.scrub(block.decode("utf-8"), self.null_token)
                # The export checks its own output before any of it leaves (S3), block by block.
                self.scrub.check(text)
                self.records += records
                await self._out(self.zip.compress(text.encode("utf-8")))
        except Exception as exc:  # noqa: BLE001 - kept and raised after the COPY (see the class)
            self.error = exc

    async def _out(self, data: bytes) -> None:
        if data:
            self.sha.update(data)
            self.size += len(data)
            await asyncio.to_thread(self.writer.write, data)

    async def finish(self) -> None:
        if self.error is not None:
            raise self.error
        self.split.finish()
        await self._out(self.zip.flush())
        await asyncio.to_thread(self.writer.flush)


#: The server-side limits, by the SQLSTATE PostgreSQL raises when one ends a statement or a session.
_LIMIT_SQLSTATES = {"57014": "statement_timeout", "55P03": "lock_timeout",
                    "25P03": "idle_in_transaction_session_timeout"}
_PG_INTERVAL_RE = re.compile(r"^[1-9][0-9]{0,6}(ms|s|min)$")


async def export_catalog(conn, store, *, stamp: str, commit: Optional[str] = None,
                         specs: Sequence[TableSpec] = CATALOG_TABLES, schema: str = "public",
                         statement_timeout: str = EXPORT_STATEMENT_TIMEOUT,
                         lock_timeout: str = EXPORT_LOCK_TIMEOUT,
                         idle_timeout: str = EXPORT_IDLE_IN_TRANSACTION_TIMEOUT,
                         deadline_seconds: float = EXPORT_DEADLINE_SECONDS) -> Dict[str, Any]:
    """Prod's load (S5): one READ ONLY snapshot, each table streamed (memory stays flat whatever the
    table's size), every statement under statement_timeout and lock_timeout, idle_in_transaction
    capped, and the whole export under a client-side deadline that terminates the connection.
    Every file is an unfinished upload until the last table has passed; only then are the files
    committed, and the manifest last."""
    if not _STAMP_RE.match(stamp):
        raise CopyRefused("stamp must look like YYYYmmddTHHMMZ")
    for setting, value in (("statement_timeout", statement_timeout), ("lock_timeout", lock_timeout),
                           ("idle_in_transaction_session_timeout", idle_timeout)):
        if not _PG_INTERVAL_RE.match(value):
            raise CopyRefused("%s must look like 30s, 500ms or 20min" % setting)
    if not 0 < deadline_seconds <= MAX_EXPORT_DEADLINE_SECONDS:
        raise CopyRefused("the export deadline must be between 0 and %d seconds" % MAX_EXPORT_DEADLINE_SECONDS)
    _assert_no_never_copy((s.name for s in specs), "the export's table list")
    writers: List[Any] = []
    # The deadline TERMINATES the connection: the server ends the session, its transaction and its
    # snapshot at once, whatever the client is waiting on (a cancelled COPY can stall asyncpg; see
    # _ExportSink). The caller's connection is unusable afterwards; the job exits on the refusal.
    expired: List[bool] = []

    def _expire() -> None:
        expired.append(True)
        conn.terminate()

    timer = asyncio.get_running_loop().call_later(deadline_seconds, _expire)
    try:
        try:
            tables, absent = await _export_tables(
                conn, store, stamp, specs, schema, writers,
                (("statement_timeout", statement_timeout), ("lock_timeout", lock_timeout),
                 ("idle_in_transaction_session_timeout", idle_timeout)))
        except Exception as exc:
            if expired:
                raise CopyRefused("the export passed its %gs deadline; nothing was written" % deadline_seconds) from None
            limit = _LIMIT_SQLSTATES.get(getattr(exc, "sqlstate", None) or "")
            if limit:
                raise CopyRefused("the export hit its %s on the source; nothing was written" % limit) from None
            raise
        finally:
            timer.cancel()
        if expired:
            raise CopyRefused("the export passed its %gs deadline; nothing was written" % deadline_seconds)
        while writers:
            writers[0].commit()
            writers.pop(0)
    finally:
        for writer in writers:
            writer.abort()
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


async def _export_tables(conn, store, stamp: str, specs: Sequence[TableSpec], schema: str,
                         writers: List[Any], limits) -> Tuple[List[Dict[str, Any]], List[str]]:
    tables: List[Dict[str, Any]] = []
    absent: List[str] = []
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        version = getattr(conn, "get_server_version", None)
        extra: Tuple[Tuple[str, str], ...] = (("application_name", "sandbox_catalog_export"),)
        if version is not None and version().major >= 14:
            extra += (("client_connection_check_interval", EXPORT_CLIENT_CHECK_INTERVAL),)
        for setting, value in limits + extra:
            # set_config(..., true) is SET LOCAL: it ends with this transaction.
            await conn.execute("SELECT pg_catalog.set_config($1, $2, true)", setting, value)
        for spec in specs:
            existing = await _table_columns(conn, schema, spec.name)
            if not existing:
                if spec.optional:
                    absent.append(spec.name)
                    continue
                raise CopyRefused("table %s does not exist in the source" % spec.name)
            plan = plan_columns(spec, existing, await _table_column_types(conn, schema, spec.name))
            where = " WHERE %s" % spec.where if spec.where else ""
            query = "SELECT %s FROM %s.%s%s" % (", ".join(e for _, e in plan), _q(schema), _q(spec.name), where)
            planned = [c for c, _ in plan]
            fname = "%s/%s.csv.gz" % (stamp, spec.name)
            writer = await asyncio.to_thread(store.open_writer, fname)
            writers.append(writer)
            # The scrub (S3) runs in the sink, on every JSON and text column, before anything is sent.
            sink = _ExportSink(spec.name, planned, "__sandbox_copy_null_%s__" % secrets.token_hex(16), writer)
            await conn.copy_from_query(query, output=sink, format="csv", null=sink.null_token)
            await sink.finish()
            rows = await conn.fetchval("SELECT count(*) FROM %s.%s%s" % (_q(schema), _q(spec.name), where))
            if sink.records != rows:
                raise CopyRefused("table %s: CSV record count differs from the snapshot's" % spec.name)
            tables.append({
                "name": spec.name,
                "columns": planned,
                "json_columns": json_columns_of(spec, planned),
                "rows": int(rows),
                "file": fname,
                "bytes": sink.size,
                "sha256": sink.sha.hexdigest(),
                # A gate now (SF3), not a review signal: after the scrub the copy holds no
                # email-shaped value anywhere, and the importer refuses a manifest saying otherwise.
                "email_like_values": 0,
            })
    return tables, absent


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
    _assert_no_never_copy((s.name for s in specs), "the import's table list")
    _assert_no_never_copy((str(e.get("name") or "") for e in manifest.get("tables") or []), "the manifest")
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
        from jobs.sandbox_catalog_columns import column_class

        if entry.get("email_like_values") != 0:
            raise CopyRefused("manifest table %s reports email-shaped values; the copy carries none" % name)
        if list(entry.get("json_columns") or []) != json_columns_of(spec, columns):
            raise CopyRefused("manifest table %s: its JSON columns are not the classified ones" % name)
        for column in columns:
            if column in spec.drop:
                raise CopyRefused("manifest table %s carries dropped column %s" % (name, column))
            if spec.keep is not None and column not in spec.keep:
                raise CopyRefused("manifest table %s carries column %s outside its keep list" % (name, column))
            if column_is_risky(column) and column not in spec.allow_risky and column not in spec.replace:
                raise CopyRefused("manifest table %s carries risky column %s" % (name, column))
            if column_class(name, column) is None and column not in spec.replace:
                raise CopyRefused("manifest table %s carries unclassified column %s" % (name, column))
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


def _check_file(store, entry: Mapping[str, Any], generation: Optional[str]) -> None:
    """Pass 1 of the import, before the database is touched: the file's sha256, its gzip framing, its
    record count and the scrub, all in one streamed read. A sha256 mismatch is reported first."""
    name = entry["name"]
    sha = hashlib.sha256()
    problem: Optional[CopyRefused] = None
    records = 0

    read = {"whole": False}

    def hashed():
        for chunk in store.open_reader(entry["file"], generation):
            sha.update(chunk)
            yield chunk
        read["whole"] = True

    split = RecordSplitter(name)
    scrub = _TableScrub(name, entry["columns"])
    chunks = hashed()
    try:
        for block in _inflated(chunks, name):
            whole = split.feed(block)
            if whole:
                records += scrub.check(whole.decode("utf-8"))
        split.finish()
    except (CopyRefused, UnicodeDecodeError) as exc:
        problem = exc if isinstance(exc, CopyRefused) else CopyRefused("table %s: the file is not UTF-8" % name)
        for _ in chunks:  # finish the read so the sha256 is the whole file's (a read error raises here)
            pass
        if not read["whole"]:  # the read itself failed: that is the refusal
            raise problem
    if sha.hexdigest() != entry.get("sha256"):
        raise CopyRefused("table %s: file sha256 does not match the manifest" % name)
    if problem is not None:
        raise problem
    if records != entry.get("rows"):
        raise CopyRefused("table %s: file row count does not match the manifest" % name)


async def _load_stream(store, entry: Mapping[str, Any], generation: Optional[str]):
    """Pass 2, inside the import's transaction: the same object generation, inflated straight into
    COPY. Its sha256 is checked again at the end; a mismatch raises inside the COPY, so the
    transaction rolls back."""
    sha = hashlib.sha256()

    def hashed():
        for chunk in store.open_reader(entry["file"], generation):
            sha.update(chunk)
            yield chunk

    inflated = iter(_inflated(hashed(), entry["name"]))
    done = object()
    while True:
        block = await asyncio.to_thread(next, inflated, done)
        if block is done:
            break
        yield block
    if sha.hexdigest() != entry.get("sha256"):
        raise CopyRefused("table %s: the file changed between the check and the load" % entry["name"])


async def import_catalog(conn, store, *, stamp: Optional[str] = None,
                         specs: Sequence[TableSpec] = CATALOG_TABLES, schema: str = "public") -> Dict[str, Any]:
    stamp = stamp or newest_complete_stamp(store)
    if not stamp:
        raise CopyRefused("no complete export (no stamp with a manifest) in the store")
    manifest = json.loads(store.get("%s/%s" % (stamp, MANIFEST_NAME)).decode("utf-8"))
    if manifest.get("stamp") != stamp:
        raise CopyRefused("manifest stamp does not match its folder")
    validate_manifest(manifest, specs)

    # Check every file before touching the database, one streamed read each (S5): sha256, gzip, row
    # count and the scrub (the importer does not trust the export's). The load then reads the same
    # object generation again and re-checks its sha256, so nothing is held in memory between the two.
    generations: Dict[str, Optional[str]] = {}
    entries = {e["name"]: e for e in manifest["tables"]}
    for name, entry in entries.items():
        if entry.get("file") != "%s/%s.csv.gz" % (stamp, name):
            raise CopyRefused("manifest table %s points outside its stamp" % name)
        generations[name] = await asyncio.to_thread(store.generation, entry["file"])
        await asyncio.to_thread(_check_file, store, entry, generations[name])

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
            await conn.copy_to_table(stage, source=_load_stream(store, entries[name], generations[name]),
                                     columns=columns, format="csv")
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
