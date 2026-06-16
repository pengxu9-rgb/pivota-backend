# ADR-006: Pivota-Owned GSC Indexing for the Per-SKU `request_indexing` Gap

**Status:** Proposed · **Date:** 2026-06-16 · **Scope:** `pivota-backend` GSC integration + `pivota-merchants-portal` per-SKU "What to do next"

---

## Context

The per-SKU audit emits a `get_indexed` prescription (`PRIMARY_SKU_GET_INDEXED`,
`services/next_best_action.py:40`) when a SKU isn't live in the AI shopping surface yet.
It maps to `cta.action = "request_indexing"` (`_SKU_CTA_ACTION`, line 61) and stamps
`cta.target_sku_key` (line 254).

The portal **previously rendered the wrong UI** for this gap: `request_indexing` shared the
same gate as `request_enrichment` and showed the INCI/brand-URL **evidence form**
(`AddToPivotaPageForm` → `POST /merchant/pdps/.../evidence`), which grades ingredient claims and
does **nothing** for indexing. That over-promise has been removed in the portal —
`PerSkuNextStep.tsx` now gates the evidence form to `request_enrichment` only, and
`request_indexing` renders the backend's deterministic publish/sitemap/crawlable checklist
(`self_serve_actions` from the `get_indexed` branch, `next_best_action.py:465`) plus the
conditional "Once it's live and crawlable, add its details on your Pivota page" note.

That fix is **honest but inert** — it tells the merchant what to do; Pivota does nothing.
This ADR records the design for the *active* path: **Pivota submits the canonical Pivota PDP
URL to Google's Indexing API to accelerate discovery of the Pivota-hosted product surface.**

There **is** GSC machinery today (`services/gsc_integration.py`, `routes/gsc_oauth_routes.py`,
`services/executor_agents/gsc_url_submission.py`), flag-gated off
(`settings.gsc_integration_enabled`, default `false`, `config/settings.py:386`). **It is built
for the wrong principal** — see Decision.

---

## What we can and cannot do (the constraint that drives everything)

Google's Indexing API only accepts a URL submission from a **verified owner of that URL's
property** in Search Console.

- **We cannot submit the merchant's own store URL.** We don't own or verify the merchant's
  domain. Telling a merchant we'll "get *your product page* indexed" is an over-promise we
  cannot keep, and is the exact framing we just removed from the portal.
- **We can submit Pivota's canonical PDP URL** (`agent.pivota.cc/products/sig_*`). Pivota owns
  and verifies `agent.pivota.cc`, so Pivota — and only Pivota — can submit those URLs.

So the only feasible active path is: **Pivota submits its own canonical product URLs under a
Pivota-owned credential.** This is *simpler* operationally than the existing design (no
per-merchant connect flow) but is **not** a wire-up of the current code.

---

## The existing machinery is built for the wrong principal

The shipped GSC service authenticates every submission as the **merchant's** OAuth token:

- `submit_url_to_gsc(merchant_id, url, …)` → `access_token = _get_valid_access_token(merchant_id)`
  (`gsc_integration.py:163`).
- `submit_audit_canonical_urls` selects Pivota canonical URLs (`url_source ==
  'pivota_canonical_pdp'`, line 488) **but gates on `is_gsc_integrated(merchant_id)`** (line 506)
  and submits under the merchant's `authorized_site_url` (`_get_authorized_site_url`, line 383).
- The entire OAuth flow (`routes/gsc_oauth_routes.py`) and token store (`db.gsc_tokens`) are
  keyed on `merchant_id`.

This is **internally inconsistent for the canonical-URL purpose**: it submits an
`agent.pivota.cc/...` URL authenticated as the *merchant*, who is not a verified owner of
`agent.pivota.cc`. Google rejects this (HTTP 403, "user is not an owner of the property"). The
per-merchant model only makes sense for submitting the *merchant's own* URLs under *their*
property — the thing we established we cannot/should not do.

There is **no Pivota-owned service-account or property concept** anywhere in the code today
(`grep -rni "service_account\|pivota_site\|owned_site" services/gsc_integration.py
config/settings.py` → empty).

---

## Decision

Build a **Pivota-owned-credential** submission path, decoupled from merchant OAuth:

1. **One Pivota-owned credential** — a service account (or a single Pivota Google identity) that
   is the **verified owner of `agent.pivota.cc`** in Search Console. New settings:
   `gsc_pivota_service_account_json` / `gsc_pivota_property_url` (or equivalent). No per-merchant
   tokens involved.
2. **Submission keyed on the canonical URL, not the merchant identity.** Reuse the Indexing API
   call shape from `submit_url_to_gsc` (line 151) and the `gsc_url_submissions` upsert
   (`_upsert_url_submission`, line 389), but swap `_get_valid_access_token(merchant_id)` for the
   Pivota-credential token. `merchant_id` stays on the row for reporting attribution, not for auth.
3. **Trigger:** submit a SKU's canonical PDP URL when the per-SKU prescription is `get_indexed`
   AND the SKU has a `pivota_canonical_pdp` URL. The portal CTA (`request_indexing`, carrying the
   now-consumed `cta.target_sku_key`) requests it; the backend resolves the SKU's canonical URL
   and submits. No merchant connect step.
4. **Status read-back** via `get_index_status` (line 225) / `gsc_url_submissions`, surfaced to the
   portal so the merchant sees `submitted → pending → indexed` for the **Pivota page**.

The existing per-merchant OAuth flow is **not deleted** — it remains the (separate, still-off)
mechanism should we ever offer "submit your *own* store URLs via *your* connected GSC." It is
simply not the mechanism for the canonical-URL path.

---

## Copy scope (non-negotiable — this is how we avoid re-introducing the over-promise)

Submitting `agent.pivota.cc/...` to the Indexing API tells Google "crawl this **Pivota page**."
It accelerates discovery of the **Pivota canonical PDP** (the surface AI shoppers read). It does
**not** index the merchant's own store page.

- The per-SKU CTA must read **"get this product's Pivota page indexed"**, never "get your product
  indexed."
- Status copy must attribute progress to the **Pivota page**, not the merchant's site.
- The self-serve checklist (publish / sitemap / crawlable, already shipped) remains the answer for
  the merchant's *own* page — the two are complementary and must stay visually distinct, as they
  are today in `PerSkuNextStep.tsx` ("Add it to your Pivota page" vs. "Do this yourself").

---

## Validation step (gate before we tell merchants it does anything)

Google's Indexing API is **officially scoped to `JobPosting` and livestream `BroadcastEvent`**
structured data. General product URLs are a gray area Google may silently ignore. **Before
shipping, prove it empirically:**

1. Submit a real `agent.pivota.cc/products/sig_*` URL under the Pivota credential.
2. Poll `get_index_status` / URL Inspection and confirm Google actually transitions the URL toward
   indexed within a reasonable window.
3. If Google ignores product URLs: **do not ship a "we submitted it to Google" claim.** Fall back
   to the mechanisms that always work for the Pivota PDP — sitemap inclusion, internal linking, and
   freshness pings — and scope the copy to those. (This is roughly what checklist (a) already says
   for the merchant's own page.)

This validation is the **go/no-go** for the active path. A negative result doesn't block the
already-shipped honest checklist; it just means the "Pivota submits it" button isn't real yet.

---

## Phased build plan

1. **Validation spike (no UI):** stand up the Pivota credential + property verification, submit one
   canonical URL, confirm Google honors it (the gate above). Throwaway-able.
2. **Pivota-owned submission core:** add the Pivota-credential token path; refactor
   `submit_url_to_gsc` / `submit_audit_canonical_urls` to submit under the Pivota credential keyed
   on canonical URL (merchant_id for attribution only). Keep best-effort/never-raise contract.
3. **Per-SKU trigger + read-back:** consume `cta.target_sku_key` in the portal `request_indexing`
   CTA; backend resolves SKU → canonical URL → submit; surface `gsc_url_submissions` status per SKU.
4. **Copy + flag flip:** ship the "get this product's Pivota page indexed" CTA and status copy;
   turn on `gsc_integration_enabled` (or a dedicated `gsc_pivota_submit_enabled`) once 1–3 pass.

---

## Open questions

1. **Service account vs. single Google identity** for the Pivota credential. Service account is
   cleaner (no human token refresh) if it can be added as a verified owner of `agent.pivota.cc`.
2. **New flag vs. reuse `gsc_integration_enabled`.** The existing flag's comment ties it to the
   per-merchant OAuth + audit-pipeline-consumes-state model. Recommend a separate
   `gsc_pivota_submit_enabled` so the two principals flip independently.
3. **Submit timing:** at PDP creation (every new `sig_*` page) vs. only on `get_indexed`
   prescription. PDP-creation is broader coverage; prescription-triggered is cheaper and tied to a
   merchant-visible action. Recommend prescription-triggered first, broaden later.
4. **Quota.** Indexing API has a default ~200 URLs/day per project. Batch canonical submissions
   under the one Pivota project — confirm headroom before broadening to PDP-creation triggers.
5. **Honesty if Google ignores product URLs** (validation negative): is "we asked Google to crawl
   your Pivota page (sitemap + freshness ping)" still worth a CTA, or do we leave `request_indexing`
   as checklist-only? Recommend checklist-only until the active path proves out.
