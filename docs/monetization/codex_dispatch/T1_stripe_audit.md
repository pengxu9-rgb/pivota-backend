# Codex prompt — T1: Stripe codebase audit (read-only)

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already wired as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.

## Task

Read the existing Stripe-related code thoroughly. Produce a single markdown document that gives any future implementation agent enough context to extend the Stripe integration into Stripe Billing (subscriptions) + Stripe Invoicing (GMV-take) without re-discovering the system.

## Files to read

```
adapters/stripe_adapter.py
routes/payment_routes.py
orchestrator/payment_orchestrator.py
services/psp_payment_finalizer.py
config/settings.py         (search for Stripe-related env vars)
PAYMENT_TESTING_COORDINATION.md   (if present in this branch)
```

Plus: any file the above imports from that is Stripe-relevant.

## Output

Write to: `docs/monetization/T1_stripe_codebase_audit.md`

The document MUST contain these sections (in this order):

1. **Current Stripe integration shape** — what calls Stripe today, in what flow, with what arguments. Be concrete: file path, function name, Stripe SDK call.
2. **Function signatures and conventions** — naming patterns, async vs sync, error handling style, dependency-injection patterns.
3. **Env vars and config** — which Stripe-related env vars are referenced, what they map to.
4. **Extension points for Stripe Billing (subscriptions)** — where `stripe.Subscription.create` + `stripe.checkout.Session.create` would slot in cleanly. Be specific about which module/function would own it.
5. **Extension points for Stripe Invoicing (GMV-take)** — where `stripe.Invoice.create` (draft first) + `stripe.InvoiceItem.create(invoice=...)` would slot in.
6. **Existing webhook handling** — is there a webhook endpoint today? What events does it handle? How is signature verification done?
7. **Gotchas** — anything surprising, brittle, or non-obvious in the current code that v1.3 implementation needs to know about.

## Acceptance criteria

- Document written to the exact path above.
- All 7 sections present and concrete (file paths + function names, not generalities).
- No code changes anywhere.
- No new dependencies.

## Don't do

- Don't modify any existing files.
- Don't propose architecture changes. This task is a read, not a redesign. v1.3 blueprint is the architecture spec.
- Don't speculate about what code "should" do — only document what it actually does today.
