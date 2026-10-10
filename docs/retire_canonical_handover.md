# Retire ↔ election stalemate: the retire hands the canonical URL over

## What is stuck (measured on prod 2026-10-10, read-only)

The drain job `rij_2d07af023f834f7a8bc2732f8740e9eb` applied run `rir_61d5fcf5e49c455b9bf15c1062611b1e` on
image 908d9a93a. Its stale-brand retire returned `retired 1 (retire_c360a327e9a2), new_not_serving 8,
waiting_for_new_key 13, already_suppressed 26`.

Each of the 8 gift-set content_keys (The Cheeky Duo, SOS Spray Duo, SOS 3-Step Skincare Set, Milky Lip Set,
MakeWaves® Mascara Set, LipSoftie® Lip Treatment Set, LipSoftie® Deluxe Gift Set, GetSet® Powder Puff Duo)
has the same three rows:

| row | brand / merchant | state | trust |
|---|---|---|---|
| OLD `ext:tower-28-beauty-…` | Tower 28 Beauty / `merch_obs_f11e…` | live, **elected** (`dedupe_keeper`, 2026-07-27 02:59) | public |
| NEW `ext:tower-28-…` | Tower 28 / `merch_obs_18d1…` | live, same host, written 10-09 14:38 | shadow `NON_CANONICAL_DUPLICATE` |
| T `prod::external_seed::…tower-28-beauty:…` | Tower 28 Beauty | `step5_same_merchant_same_url_dup` tombstone (07-10) with `keeper_product_key` → OLD | blocked |

Two more facts from the same census:
- The real trust policy (`derive_trust`), run on NEW's own joined row with `row_is_elected_canonical` forced
  to true, returns `public` for all 8. The election is the only thing keeping NEW out of search.
- Catalog-wide, of 1,443 content_keys that have an election and at least 2 live rows, exactly these 8 have
  this shape: the elected row has a same-host sibling under another spelling of the brand, and the elected
  row's key is the sibling's stale key (`derive_product_key(elected.brand, sibling.title)`).

A ninth Tower 28 key (`ck_c7bc…`, "Mini MakeWaves Mascara in Jet") also shows `NON_CANONICAL_DUPLICATE`, but
it is a different case. Its old row was already retired on 09-28. Its election (lexicographic) holds a
different live Tower 28 row, "MakeWaves® Mini Mascara", which shares the content_key. Nothing is waiting on
it, and this change does not touch it.

## Why it is a stalemate, and a correction to the framing

The retire keeps OLD while NEW is not searchable (`select_retirable` → `new_not_serving`). NEW is not
searchable because OLD holds the election. OLD holds the election because `pick_winner` ranks the step-5
keeper above stickiness, and T's keeper pointer names OLD.

**The keeper rank is not the root cause.** Without it, stickiness alone would keep OLD: the stored winner is
re-elected only when it stops being a candidate, and OLD is a live candidate. Any brand-spelling retire whose
old row holds the URL stalls the same way, whatever the `election_reason`. So the fix must not depend on the
election reason.

## Options

**(b) The election hands the keeper rank to a superseding row.** Rejected.
- The election would have to recognise a "same-host, same-title, new-spelling" pair. That restates the
  retire's cohort rule (`cohort_from_records`, which is re-derived and never passed in) in a second place
  that can drift from it.
- It would move a live URL before the retire has decided OLD goes. If the retire then refuses (foreign
  source, not serving, lock busy), the URL has moved for nothing, and T's keeper (OLD) now disagrees with the
  election. That is the two-hop canonical chain the keeper rung exists to prevent.
- The retire's `revert` could not undo it.

**(c) The retire counts NEW as searchable when it is non-public only because of `NON_CANONICAL_DUPLICATE`.**
Rejected on its own.
- The retire would tombstone OLD while NEW is still shadow. The product then drops out of public search until
  the next 6-hourly election moves the URL AND the next trust refresh reaches NEW: hours, on every run.
- The drain's own read-back (`searchable before, new key not searchable`) would flag every such retire as
  `readback_failed`.
- Reading reason codes would also restate the trust policy instead of asking it.

**(a) The retire hands the canonical over, in its own transaction.** Chosen. Its acceptance test is a
narrowed, policy-backed form of (c).

## The design

`plan_for_cohort` moves a pair from `new_not_serving` to `live` with a `handover` only when ALL of these hold
(`select_handovers`, pure):

1. The serving check passes for the pair, so search is the only surface it fails on.
2. OLD and NEW share one content_key, and both carry a `sig_` signature.
3. The content_key's stored election names OLD's signature. The retire is tombstoning the very row that
   holds the URL, so that URL moves anyway: the election's own stickiness rule re-elects once the stored
   winner stops being a candidate.
4. The real `pick_winner` agrees that the URL goes to NEW. It runs on the post-retire state: the real
   candidate set (`candidates_query`, limited to this content_key) minus OLD, `stored` = OLD, and
   `keeper` = the keeper `KEEPER_SIGS_SQL` will compute once OLD is a tombstone naming NEW. That keeper is
   the lowest signature among the live keepers that are left: every current keeper except OLD, plus NEW.
   Refusals:
   - NEW is not a candidate;
   - another live keeper that sorts first competes;
   - any other outcome that is not NEW.
5. The real trust policy says NEW goes public once elected. That is `derive_trust` on NEW's joined row from
   the upserter's own SQL, with `row_is_elected_canonical=True`. NEW's lifecycle must also be in the search
   list (NULL, validated, published). The trust reason codes are never read.

`write_retire` adds two statements to the existing transaction for each handover. Both are guarded, and both
are checked with RETURNING, because `databases` returns no rowcount on asyncpg:
- OLD's tombstone metadata gains `keeper_product_key = NEW`. The row layer then names the successor, and the
  next sweep's keeper rung computes NEW by itself. PIVOTA-Agent#1833's tombstone→keeper canonical points
  OLD's still-200 page at NEW, so the old URL's equity consolidates instead of splitting.
- `content_canonical_election` is set from OLD's signature to NEW's, `WHERE canonical_sig_id = OLD`.
  `elected_at` is untouched (the sweep's convention), and the reason is `pick_winner`'s.

If either guard misses (for example, the election moved after the plan), the whole retire rolls back, the
outcome is `error`, and nothing is retired. After the commit, `refresh_trust` recomputes trust for the
retired keys AND the handed-over new keys. A handed-over key that does not read `public` is a
`trust_problem`, and the drain fails the job on it.

The manifest records each handover's before-state: content_key, stale and new keys, both signatures, and the
prior `election_reason`. `revert_manifest` puts the election back to OLD only where:
- OLD was actually restored (never point the canonical at a tombstone), AND
- the election still names NEW (a later move is left alone and reported).

Restoring OLD's prior metadata drops the keeper pointer. The keeper rung then names OLD again through T, so
the next sweep agrees with the revert and writes nothing. Trust is refreshed for the restored keys and the
handed-back new keys. The new keys go back to shadow.

## Why this does not reopen the URL churn that stickiness prevents

- The election module is unchanged. No ranking changes, and no content_key moves except one whose holder the
  retire is tombstoning in the same transaction.
- That URL would move at the next sweep anyway, because a tombstone is not a candidate. The handover only
  removes the gap and chooses the successor with the election's own `pick_winner`.
- After the handover, the next sweep plans nothing: `pick_winner` returns `dedupe_keeper` = stored. The
  Postgres gate runs the real `KEEPER_SIGS_SQL` and `plan_elections` on the post-retire state to prove it.
  It proves the same after the revert.
- OLD's page keeps answering 200 (`brand_attribution_key_supersede` is not a terminal 410 reason), with
  rel=canonical pointing at NEW.

## Rollout

- **Backend web/CLI:** deploys on merge.
- **Drain:** the `retailer-ingest-drain` Cloud Run job runs a pinned image (still 908d9a93a as of 10-09), so
  it needs a re-image to at least this merge. That is the drain owner's / Peng's call (single writer).
- **Tower 28's 8 sets:** need a prod write. First a dry-run plan with the CLI (one-off job). Then, with
  Peng's explicit go, `--apply` with `--stale-brand "Tower 28 Beauty"`, which performs the handover and the
  retire in one transaction. Alternatively, wait for a drain re-run of tower28beauty.com on the re-imaged
  drain.
