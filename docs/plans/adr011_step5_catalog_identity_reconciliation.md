# Step-5 Catalog Identity Reconciliation — Plan (2026-07-10)

Planning doc for the "fix the existing catalog" follow-up from
`docs/adr/ADR-011-rollout-handoff.md` §5.2: reconcile the **1,076 same-merchant
duplicate content_keys** and the **374 cross-merchant shared content_keys**.
Companion specs: ADR-010 (resolver, D-2, two-grain), ADR-011 (intake contract,
now live at all five doors), ADR-008 Appendix A (parked retro-merge design).

## 0. The headline: this backlog is operational noise, not identity ambiguity

The handoff framed step-5 as "the dangerous one" — careful resolver-driven
retro-merge with human review, because mis-merge is worse than fragmentation.
Fresh characterization of prod (2026-07-10, read-only) shows the backlog
decomposes almost entirely into four mundane causes, none of which need
Tier-1+ matching, embeddings, or the unbuilt D-2 merge schema:

### Cross-merchant (374 keys) — ~96% is ONE duplicate store connection

| Slice | Keys | What it actually is |
|---|---|---|
| `merch_bbd34645bc1950cc` + `merch_efbc46b4619cfbdf` | **360** | The **same Shopify store** (`92sfrj-bi.myshopify.com`) connected under two merchant accounts — rows share `source_product_id`. Both accounts still sync (last seen 06-29 / 07-09). |
| `pivota-review-demo` + `pivota-review-demo-2` | 9 | Two internal demo stores with identical catalogs. Expected; not a defect. |
| external_seed ↔ first-party (ownist, anuko, 92sfrj) | 5 | The genuine seed/first-party twin case — the existing `cross_merchant_redundant_external_seed` suppression lane (50 rows already) covers this pattern. |

### Same-merchant (1,076 keys) — dominated by seed URL-noise

| Slice | Keys | Rows | What it actually is |
|---|---|---|---|
| external_seed, **one shared canonical_url** | 571 | — | The same PDP seeded N times as distinct seed records (e.g. 43 rows of one foundation URL — per-shade/repeat crawls). |
| external_seed, all-distinct URLs | 193 | — | Mostly **campaign-slug clones** (dozens of `biodance.com/products/0627_cm_…` ad PDPs of one product) and **querystring variants** (`?variant=…&utm_source=pivota`) that URL normalization collapses. |
| external_seed, mixed / multi-domain | 80 / 55 | — | Combination of the above; 55 groups span >1 source_domain. |
| (external_seed total) | **844** | 2,364 | |
| The two 92sfrj merchant accounts | 115 + 115 | 497 + 497 | Identical counts — the same store's intra-catalog brand+title families, doubled by the duplicate connection. Collapses to ≤115 once Lane 1 lands. |
| url_audit | 2 | 4 | Tiny; handle in review rail. |

Every dup group has all-distinct `source_product_id`s — these are distinct
source records sharing brand+title, not re-mints. Intake is no longer adding
to this backlog (ADR-011 live; the 07-09 organic burst held the 1,076 plateau
exactly).

**Consequence:** per ADR-010's own escalation rule ("embeddings/LLM
adjudication remain deferred until scale demands them"), step-5 stays
deterministic + human-reviewed. We do **not** build the D-2 proposals schema,
Tier-1 matching, or auto-merge for this. The D-2/resolver track remains the
right home for *future* organic cross-merchant overlap; this backlog doesn't
exercise it.

## 1. Invariants the plan must respect (from ADR-010/-011)

- **`content_key` is never re-minted or dropped** for an existing row (hard
  invariant; it's the serving spine — `agent_pdp_view` PK, FK from
  `agent_decision_candidates`, recall joins, publish surface).
- All reconciliation therefore happens by **row suppression** (reversible
  tombstone: `suppression_reason` + `suppression_metadata`, existing pattern)
  and, where needed, **pg-membership** changes — never by editing keys.
- **Propose → human review → apply.** Every mutating script is dry-run by
  default; the apply cut runs only on a reviewed proposal file.
- **Money is untouchable by construction** (keys on merchant-scoped `cp_` ids,
  not pg/content_key) — the blast radius is serving/buy-box + publish, which is
  where the review attention goes.
- **Citation guard:** signatures are write-once and citable. No row with a
  minted `pivota_signature_id` that is cited (merchant_citations) may be
  suppressed while an uncited twin survives — the keep-one policy must prefer
  keeping the cited/sig-bearing row. Verify per group before apply.
- **Two-mirror gotcha:** a deactivated external seed does NOT auto-suppress its
  catalog_products mirror. The working set must join seed status; orphan
  mirrors get swept in Lane 0, not double-counted in later lanes.

## 2. Lanes, in order

### Lane 0 — working-set hygiene (build: 1 read-only script)
`scripts/step5_working_set.py` (read-only report): re-derives the backlog
excluding (a) suppressed rows, (b) external_seed rows whose seed is no longer
`active` (the two-mirror orphans — reported separately for an explicit
suppression sweep using the existing `onboard_external_brand_from_crawl`
pattern), (c) the demo stores. Emits per-lane group lists as JSON for the
proposal cuts below. This report is the single source of truth for lanes 1–4
and re-runs after each lane to show convergence.

### Lane 1 — the duplicate store connection (clears ~360 cross-merchant + halves the shopify same-merchant slice)
**Founder/ops decision required, not code:** which of
`merch_bbd34645bc1950cc` / `merch_efbc46b4619cfbdf` is the canonical account
for `92sfrj-bi.myshopify.com` — and whether this whole cluster is test data
(the surviving account also carries the wix dog-harness test products that
mint keyless `no_identity_inputs` rows every sync tick). Execution once
decided: disconnect the losing store connection, suppress its catalog rows
(`suppression_reason='duplicate_store_connection'`), and verify the losing
account stops syncing. Also decide disposition of the demo-store 9 keys
(suggest: mark/exclude as test track, no suppression).

### Lane 2 — same-URL seed dedup (571 groups; mechanical, lowest risk)
Same merchant + same content_key + same **normalized** canonical_url →
keep exactly one row (prefer: sig-bearing/cited > seed still active > richest
payload > newest), suppress the rest with
`suppression_reason='same_merchant_same_url_dup'` and evidence in
`suppression_metadata` `{lane, group_key, kept_row, script, run_id}`.
Also deactivate the losing seeds (both paths, per the two-mirror rule).
Normalization strips querystring/UTM (reusing
`pdp_matcher.deterministic.normalize_canonical_url`), which pulls the
`?variant=` clones out of Lane 3 into this lane. Dry-run emits a proposal
file + a stratified sample (~50 groups) for human review before `--execute`.

### Lane 3 — campaign-clone groups (remainder of the 193 + 80 mixed)
Same merchant + same content_key + same source_domain, distinct normalized
slugs (the biodance ad-PDP pattern). Keep-one policy is a judgment call
(canonical slug heuristic; active seed; citation guard) → propose-first, human
reviews every group (a few hundred rows at most). Anything not confidently a
clone stays unsuppressed and falls to Lane 4. Upstream: the crawl-side fix
sketched in `docs/HANDOFF_crawl_side_dedup.md` (PIVOTA-Agent repo) should be
confirmed still holding so this lane doesn't refill.

### Lane 4 — reviewed residue (small)
The 55 multi-domain groups, the 5 genuine seed↔first-party twins (use the
existing `cross_merchant_redundant_external_seed` lane), the 2 url_audit
groups, and Lane-3 rejects. Route through `pdp_review_tasks`
(module='identity') so decisions accrete as the proto gold-label set ADR-010
Action Item 5 wants. Genuine variant families that *should* share a family key
are explicitly **kept** (two-grain: family sharing is correct; variant grain
is ADR-010's future buy-box work, out of scope here).

## 3. What we deliberately do NOT build now

- D-2 schema (proposals table, provenance columns on product_group_members,
  identity_resolution_events, unmerge machinery) — suppression is already
  reversible and carries evidence in `suppression_metadata`; the backlog
  doesn't need competing proposals.
- Tier-1/2/3 matching, auto-merge, cross-merchant canonical cards.
- Any content_key or pg rewrite. (`product_group_members` is only touched if
  Lane 0 shows suppressed rows leaving orphan singleton memberships — sweep
  with existing `ensure_singleton_group_membership` idempotency if so.)

## 4. Sequencing, verification, success metric

1. Lane 0 report → review → orphan-mirror sweep (reversible) → re-run D-1.
2. Lane 1 decision (founder) → execute → re-run D-1. Expect cross-merchant
   374 → ~14 and same-merchant 1,076 → ~961 immediately.
3. Lane 2 dry-run → sample review → execute → re-run D-1. Expect the bulk of
   the 571 one-URL groups to clear (−~500 keys).
4. Lane 3 propose → full review → execute. Lane 4 through the review rail.
5. Done-when: `measure_identity_duplication.py` shows same-merchant dup keys
   at the "legitimate variant family" residue (target < ~100, labeled) and
   cross-merchant ≈ demo-store count only; numbers stay flat the following
   month (intake contract holds the line).

Each apply cut is one PR (script + proposal file + post-run D-1 output), so
every mutation is reviewable and revertible (`suppression_reason IS NULL`
restore by run_id).

## 5. Open decisions (blocking, in order)

1. **92sfrj-bi cluster** (Lane 1): which merchant account survives — or is the
   whole cluster test data to be suppressed/disconnected outright? Also: fix
   the wix null-brand keyless-MINT source at the same time?
2. **Demo stores**: exclude-from-metrics vs suppress.
3. **Keep-one policy for campaign clones** (Lane 3): is "cleanest slug +
   active seed + citation guard" acceptable, or does BD want specific ad PDPs
   kept servable?
4. **Crawl-side dedup status** (PIVOTA-Agent): confirmed holding since
   2026-07-08? Lane 3 refills if not.
