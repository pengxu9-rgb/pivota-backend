-- B3 — the PRIMARY COMMERCE DESTINATION of a grounded AI answer.
--
-- WHAT THIS ADDS THAT THE TABLE COULD NOT SAY. citation_observations already
-- records WHO was cited (cited_host), and what that host is relative to the
-- merchant (citation_role, host_type, first_party, is_competitor). None of
-- those answer the question a merchant asks first: WHERE DID THE ANSWER SEND
-- THE BUYER? A citation list is not a destination list — most cited hosts are
-- sources (the editorial round-up the model read, the forum thread it
-- paraphrased). A shopper cannot buy from any of them.
--
--   destination_rank        the host's ZERO-BASED position in that response's
--                           citation list — the order the model itself attached
--                           its sources in. NULLABLE, because that order exists
--                           in exactly one place in the pipeline
--                           (build_authority_map's grounding-source loop; every
--                           later structure is a HOST-keyed aggregate), so a row
--                           deposited from a report written before B3 genuinely
--                           has no position. NULL says that; 0 would falsely
--                           claim "the answer's first citation".
--
--   is_primary_destination  this host was selected as THE ONE place the answer
--                           sent the buyer. NOT NULL DEFAULT FALSE: unknown is a
--                           negative here, never a null, because the count this
--                           feeds is "how many answers routed a buyer to you"
--                           and a null would silently join neither side.
--
-- THE INVARIANT THE READ SIDE DEPENDS ON: at most ONE row per response —
-- (audit_run_id, content_key, provider, query), which is the response identity
-- this table already carries — may have is_primary_destination = TRUE. It is
-- enforced in code twice (services/agent_center_bd_report_service.
-- build_authority_map picks one winner per response;
-- services/audit_evidence_builder.extract_citation_observations demotes any
-- second claim it sees in a stored report) rather than by a database
-- constraint, because a partial UNIQUE index on
-- (audit_run_id, content_key, provider, query) WHERE is_primary_destination
-- would make a duplicate claim ABORT the deposit of an entire audit's
-- citations. A best-effort telemetry write must not be able to do that.
--
-- ZERO true rows for a response is a first-class outcome, not a gap: it is the
-- "AI answered the question and gave the shopper nowhere to buy" case.
--
-- The selection rule is versioned — services/primary_destination.
-- PRIMARY_DESTINATION_VERSION — and that version is recorded on every run in
-- audit_basis (migration 208), so a rule change can never be read as merchant
-- movement by a before/after diff.
--
-- WHAT IS DELIBERATELY NOT HERE. The original spec asked the selector to also
-- use explicit buy/purchase language and the surrounding answer context. The
-- audit persists only grounding_sources and a 280-character evidence_excerpt —
-- the full answer text is never stored — so neither signal exists. They are
-- named as absent rather than approximated from the excerpt.
--
-- Additive and idempotent; safe to re-run.
--
-- Both statements are mirrored VERBATIM in db/audit_evidence.py's inline DDL
-- backstop (_DDL_STATEMENTS), which also carries them inside its
-- CREATE TABLE IF NOT EXISTS for a fresh database, and in db/schema_guard.py's
-- startup self-heal. All three must agree — migrations + schema_guard are the
-- schema truth.

ALTER TABLE citation_observations
  ADD COLUMN IF NOT EXISTS destination_rank INTEGER NULL;

ALTER TABLE citation_observations
  ADD COLUMN IF NOT EXISTS is_primary_destination BOOLEAN NOT NULL DEFAULT FALSE;

-- The B3 read is "the primary destinations of this run", and they are a small
-- minority of rows. A partial index keeps that off a full scan without paying
-- for the false rows.
CREATE INDEX IF NOT EXISTS idx_citation_observations_primary_destination
  ON citation_observations (audit_run_id, cited_host)
  WHERE is_primary_destination;

COMMENT ON COLUMN citation_observations.destination_rank IS
  'B3: zero-based position of this cited host in the response''s citation list (the model''s own ordering). NULL for rows deposited from reports written before B3 — a missing position, not position 0.';
COMMENT ON COLUMN citation_observations.is_primary_destination IS
  'B3: this host was selected as the ONE commerce destination of this response (services/primary_destination.py). At most one TRUE row per (audit_run_id, content_key, provider, query); ZERO is the real "no actionable destination" outcome. Enforced in code, not by constraint, so a duplicate claim cannot abort an audit''s whole citation deposit.';
