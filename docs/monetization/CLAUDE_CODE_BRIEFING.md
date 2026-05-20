# Claude Code Briefing — Build Pivota Monetization System v1.3

You are executing the build of Pivota's in-house monetization system. The architecture is locked. Your job is to dispatch Codex CLI sessions in parallel waves, review their outputs, apply migrations, and report architectural questions back to Cowork.

## Inputs (read these first, in this order)

1. **Architecture spec** — `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx`
   The canonical design. Codex tasks must implement v1.3 exactly. Do not improvise on architecture; surface architectural questions back to Jack (and from there to Cowork).

2. **Task Pack** — `docs/monetization/Pivota_Monetization_Codex_Task_Pack.docx`
   Eight Codex tasks (T1–T8). Each has its own context, prompt, files-to-read list, acceptance criteria, and "don't do" guardrails. Parallelism schedule on page 2.

3. **Dispatch kit** — `docs/monetization/codex_dispatch/`
   - `README.md` — wave schedule
   - `T1_stripe_audit.md` — full prompt for T1, ready to pipe (template for how to construct T2–T8 if you don't want to extract from the DOCX)
   - You can construct T2–T8 prompts from the Task Pack DOCX, or write them in the same style as T1 into this directory.

4. **Cohort context (optional reading)** — `outputs/Cohort_1_Top20_Overview_Brief.docx` (in Jack's session outputs, not in repo). The 20 brands the monetization system will serve in cohort #1.

## Working pattern

**Wave dispatch sequence:**

| Wave | Tasks | Parallelism |
|------|-------|-------------|
| 1 | T1 (Stripe audit) + T2 (DB audit) | Both at once, no dependencies |
| 2 | T3 (17 migrations) | Single; needs T2's audit doc |
| 3 | T4 (webhooks) + T5 (metering) + T6 (GMV aggregation) | Three at once; need T1+T3 / T3 / T2+T3 |
| 4 | T7 (invoice gen) + T8 (partner settlement + Test Clock) | Both at once; need T3+T6 / T3 |

**For each task:**
1. Construct the Codex prompt (from Task Pack DOCX or `codex_dispatch/*.md` files).
2. Pipe to `codex` CLI in a backgrounded subprocess. Capture stdout/stderr to `docs/monetization/codex_dispatch/outputs/T{N}.log`.
3. When the task completes, verify Codex produced the expected deliverable at the expected path (see acceptance criteria in the Task Pack).
4. Spot-check the output for sanity (e.g., for migrations: do they compile against the existing schema? for service code: does it import what it claims to?).
5. If acceptance criteria fail, re-prompt Codex with a clarification round; if it's an architectural question, surface to Jack with the specific decision needed.

**Don't auto-merge.** Codex's output should land in branches or unstaged changes for Jack to review before commit. The v1 first cycle is "manual finalize" — same philosophy applies to the build itself.

## What to do when an architectural question surfaces

Stop the affected workstream. Don't speculate. Write the question to a file at `docs/monetization/questions_for_cowork.md` (append, don't overwrite) with:
- Task ID (T1–T8)
- The architectural question
- What Codex was asked
- What Codex produced or proposed
- Recommended resolution if any

Jack will bring those questions to Cowork (me) for resolution. I'll write back into the blueprint as v1.4+ if the answer changes architecture.

## Things explicitly NOT in scope for this build

These come later, after Markato-ready:
- Markato term sheet (designed after the first clean billing cycle proves out)
- Partner program v2 for second + third anchors
- Protocol-native settlement (v1.5)
- Admin UI in pivota-employee-portal (Jack scopes after backend lands)
- Merchant-portal credit/dispute UI (same)
- Production deployment runbook (written after Week 9 dry run)

If Codex starts drifting into these areas, redirect it back to the v1.3 scope.

## Markato-ready exit criteria

The build is complete when all 15 items in the v1.3 Acceptance Checklist (Appendix E of the blueprint) pass on a Stripe Test Clock simulation + one real cohort brand (probably 7Journeys). Until then: not ready, no Markato term sheet, no cohort #2 intros.

## Going-forward token budget

Token-heavy work (code reading, implementation drafting, migration writing, test scaffolding) belongs in Codex sessions. Don't try to do those in your own Claude Code session — dispatch them. Your Claude Code session is for orchestration, review, git management, and resolving the smaller-scoped things Codex can't (running migrations against the DB, executing tests, hand-edits that are too small for a Codex round trip).

Cowork (Jack's other session) is for architecture/product decisions. Don't pull Cowork into implementation; pull Cowork into design.

## Quick start

```bash
# 1. Read the blueprint + task pack first.
#    The blueprint is the source of truth.

# 2. Dispatch Wave 1 (T1 + T2 in parallel).
cd /Users/pengchydan/dev/pivota-backend-receipt-suppress-fix
mkdir -p docs/monetization/codex_dispatch/outputs

# T1 — Stripe audit (read-only, no deps)
codex < docs/monetization/codex_dispatch/T1_stripe_audit.md \
  > docs/monetization/codex_dispatch/outputs/T1.log 2>&1 &

# T2 — DB audit (read-only, no deps).
# Construct the T2 prompt from the Task Pack DOCX or write a T2_db_audit.md
# in codex_dispatch/ following T1's structure. Then:
codex < docs/monetization/codex_dispatch/T2_db_audit.md \
  > docs/monetization/codex_dispatch/outputs/T2.log 2>&1 &

wait

# 3. Verify Wave 1 outputs exist:
ls -l docs/monetization/T1_stripe_codebase_audit.md
ls -l docs/monetization/T2_db_audit.md

# 4. If both look good, proceed to Wave 2 (T3 migrations).
# 5. Wave 3 (T4 + T5 + T6 parallel).
# 6. Wave 4 (T7 + T8 parallel).
```

That's the build. Architecture is locked in v1.3; Codex implements; you orchestrate; Cowork stays in design mode.
