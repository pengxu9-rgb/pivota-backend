# Codex prompt — T2: DB migrations + schema audit (read-only)

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already integrated as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.
Existing patterns to follow: see db/, services/, routes/, adapters/ — match style of existing files.
Output: code in the existing repo layout. Migrations as SQL files in db/migrations/.
Don't add new dependencies unless absolutely required. Don't rewrite existing code unless explicitly asked.

## Task

v1.3 adds 14 new tables and extends 3 existing ones. Before drafting migrations, we need a complete picture of the existing migration style, naming conventions, FK patterns, and which existing tables we're extending. Produce a single markdown document that gives any future implementation agent enough context to write all 17 migrations without re-discovering the database layer.

## Files to read

```
db/                    (entire directory; focus on: accounts.py, merchants.py, orders.py,
                        commerce_attribution.py, payout_repo.py, merchant_tasks.py)
db/migrations/         (entire directory; read the last 10 migrations to learn the style)
models.py              (if present at project root or db/ root)
schema.py              (if present at project root or db/ root)
```

Plus: any file the above imports from that is schema-relevant.

## Output

Write to: `docs/monetization/T2_db_audit.md`

The document MUST contain these sections (in this order):

1. **Migration convention** — how are migrations named, structured, applied? Are they raw SQL files or Alembic? What's the naming pattern? How are UP and DOWN handled?
2. **Existing tables relevant to monetization** — fully document the columns + types + indexes + constraints for: `merchants`, `shop_users`, `orders`, `surface_click_events`, `commerce_attribution_edges`, `agent_payouts`, `payout_repo`. Be concrete: exact column names, Postgres types, nullability, defaults.
3. **FK patterns** — what's the convention for FKs? UUIDs or bigints for primary keys? UUID generation (gen_random_uuid() vs uuid_generate_v4())? CASCADE rules?
4. **Timestamp conventions** — `created_at` / `updated_at` — TIMESTAMPTZ vs TIMESTAMP? Timezone handling? Default expressions?
5. **JSONB vs structured columns** — when does the codebase use jsonb and when structured columns? Any naming convention for jsonb fields?
6. **Index patterns** — composite indexes, partial indexes, naming conventions — any patterns to follow?
7. **Extension points** — for the 3 tables v1.3 extends (`merchants`, `commerce_attribution_edges`, `agent_payouts`), document exactly where the new extension columns slot in and what the current table definition looks like. Flag any constraints that T3 migrations must not break.

## Acceptance criteria

- Document written to the exact path above (`docs/monetization/T2_db_audit.md`).
- Every existing table relevant to monetization has its columns + types listed.
- Migration style documented (raw SQL vs Alembic + naming convention).
- Extension points section shows the current DDL for each of the 3 tables being extended.
- No code changes anywhere.
- No new dependencies.

## Don't do

- Don't write any migrations in this task — T3 owns that.
- Don't modify any existing files.
- Don't propose schema changes — this task is a read, not a redesign. v1.3 blueprint is the architecture spec.
- Don't speculate about what code "should" do — only document what it actually does today.
