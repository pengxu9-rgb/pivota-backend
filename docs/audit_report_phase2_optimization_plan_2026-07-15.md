# Audit Report Phase-2 Optimization Plan — 2026-07-15

Builds on the shipped Phase-1 track (report summary contract 1.2, full-report
re-layout, niche-first evidence, measured get-cited reasons, paid PPT deck,
homepage hero — backend #1410/#1413/#1414/#1415/#1417, portal #168–#173).

**Framing (from the 2026 industry benchmark — Profound / Peec / Otterly):**
our single-report depth already leads the monitoring-first competitors (per-SKU
dimension scoring, verbatim prompt evidence, executable actions — the category's
common gap is "diagnoses but doesn't fix"). Our gaps are the **time dimension**
(trends, diffs, share-of-voice) and the **distribution dimension** (sharing,
alerts, habit loop). Every item below either closes one of those gaps or
deepens the execution-loop moat. House rules carry over: verbatim measured
data only, no fabricated joins, unverified URLs never render, unmeasurable ≠
zero.

**Dark-launch convention:** presentation items gate on **contract-field
presence** (absent field → nothing renders), not new env flags — the pattern
that already worked for contract 1.1/1.2. Only B1/B2 (new endpoints with
side effects) get real flags.

---

## Workstream A — Time dimension (data already collected; presentation-layer)

### A1. "Since last audit" block  — S/M
- **What:** under the score strip, when a prior comparable run exists:
  "Fixed since last audit: N prompts · Newly losing: M · Still losing: K",
  per-dimension deltas, days since.
- **Data:** `reaudit_delta` (persisted per run by `_attach_reaudit_delta`),
  `outreach_outcomes`, `brand_rollup.tracking.history`. Persisted at run time
  → renders on runs created after deploy; absent on older runs (accepted —
  same rule as losing_queries).
- **Backend:** contract 1.3 additive block `since_last_audit` (counts +
  capped example lists + dimension deltas + previous_run_id). Same-semantics
  guard: only compare runs with identical prompt-basis identity (basis
  pinning already exists) and same score semantics version.
- **Portal:** compact strip row + expandable lists (Disclosure).
- **AC:** two consecutive Mojawa runs render fixed/new/still counts that
  hand-match the raw reports; first-ever run renders nothing.

### A2. Competitor share-of-voice  — M
- **What:** "who wins the prompts we probed" — brand vs top named
  competitors, this run + trend across runs. The category's headline metric
  (Profound/Peec/Otterly all lead with it); we have the data, not the view.
- **Metric (honest definition):** share of probed prompts where X was
  named/cited in the grounded answer, per run. Numerators from
  `who_ai_cites_instead.competitors[].times_named` + brand cited runs from
  `run_facts`/`source_summary`; denominator = prompts probed. Label the
  basis on-chart ("of the N buyer-intent prompts we tested").
- **Backend:** contract block `share_of_voice` {brand: {name, pct},
  competitors[≤5], prompts_probed}; trend endpoint reading the merchant's
  recent same-basis runs (report_jsonb extraction, capped + cached).
- **Portal:** horizontal bar row (this run) + sparkline (trend) in the
  overview card under the strip.
- **Risks:** cross-run comparability when the prompt basis changed → only
  trend across same-basis runs, disclose otherwise; competitor-name
  normalization (alias folding exists — reuse brand-alias utilities).
- **AC:** Mojawa run shows Shokz/Sony/Bose shares that hand-match
  times_named; basis label always present.

### A3. Prompt Explorer  — S (portal-only)
- **What:** one flat, filterable table of EVERY probed prompt across SKUs
  (query, engine verdicts, axis, spec-matched badge, win/loss, cited hosts,
  SKU), inside the diagnostics tier. Includes WON prompts — today's report
  over-indexes on losses. Peec's whole dashboard is prompt-centric; ours is
  buried per-SKU.
- **Data:** `per_sku_reports[].opportunity.per_prompt[]` — already in the
  full payload; zero backend change.
- **AC:** row count equals sum of per_prompt lengths; filters (engine /
  axis / outcome / SKU) compose; CSV export button (client-side).

### A4. Action impact badges  — S
- **What:** each Start-here action gets "lifts: {dimension}" + effort
  ("self-serve" / "Pivota-assisted" — fields already exist) so merchants
  read impact × effort at a glance (Lighthouse "estimated savings" analog).
- **Backend:** `primary_gap → lifted dimension` map (derivable from the
  existing gap classifier) stamped on prioritized actions / contract
  top_actions. Never a fabricated numeric lift — dimension + direction only.

## Workstream B — Distribution & habit loop

### B1. Scheduled re-audit + change email  — L, **flag + explicit opt-in**
- **What:** merchant opts into weekly/monthly re-audit; email digest = A1's
  diff (fixed/new/still + score move). The category-standard habit loop and
  a natural recurring-credit sink.
- **Backend:** schedule table + cron (reuse APScheduler infra — mind the
  interval-reset gotcha fixed in #1377), pre-flight credit check (skip +
  notify when insufficient, never silent-drain), standard per-run billing.
- **Needs user decision:** cadence options, billing copy, email provider
  status (see Decisions).

### B2. Read-only share link  — M, **flag**
- **What:** "Copy share link" next to Export deck → unguessable tokenized
  URL, revocable, default 30-day expiry, rendering a read-only report
  (agency/channel-partner demo scene — complements the PPT).
- **Scope (recommended):** score strip + narrative + Start-here (no action
  buttons) + get-cited (no draft buttons) + diagnostics; EXCLUDES custom
  prompts, billing/free-audit chrome, deck export.
- **Security:** 128-bit token, server-side revocation, no merchant PII
  beyond brand name, robots noindex.

### B3. Peer percentile — SPEC ONLY, deferred
- "Better than 62% of beauty brands" — gated on cohort size (≥30 same-
  vertical per-SKU-audited brands). Contract slot already reserved. Revisit
  with the band-anchor ratification (Decisions #1).

## Workstream C — Get-cited pitch path (decided direction 2026-07-15)

### C1. Registry contact_url + tiered card destinations  — M
- Tiering per host: (1) pitch-ready registry host → recipient shown +
  `mailto:` prefilled with the generated draft (send stays with the user —
  auto-send remains parked); (2) registry host with **verified**
  `contact_url` → "Open pitch page" deep link; (3) community/KOL → existing
  specific thread/channel starters; (4) fallback → homepage (never a
  guessed URL).
- Registry schema: add optional `contact_url` (+ `contact_url_verified_at`);
  mind the duplicate-host-key silent-clobber gotcha (dup-key test exists).
- Backend passthrough onto outreach moves / pitch targets; portal ChannelRow
  renders by tier.

### C2. Contact-page discovery sweep  — S/M
- Extend the registry-growth sweep: candidate contact/write-for-us/tips
  URLs per registry host, verified (HTTP 200 + content heuristic) →
  emitted as PROPOSALS for human approval into the registry. Verified-only
  ever ships.

## Workstream D — Debt & guardrails (parked review P2s)

- **D1. Deck coherence + COGS guard — S:** deck adds the score explainer /
  "not counted" note (contract 1.2 story consistency); cache the exec-summary
  per run_id (kills repeat-export LLM COGS AND aligns with the one-charge
  idempotency).
- **D2. Portal CI — S:** wire `node --test` + `verify:summary` + `next build`
  as a merge gate. Blocked on runner choice (GH Actions quota — Decisions #5).
- **D3. Cleanup — S:** remove orphaned OutreachMovesPanel (GetCitedPanel is
  the real path), prune stale worktrees, document .env.local flags.

---

## Sequencing (each wave = implement → agent review → merge → prod-verify on Mojawa)

| Wave | Items | Rationale |
|---|---|---|
| 1 | A1 + A4 + C1 | Highest value-per-risk; A1 needs runs to accumulate → ship earliest; C1 is the decided pitch-path direction |
| 2 | A2 + A3 (+ D1, D3 riders) | SoV needs A1's same-basis guard; Explorer is independent but pairs with A2 in the diagnostics tier |
| 3 | B1 + B2 + C2 | New pipes; B1 email digest consumes A1's diff |
| Ongoing | D2, B3 spec | Runner decision / cohort growth |

## Decisions needed (blocking marked ★)

1. **Band anchors** (60/75/90 → "6=及格") — now live-displayed; ratify or
   re-anchor. Pairs with B3 percentile later.
2. ★ **B1 pricing/cadence + email provider** — weekly vs monthly options,
   billing copy, and whether the outreach email infra is approved for
   merchant-facing digests.
3. ★ **B2 share-link scope** — confirm the recommended inclusion list.
4. **Head-term probe budget** (still open from round 3): keep 1-2 head
   probes as baseline vs reallocating budget to spec-matched prompts —
   measurement change, affects cross-run comparability; recommend deciding
   before B1 locks in scheduled同基准 re-runs.
5. **CI runner** for D2 (GH Actions quota状态).

## Non-goals (deliberate)

- Sentiment scoring (our verify layer's answer-accuracy is more actionable).
- Daily lightweight monitoring (cost model mismatch with per-run billed
  deep audits; B1's weekly/monthly cadence is the fit).
- Auto-send outreach (stays parked; mailto keeps the human in the loop).
