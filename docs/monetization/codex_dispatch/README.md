# Codex Dispatch Kit — Pivota Monetization System v1.3

Self-contained Codex prompts ready to dispatch in parallel waves.

## Quick start

```bash
cd /Users/pengchydan/dev/pivota-backend-receipt-suppress-fix/docs/monetization/codex_dispatch

# Wave 1 (parallel) — read-only audits, no dependencies
./dispatch_wave1.sh

# Wave 2 — migrations (after Wave 1 completes; needs T2 audit doc)
./dispatch_wave2.sh

# Wave 3 (parallel) — service drafts (after Wave 2)
./dispatch_wave3.sh

# Wave 4 (parallel) — invoice generation + partner settlement (after Wave 3)
./dispatch_wave4.sh
```

Each script invokes Codex with a prompt file and writes the session log to `outputs/`.

## Wave schedule

| Wave | Tasks | Parallelism | Blocks |
|------|-------|-------------|--------|
| 1 | T1 (Stripe audit), T2 (DB audit) | Both at once | — |
| 2 | T3 (migrations) | Single | Needs T2 |
| 3 | T4 (webhooks), T5 (metering), T6 (GMV) | All three at once | Need T1+T3 / T3 / T2+T3 |
| 4 | T7 (invoice gen), T8 (partner + Test Clock) | Both at once | Need T3+T6 / T3 |

Total wall-clock to all 8 deliverables: ~5–7 days at typical Codex throughput, vs. ~3 weeks if sequential.

## Files

Prompt files (self-contained — each can be piped to Codex independently):
- `T1_stripe_audit.md`
- `T2_db_audit.md`
- `T3_migrations.md`
- `T4_billing_routes.md`
- `T5_metering_service.md`
- `T6_gmv_aggregation.md`
- `T7_invoice_generation.md`
- `T8_partner_settlement.md`

Dispatch scripts:
- `dispatch_wave1.sh` — T1 + T2 parallel
- `dispatch_wave2.sh` — T3 alone
- `dispatch_wave3.sh` — T4 + T5 + T6 parallel
- `dispatch_wave4.sh` — T7 + T8 parallel

Outputs:
- `outputs/T*.log` — session transcripts (each task writes its log here)
- Codex's actual code/doc deliverables land in their target paths inside the repo (e.g., `services/metering_service.py`, `db/migrations/*.sql`, `docs/monetization/T1_stripe_codebase_audit.md`)

## If your Codex CLI invocation differs

The dispatch scripts assume `codex < prompt_file.md` pipes the prompt to a non-interactive Codex session. If your CLI version uses different flags (`--prompt-file`, `--input`, etc.), edit the `CODEX_INVOKE` variable at the top of each dispatch script. The prompt files themselves are CLI-agnostic — they work however you feed them to Codex.

## Source

Generated from `Pivota_Monetization_Codex_Task_Pack.docx` (parent dir). See `Pivota_Monetization_System_v1.3_Blueprint.docx` for the architecture spec each task implements.
