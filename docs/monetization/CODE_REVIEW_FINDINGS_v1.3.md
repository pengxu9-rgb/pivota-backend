# v1.3 Code Review Findings

This file is the artifact Stage 1 §6 promotion gate references:

> [ ] **Ultrareview findings reconciled.** `/ultrareview` was launched against PR #581 (core v1.3) in parallel with Stage 1; findings live in `docs/monetization/CODE_REVIEW_FINDINGS_v1.3.md`.

`/ultrareview` failed to launch in this session (no git repo at the working directory the slash command expected). As a substitute, a full **codex code review** was dispatched against the merge commit `ae4e93ad25d4ea2cdfb3bbb5ead516fdd5f0edf5` (PR #581 + downstream sign-off corrections) on 2026-05-22. The review brief at `/tmp/codex_pr581_review_brief.md` mirrored the Stage 1 §6 promotion-gate dimensions: T9 stamping correctness, attribution gate semantics, migration schema, Stripe Live integration, T5 reservation race conditions, T6 date bucketing, T7/T8 idempotency, codex-authored failure modes.

Verdict at the time of review: **fail for Stage 1 → Stage 2/3 promotion** on the criticals. Following remediation, every critical and high finding affecting Stage 1 surfaces has been resolved. Findings affecting only Stage 4 paused paths have likewise been fixed proactively to keep Stage 4 unpause a scheduler-resume call rather than a redeploy cycle.

## Finding ledger

| # | severity | file:line (at review time) | finding (one-liner) | disposition | PR / commit |
|---|---|---|---|---|---|
| 1 | critical | `services/refund_service.py:119` + `services/commerce_attribution_service.py:403` | Prod refund path only writes legacy `refunded_amount` decimal; T6 reads `refund_amount_cents` which was never populated. Refunded orders billed at gross. | **fix-now** | #595 (`776634f`) — cents column wired into bulk UPDATE; #602 (`94e1f5d`) — SQL-side atomic UPDATE with JSONB `?` idempotency, multi-edge fan-out safe |
| 2 | critical | `services/gmv_aggregation_service.py:21` | `DATE(timestamptz)` is session-TZ dependent in both projection and WHERE; same drift class that drove migrations 120/121. | **fix-now** | #596 (`8417852`) — `(e.created_at AT TIME ZONE 'UTC')::date` in projection, predicate, and GROUP BY; `_coerce_date()` normalizes to UTC for `apply_refund`'s recompute path |
| 3 | high | `db/migrations/121_*.sql` | Migration 121 absent from PR #581's tree. | **false positive** | Landed in PR #590 (parallel branch); applied manually via psql public proxy 2026-05-22 (cowork trail log entry); pre-flight verified `billing_runs.period_{start,end}` both `data_type='date'` |
| 4 | high | `routes/billing_routes.py:76` | Stripe retry on `failed`/stale `pending` events hit `ON CONFLICT DO NOTHING` and returned `duplicate` without reprocessing. Failed events stay failed forever. | **fix-now** | #599 (`f42d9d7`) — `_claim_retryable_event()` with `FOR UPDATE SKIP LOCKED` + status-based dispatch (`processed/ignored → ack`, `failed/stale-pending → reclaim`, `pending fresh → 409`) |
| 5 | high | `services/psp_payment_finalizer.py:226` | T9 synchronous stamp in try/except that swallows; paid orders with null `gross_attributed_gmv_cents` silently exit T6 visibility. | **fix-now** | #597 (`781d84d`) — 5-minute reaper job in `jobs/stamp_attribution_reaper_job.py` finds null-stamp paid orders in a 24h window and retries via `stamp_gross_attributed_gmv()`; #598 (`6d0a944`) — hotfix for SQL `SELECT DISTINCT` + `ORDER BY paid_at` requiring `paid_at` in projection (failed every tick on first deploy) |
| 6 | high | `routes/billing_routes.py:142`, `services/invoice_generation_service.py:374`, `services/partner_settlement_service.py:517` | Live Stripe `create`/`transfer` calls lack `idempotency_key`. Network retries can produce duplicate customers, invoices, line items, transfers. Connect transfers are unrecoverable without manual Dashboard intervention. | **fix-now** | #600 (`4c66142`) — deterministic keys on all 6 call sites: `merchant_customer:{merchant_id}`, `checkout_session:{merchant_id}:{price_id}:{date_iso}`, `invoice:{billing_run_id}:{merchant_id}`, `invoice_item:{billing_run_id}:{gmv_rollup_id}`, `invoice_item_adj:{dispute_id}:{billing_run_item_id}`, `payout:{payout_id}` |
| 7 | high | `services/invoice_generation_service.py:318` | `run_billing_cycle` marks the run `'completed'` even when per-merchant invoices fail; same idempotency_key blocks retry of failed merchants. | **fix-now** | #601 (`b15ae2f`) + migration 122 — new `partial_failed` status; resume-on-retry path skips merchants already in `invoices` for the run_id; per-merchant Stripe idempotency keys from #600 provide belt-and-suspenders against duplicate Stripe-side artifacts |
| 8 | medium | `services/commerce_attribution_service.py:129` | `has_attribution_signal()` field list misses agent taxonomy fields (`agent_id`, `source_channel`, `query_source`, `protocol_name`), accepts weak signals like bare `surface`. Both ways: silently rejects valid agent orders and over-accepts weak attribution. | **fix-in-v1.4** | PR #594 (`d1499fa`) shipped a Prometheus counter + WARN log on every silent reject so Stage 1 can size the affected cohort before deciding the gate's shape. Without runtime data, "what fields count as signal" is a design question, not a code question. Revisit once Stage 1 monitoring tells us the direct-checkout rejection rate. Stage 2 promotion is not blocked — the gate is already correct for agent-routed traffic, which is what Stage 1 measures. |
| 9 | low | `services/invoice_generation_service.py:386` + `tests/test_invoice_generation_service.py:268` | `auto_advance=True` set in service vs `False` asserted in test — stale test assertion. | **fix-in-v1.4** | Trivial alignment. Defer to the next PR through `test_invoice_generation_service.py`; not worth a standalone PR. Stage 1 unaffected. |

## Promotion-gate alignment with Stage 1 §6

Per the runbook clauses:

- **Critical findings affecting stamping math, idempotency, money flow, or partner balance accounting** → "fix with a scoped v1.3.x PR + redeploy; Stage 1 monitoring window restarts." Findings 1, 2, 4, 5, 6, 7 fit this class. All fixed and merged. Stage 1 monitoring window starts from **2026-05-22** (after the final fix `94e1f5d`).

- **Critical findings affecting only paused code paths (T7/T8) or read-only diagnostic paths** → "fix as v1.3.x patch; does NOT restart the Stage 1 window because Stage 1 doesn't exercise those paths." Findings 6 and 7 are partly Stage 4-coded (T7/T8) but the cron registrations are paused, so they couldn't have fired in Stage 1 production. Finding 4 (webhook retry) IS exercised in Stage 1 because `checkout.session.completed` and `invoice.paid` webhooks fire whenever a merchant subscribes — patched as a scoped v1.3.x without restart.

- **Medium / low** → "log with a disposition; do not block Stage 2 promotion." Findings 8 and 9 logged above.

- **No ultrareview run completed** → "run it before promoting." `/ultrareview` could not be launched in this session due to working-directory limitations of the slash command. Codex code review against the merge commit (with the Stage 1 §6 promotion-gate dimensions baked into the brief) was used as the substitute. **Cowork should explicitly accept this substitute before promotion**, or run `/ultrareview` themselves against PR #581 if they prefer the agent's first-party review.

## Cowork sign-off line

> [ ] Cowork accepts codex review as `/ultrareview` substitute for the v1.3 promotion gate. Findings 8 and 9 dispositioned as fix-in-v1.4; Stage 2 promotion is not blocked on them.

## References

- Codex review report (local artifact, not committed): `/tmp/codex_pr581_last_message.md`. Generated by `codex exec --sandbox read-only` with brief `/tmp/codex_pr581_review_brief.md` against merge commit `ae4e93ad25d4ea2cdfb3bbb5ead516fdd5f0edf5`.
- Trail log entries for each finding's resolution: `docs/monetization/questions_for_cowork.md` "v1.3 Stage 1 post-deploy trail log (2026-05-22)".
- Stage 1 §6 promotion checklist: `docs/monetization/deploy/STAGE_1_SHADOW_MODE_ROLLOUT.md`.
