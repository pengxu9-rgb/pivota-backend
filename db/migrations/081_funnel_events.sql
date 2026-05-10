-- PR-5: per-channel + per-stage funnel events.
--
-- The audit pipeline measures *appearance* (does the product show up
-- in grounded sources / agent answers?). It doesn't measure
-- *conversion* (did the impression lead to a click? a sale?).
-- This table is the funnel layer: every meaningful interaction with
-- a Pivota-tracked product gets logged with stage + source_channel.
--
-- Append-only. NEVER UPDATE. Aggregation queries read from this
-- table for "stage X → stage Y conversion rate per channel."
--
-- source_channel enum (extensible — future channels add new values):
--   ai_grounded_search  AI answer cited via grounded web search
--   ai_agent            autonomous shopping agent queried Pivota API
--   social_own          merchant's own TikTok/IG post
--   social_kol          third-party creator post
--   editorial           press / review site
--   seo_organic         Google/Bing organic search
--   retail              Amazon / Walmart / Sephora marketplace
--   direct              direct site visit (typed URL or bookmark)
--   unknown             couldn't infer from referrer/utm
--
-- stage enum (typical funnel; not all channels surface every stage):
--   impression          product appeared in a result/answer
--   profile_visit       agent clicked merchant brand link
--   click               agent clicked a specific product link
--   pdp_view            product detail viewed
--   add_to_cart         added to cart / wishlist
--   conversion          order placed
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS funnel_events (
  event_id        UUID PRIMARY KEY,
  merchant_id     TEXT NOT NULL,
  product_key     TEXT NULL,                 -- nullable: brand-level events with no product
  source_channel  TEXT NOT NULL,
  stage           TEXT NOT NULL,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Per-event evidence the inference engine used: utm params,
  -- agent_id, query text, etc. Helps debug "why did we infer
  -- channel=X for this event?" + supports cross-channel attribution
  -- queries later.
  attribution_jsonb JSONB NULL
);

-- Funnel rollup query: "for merchant X over window Y, what's the
-- count per (channel, stage)?" — primary read pattern.
CREATE INDEX IF NOT EXISTS idx_funnel_events_rollup
    ON funnel_events (merchant_id, source_channel, stage, occurred_at);

-- Time-window scan independent of channel: "show all funnel events
-- for merchant X in the last N days" — used for the audit / trend
-- side panel.
CREATE INDEX IF NOT EXISTS idx_funnel_events_merchant_time
    ON funnel_events (merchant_id, occurred_at DESC);

-- Per-product analytics: "which products get the most funnel
-- traffic?" — feeds the per-product detail dashboards.
CREATE INDEX IF NOT EXISTS idx_funnel_events_product
    ON funnel_events (merchant_id, product_key, occurred_at DESC)
    WHERE product_key IS NOT NULL;
