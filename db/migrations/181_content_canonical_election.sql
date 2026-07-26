-- 181_content_canonical_election.sql
--
-- ONE canonical URL per content_key, decided ONCE and stored.
--
-- THE PROBLEM. 474 content_keys in prod (measured 2026-07-25 off the live
-- /api/canonical/products feed) carry more than one sitemap-eligible,
-- renderable `pivota_signature_id` — 551 redundant URLs across groups of 2
-- (425), 3 (25), 4 (22), 5 (1) and 7 (1). The largest cohort is Path-C minted
-- canonicals (source_system='catalog_enrichment_agent_v1') sharing a
-- content_key with the external_seed MIRROR row they were minted from; P3
-- (PIVOTA-Agent#1828 + pivota-backend#1585) made both sides renderable at once.
-- Every sig in a group serves HTTP 200 with the SAME title, the SAME Product
-- JSON-LD and a SELF-referential <link rel="canonical">. Two URLs, identical
-- content, each claiming to be canonical: textbook duplicate content, and
-- Google is free to pick the one we do NOT advertise.
--
-- WHY A TABLE AND NOT A DERIVED EXPRESSION. Two independent surfaces have to
-- name the SAME winner or the fix is worse than the bug:
--
--   * pivota-agent-ui's sitemap (scripts/sitemap_lib.mjs) advertises one URL
--     per content_key, and
--   * the gateway's get_pdp_v2 `canonical` module tells every OTHER sig's PDP
--     to point its <link rel="canonical"> at that winner.
--
-- Advertise URL A while A's own page canonicals to B and you have actively
-- instructed the crawler to drop the URL you just submitted.
--
-- They cannot each compute it. pivota-agent-ui#280 made the sitemap's choice
-- STICKY by preferring the incumbent — a sig already present in the committed
-- public/sitemap-products.xml — because a pure ordering (32-hex over 24-hex,
-- then lexicographic) would have swapped 183 already-indexed URLs for
-- brand-new ones the moment P3 landed. Incumbency is real index equity and it
-- is NOT derivable from the feed: minted and mirror rows are byte-identical
-- across every field except sig_id, canonical_url (each self-referential) and
-- last_modified. So the sticky answer has to LIVE somewhere, and a git-tracked
-- XML file in a third repo cannot be what the gateway reads on a PDP request.
--
-- This table is that somewhere. The backend elects once, persists, and both
-- surfaces READ it. Stickiness stops being a property of a committed artifact
-- and becomes a stored fact.
--
-- SEEDING. The first election is seeded FROM the live sitemap (see
-- scripts/elect_content_canonicals.py --seed-from-sitemap) so no indexed URL
-- moves. That import is exact, not approximate: all 474 current duplicate
-- groups have EXACTLY ONE member in the live sitemap-products.xml — zero have
-- two, zero have none — so the incumbency answer is unambiguous for the whole
-- population on the day it is imported.
--
-- STICKINESS RULE (see services/content_canonical_election.py): a stored
-- winner is re-elected only when it stops being a candidate. Re-election on
-- any weaker condition re-opens exactly the URL churn #280 closed.
--
-- Railway production deploys skip db/migrations/, so db/schema_guard.py
-- mirrors every statement below for self-heal at boot.

CREATE TABLE IF NOT EXISTS content_canonical_election (
  -- catalog_products.content_key. The identity the PDP actually serves, and
  -- the unit of duplication — NOT product_key (per-row) and NOT
  -- product_group_id (cross-merchant, a different and WIDER canonicalization
  -- that get_pdp_v2 already applies ahead of this one).
  content_key       TEXT PRIMARY KEY,

  -- The winning catalog_products.pivota_signature_id. The one URL
  -- /products/{sig} that the sitemap advertises and that every sibling sig's
  -- PDP points its <link rel="canonical"> at. Never NULL: a row exists only
  -- once a winner is known.
  canonical_sig_id  TEXT NOT NULL,

  -- Why this sig holds the URL, for audit: 'sitemap_incumbent' (imported from
  -- the live sitemap), 'sole_candidate', 'sig_class' (32-hex over legacy
  -- 24-hex) or 'lexicographic'. Re-election is deliberately NOT a reason —
  -- it is visible as elected_at <> updated_at, which keeps this column
  -- answering one question instead of two.
  election_reason   TEXT NOT NULL,

  -- Set once, on first election. Distinguishes an imported incumbent from a
  -- winner this system chose, without parsing election_reason.
  elected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Bumped on every re-election. elected_at == updated_at means the winner has
  -- never moved, which is the state we expect for the overwhelming majority of
  -- rows and the metric to alert on if it stops being true.
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The gateway resolves winner-by-content_key on the PDP hot path (one lookup
-- per request) and the feed LEFT JOINs it per row; the PK covers both. This
-- second index answers the REVERSE question — "is this sig a winner?" — which
-- the election sweep asks for every candidate it considers.
CREATE INDEX IF NOT EXISTS idx_content_canonical_election_sig
  ON content_canonical_election (canonical_sig_id);
