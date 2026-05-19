# Adding a new category to the merchant agent authoring surface

The merchant agent at `/dashboard/agent-chat` covers **fashion**, **beauty
care**, and **beauty tools** today. This runbook is for shipping the
(N+1)th category — pet, electronics, food, etc. — when merchant signal
appears.

**Don't follow this preemptively.** Categories without a real merchant
in them are dead UI. Check `scripts/report_category_distribution.py`
first; the signal thresholds at the bottom of this doc tell you when
to act.

The reusable patterns are already proven (the beauty surface went
through three iterations of subcategory + schema work). Following this
runbook end-to-end should take **~half a day** — most of the time goes
into deciding the schema, not writing code.

---

## Before you start

1. Run `scripts/report_category_distribution.py --format md` against prod. Confirm the target root crosses a signal threshold (see bottom of this doc). If not, stop.
2. Decide whether the merchant will author at the **product level** (one row per `catalog_products.product_key`) or the **SKU level** (one row per `catalog_skus.sku_key`). Most categories are product-level; beauty's `raw_inci` is the exception because makeup-foundation INCI varies by shade.
3. Decide whether the fields need their own table (beauty pattern — multi-row, multi-level, rich) or fit as flat columns on `catalog_products` (fashion pattern — ≤3 fields, all product-level, all simple types). **Per-SKU-ness is the deciding factor, not field count.**

---

## Backend pieces

### 1. Schema definitions in a service module

Create `services/<root>_field_authoring.py` mirroring
`services/beauty_field_authoring.py:1-50`. The key dict is
`SUBCATEGORY_SCHEMAS`:

```python
SUBCATEGORY_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "<root>/<subcategory>/": {
        "subcategory_kind": "<short_identifier>",
        "subcategory_group": "<group_for_ui_tab>",   # e.g. "pet_apparel" or "pet_food"
        "label": "Display label",
        "fields": (
            {
                "name": "<field_name>",
                "type": "text" | "textarea" | "enum" | "enum_multi",
                "label": "Human-readable",
                "placeholder": "Example value",
                "hint": "Why this matters for shopping search",
                "allowed_values": [...],   # only for enum / enum_multi
            },
            # ... more fields ...
        ),
    },
    # ... more subcategories ...
}
```

The same dict drives the read endpoint (selects products in scope +
returns per-product schemas) AND the write endpoint (validates
posted fields). Single source of truth — keep it that way.

### 2. Write service

Copy the write functions from `services/beauty_field_authoring.py:265-380`
(the `_write_*` helpers + `write_merchant_authored_<root>_fields()`).
Each `_write_*_field()` UPSERTs one field into its storage:

- **Flat column on `catalog_products`** (fashion pattern): `UPDATE catalog_products SET <field> = :v, <field>_source = 'merchant_authored', <field>_confidence = 1.0 WHERE product_key = :pk`. Requires a migration adding the column (see Step 4). Use this for simple text fields where merchant_payload precedence matters (Shopify metafield always wins).
- **JSONB blob in a per-root profile table** (beauty pattern): `INSERT INTO <root>_product_profiles ... ON CONFLICT (product_key) DO UPDATE SET profile_payload = jsonb_set(...)`. Use this for fields that don't justify their own column or change shape per subcategory.

Wrap every write in `async with database.transaction()` + start with `SELECT ... FOR UPDATE` on the catalog_products row (race-safe vs concurrent sync — fixed in PR #564 for fashion, mirrored in PR #569 for beauty).

### 3. HTTP endpoints in `routes/merchant_products.py`

Two endpoints, modeled on the existing beauty pair (lines ~1095-1500):

```python
@router.get("/<root>_completeness")
async def get_<root>_completeness(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    subcategory_group: Optional[str] = Query(
        None, pattern="^(<group_a>|<group_b>)$",
    ),
    current_user: dict = Depends(get_current_user),
):
    # Auth: merchant role + merchant_id presence
    # SQL: filter catalog_products by category_path LIKE '<root>/%'
    # (further filtered by subcategory_group if supplied)
    # Returns: { queue, totals, allowed_<enum>, subcategory_schemas }
```

```python
@router.put("/{platform}/{platform_product_id}/<root>_fields")
async def update_product_<root>_fields(
    platform: str,
    platform_product_id: str,
    body: <Root>FieldsBody,
    current_user: dict = Depends(get_current_user),
):
    # Auth: merchant role + ownership via products_cache
    # Body: union of all subcategories' fields, all Optional
    # Returns: { status, outcomes: { <field>: 'written' | 'skipped_payload_owned' | ... }, allowed_<enum> }
    # All-not-found → HTTP 404 (mirror beauty endpoint)
```

**Don't** generalize the route into one polymorphic endpoint that takes a `?category=` parameter. The per-root endpoint pattern keeps the body schema typed + the SQL focused. Beauty + fashion both deserve their own pair; so does the new root.

### 4. Schema migration (only if you chose flat columns)

If you went with flat columns on `catalog_products`:

1. Add columns to the SQLAlchemy `catalog_products` table in `db/catalog.py:127-135`-style.
2. Add an `ALTER TABLE IF EXISTS catalog_products ADD COLUMN IF NOT EXISTS …` block to `db/schema_guard.py` (production fast-mode skips `db/migrations/`).
3. Add a migration file `db/migrations/<next_number>_<root>_fields.sql` with the same DDL — for dev parity.

If you chose a per-root profile table (beauty pattern):

1. Add the table definition to `db/catalog.py` (the 6 beauty tables at lines 433-538 are the template).
2. Add the `CREATE TABLE IF NOT EXISTS ...` to `db/schema_guard.py`.
3. Add the migration file.

### 5. Tests

Copy `tests/test_beauty_field_authoring.py` (17 cases) and `tests/test_merchant_beauty_fields_endpoint.py` (15 cases) wholesale — replace `beauty` with `<root>` and rewrite the field-specific assertions for your fields. Pin:

- subcategory dispatch (a row in subcategory A gets A's field set, not B's)
- payload-owns guard if you wired one (a `*_source = 'merchant_payload'` row is not overwritten)
- empty / whitespace / wrong-type → `unchanged`
- product_not_found → all-outcomes 404 path
- transaction + FOR UPDATE pin
- response surfaces `subcategory_schemas` (the generic UI needs it)

Aim for 25-30 cases. Less than that, you missed an edge case.

---

## Frontend pieces

The generic schema-driven editor in
`components/agent-chat/BeautyEditor.tsx` handles arbitrary field types
already. You probably don't need a new editor — just plumbing.

### 1. Types

In `types/fashion-authoring.ts`:

- Extend `CategoryTab` with `<root>_<group_a>` and (if you have multiple groups) `<root>_<group_b>` strings.
- Extend the relevant field-name unions (`<Root>FieldName`) with your new field names.
- Extend `BeautyFieldsDraft` (or add `<Root>FieldsDraft`) with the writable shape your PUT endpoint accepts.
- Add a `<Root>SubcategoryKind` discriminator union.
- Extend `IncompleteProduct` discriminated union with a `<Root>` arm carrying `category_kind: '<root>'`, `subcategory_kind`, and `field_schemas`.

### 2. API client

In `lib/api-client.ts`, add `getMerchant<Root>Completeness()` and `updateMerchantProduct<Root>Fields()` mirroring the beauty pair. Each is ~30 lines.

### 3. Store

In `lib/merchant-fashion-store.ts`:

- The store doesn't need new actions — `setCategory()` already handles arbitrary `CategoryTab` strings and clears the right state on switch.
- If your draft shape differs structurally from `BeautyFieldsDraft`, extend `FieldsDraft` to be `FashionFieldsDraft | BeautyFieldsDraft | <Root>FieldsDraft`.

### 4. Surface dispatch

In `components/agent-chat/AgentChatSurface.tsx`:

- Add a branch in `load()`'s tab dispatcher (line ~225): when `category` is your new root tab, call `getMerchantPet/ElectronicsCompleteness(...)`.
- Extend `_shapeQueue()` and `_shapeTotals()` to handle the new payload shape. If your shape mirrors beauty's, the existing beauty branch may cover it.
- The editor dispatcher inside the `case "structured"` block needs to route to the right editor by `current.category_kind`. For most categories the generic `BeautyEditor`-style schema-driven render works — just point the dispatcher at it.

### 5. Trigger tab

In `components/agent-chat/TriggerCard.tsx`:

- Add `<root>_<group>` to the `CATEGORY_LABEL` / `CATEGORY_NOUN` / `PER_CATEGORY_HEADLINE` / `PER_CATEGORY_BODY` records.
- Add it to the `["fashion", "beauty_care", "beauty_tools", ...]` tab list in the tab pill.
- Update the per-tab StatStrip population block.

### 6. StatStrip + QueueSidebar

In `components/agent-chat/StatStrip.tsx`:

- Add your fields to `LABELS` + `TINTS` + `FIELDS_BY_CATEGORY` records.

In `components/agent-chat/QueueSidebar.tsx`:

- Add your fields to `FIELD_PILL_LABELS` + add the per-tab filter chip set to `FILTER_LABELS_PER_CATEGORY`.

### 7. Build + verify

`npm run build` — should compile clean. `npx tsc --noEmit --skipLibCheck` — should be silent. The whole frontend change is ~150 lines across 5-6 files; if you're churning more, you're overbuilding.

---

## Signal thresholds — when to ship category N+1

Build a category's authoring surface when **any** of:

1. **One merchant with >20 products in a single non-covered root.** Strongest signal — that merchant is a real customer with a real catalog and zero authoring path. Stop everything and ship for them.
2. **Three or more merchants in a single non-covered root** (even with small catalogs). Breadth signal — the platform is growing in that direction.
3. **A merchant explicitly asks for it.** The most direct signal of all. Prioritize over the passive thresholds.

If `scripts/report_category_distribution.py` shows none of the above for a root, **defer**. Adding it preemptively means maintaining dead UI for an indefinite period.

---

## Likely next candidates (when signal emerges)

In rough order of expected value:

| Root | Likely schema sketch | Storage | Effort |
|---|---|---|---|
| **pet** | `species` enum + `life_stage` enum + `size` text + `materials` text + `feeding_guide` textarea (food/treats) | Flat columns on catalog_products | ~half day |
| **electronics** | `brand` + `model` + `key_specs` JSONB + `warranty_months` + `compatibility` text | `electronics_meta` JSONB column on catalog_products | ~half day, but the spec schema-design is the hard part |
| **food / supplements** | `raw_inci` (reuse beauty's pattern) + `nutrition_facts` JSONB + `allergens` enum_multi + `certifications` enum_multi | `food_product_profiles` table | **1+ day** — regulatory nuance, defer hardest |
| **home / outdoor / sports / toys / books** | Out-of-scope until a merchant appears with a real catalog | TBD | TBD |

---

## Things to NOT do when extending

- **Don't pre-build schemas for categories without merchant signal.** Each unbuilt category is fine; each built-and-unused category is a maintenance tax.
- **Don't generalize the per-category endpoints into one polymorphic endpoint** with a `?category=` parameter. The per-root pattern is the right level of polymorphism; collapsing it past that point loses field-type safety.
- **Don't extend `agent_pdp_view` to project the new fields without a parallel decision** about whether the gateway needs them on every read. Adding columns to the canonical view is a separate v2.x decision per category.
- **Don't ship per-category LLM extraction unless you have a real prompt + grounding plan.** Each new LLM extractor is an ongoing cost; merchant-authored is fine as a v1 for any new root.

---

## When to revisit

- **Quarterly**, OR when a non-fashion/non-beauty merchant onboards.
- Run `scripts/report_category_distribution.py`. Compare the per-root counts against the signal thresholds above.
- If any uncovered root has crossed a threshold, kick off its extension following this runbook. Half a day.

The plan in `~/.claude/plans/the-problem-here-is-curious-waterfall.md` (the "should we do it now" plan) is the source-of-truth strategic doc.
