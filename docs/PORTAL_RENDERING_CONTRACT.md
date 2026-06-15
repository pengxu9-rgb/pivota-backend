# Portal Rendering Contract — AI-Readiness Report (Workstream A)

**For:** the frontend team building `app/dashboard/agent-center/ai-readiness/page.tsx`.
**Status:** backend ready · **Date:** 2026-06-15

The backend now produces a complete, merchant-grade report. **Nothing renders to
merchants yet — this is the hard blocker.** This is the exact contract to render
it. The backend side is done and frozen; no backend change is required to ship A.

## Where the data comes from
- **Endpoint:** `GET` the merchant audit run (served via `merchant_audit_routes`),
  payload `{ "brand_report": <report> }`.
- The report passes through `sanitize_report_for_merchant`, a **passthrough** that
  only strips internal score `breakdown` blocks — `merchant_narrative` and
  `authority_map` reach you intact.
- Two top-level objects to render: `brand_report.merchant_narrative` and
  `brand_report.authority_map`.

## 1. `merchant_narrative` — the 7 sections (render in order)
| Field | Type | Render |
|---|---|---|
| `headline_story` | string | One honest sentence — hero line. |
| `whats_working` | object | `summary` (string) + chips of `findability_hosts` + `branded_navigational_probes`/`category_discovery_probes` counts (hide if both 0 — see degrade rules) + `evidence_excerpt` (object or null — quote block with `source_labels`). |
| `where_youre_losing` | object | `summary` + `who_ai_cites_instead` (see §3). |
| `per_sku_scorecard` | array | Per SKU: `sku_title`, `status` (plain label), `what_it_means`, `surfaced_only_via_own_listing` (bool — badge "listed, not recommended"), `independently_recommended_for_category` (bool). |
| `verify_summary_plain` | object | `text` (plain-language reach-vs-accuracy). |
| `prioritized_actions` | array | Per action: `headline`, `first_move`, `why_this_first`, `growth_phase_label` (group header: "Create & distribute" / "Evidence intake"). |
| `honest_limits` | array<string> | Render verbatim as a muted "what we didn't measure" list. |
| `verdict_label` / `verdict_explanation` | string | Optional badge + tooltip. |

## 2. `authority_map` — the findability/endorsement split (the core of Fix 2)
Render `authority_map.host_attribution_summary` as **two visually distinct buckets — never merged**:
- **Findability** (own/marketplace listings — "your product is indexed"): `findability_hosts`.
- **Endorsement** (independent recommendation): `endorsement_hosts`; the *category* gate is `endorsement_category_hosts` / `independently_recommended_for_category`.
- `surfaced_only_via_own_listing: true` → show the explicit "listed, not recommended" state; **never** render findability as endorsement.
- `competitor_hosts` → a separate "who AI cites instead" group (NOT the merchant's).

Per-host rows (`authority_map.hosts[]`) carry `host`, `citation_role`
(`own_domain | marketplace_self_listing | independent_retailer | editorial_review
| creator | forum | competitor | unclassified`), `recommendation_class`,
`prompts_cited_count`, `cited_on_category_query`. Use `citation_role` for the chip
label/color; group findability roles vs endorsement roles.

## 3. `where_youre_losing.who_ai_cites_instead`
`{ available: bool, cited_hosts: [{host, citation_role, prompts_cited_count}], competitors: [{name, times_named}], note: string|null }`.
- `available: false` → render `note` verbatim ("landscape not available…"). **Do not invent** competitors/hosts.
- Show `competitors` as named brand chips; `cited_hosts` as host chips with role.

## Degrade rules (no fabrication, no inflation — match the backend's discipline)
1. **Hide `0/0` branded/category counts** — only populated on post-Fix-2 runs; a backfilled run may have 0. Don't show "0 probes".
2. **Render every "not available" / "honest_limits" string verbatim.** Never fill a gap with a guess.
3. **Findability ≠ endorsement, always.** A `marketplace_self_listing` host is the merchant's own listing — never label it "AI recommends you".
4. **`merchant_narrative` may be `null`** (best-effort build; a malformed run degrades to no narrative) — fall back to the existing score view, don't error.

## Worked example (real Aruen run `7fc74991`)
- `headline_story`: *"Aruen is independently recommended for the category, not just found through your own listings…"*
- Findability: `aruen.us`, `desertcart.in`, `ebay.com`, `ubuy.co.in`, `gosupps.com`, `mercari.com`, `target.com`, `kiwla.com`.
- Endorsement: `tiktok.com`, `youtube.com`, `lemon8-app.com`, `whowhatwear.com`; category endorsement = `tiktok.com` only.
- `who_ai_cites_instead.competitors`: Medicube, Abib, Byoma, Clinique, Dermalogica (real grounded names).
