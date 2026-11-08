# Crawled discount codes: the write flag, retention and opt-outs

`DEAL_CODE_CAPTURE_WRITE` makes the external-referral refresh job record the codes its shadow capture reads on a
store's own page as UNVERIFIED `merchant_published` rows in `observed_store_offers`
(`services/deal_code_capture_writer.py`). Nothing is verified, served or applied from those rows. Retention
(`services/deal_code_retention.py`) runs at the end of every refresh run and deletes them under ADR-027 Q18.

## Before the write go

1. Retention is merged AND deployed on the `external-referral-refresh` job image (seed jobs run pinned images: a
   backend deploy does not re-image the job). The writer's rows carry the `dlc_` deal_id prefix that retention keys on.
2. `DEAL_CODE_CAPTURE_SHADOW` is already on for the job. The writer acts only while both flags are on.
3. Run the job once with the write flag still off and read the `deal_code_retention` block of its summary (below).
   An empty table with no opt-outs produces no block at all.

## The opt-out notes in the summary

Retention reads the opt-out denylist (`deal_lane_optout`) once per run and reports what it could not turn into hosts.
The writer reads the same denylist. Each note counts opt-out rows, never names one.

| Count | Meaning | Writer | Retention | Operator action |
|---|---|---|---|---|
| `optout_ids_placeholder` | the opt-out names the legacy `external_seed` bucket, which is many sellers | **writes** | deletes nothing | **add a host row for the store** |
| `optout_ids_unmapped` | a merchant id with no catalog host the store owns (no rows, only marketplace URLs, or a host typed without its TLD) | **writes** | deletes nothing | **add a host row for the store** |
| `optout_ids_canonical_only` | the merchant id's hosts are known only from canonical URLs | blocks those hosts | deletes nothing | add host rows if existing rows must go now (otherwise they age out in 14 days) |
| `optout_ids_too_broad` | the merchant id spans more than 3 registrable domains | blocks every host it names | deletes nothing | add host rows for the store's own domains |
| `optout_ids_shared_host` | a host opt-out names a shared platform or registry suffix (`github.io`, `square.site`, `co.uk`, ...) | blocks only that exact host, **writes for every store under it** | deletes only that exact host | **add a host row for the store's own address** (`brand.github.io`) |

**Do not give the write go while `optout_ids_placeholder`, `optout_ids_unmapped` or `optout_ids_shared_host` is
non-zero** unless every such opt-out also has a host row. The count stays non-zero after you add the host row (it
counts the original opt-out, which still maps to nothing), so this check is manual: list `deal_lane_optout` and confirm
each such id has its store's host row next to it. These three classes cannot fail closed: there is no store host to block, so the store's codes are
still written. A host row is the store's domain as `seller_id` (`brand.example`); it covers its `www.` form and its
subdomains. An opt-out of a public suffix or a shared platform domain (`co.uk`, `myshopify.com`, `square.site`,
`github.io`, ...) covers only that exact host, never the stores under it.

Recheck the counts after any new opt-out row: the writer picks it up within 10 minutes or on the next run, and
retention deletes the store's rows at the end of that run (confirmed hosts only).

## Kill switches

- `DEAL_CODE_CAPTURE_WRITE` unset or `false` on the job: no new rows. Existing rows keep expiring under retention.
- `DEAL_CODE_RETENTION_PAUSED=true` on the job: retention does nothing (`{"paused": 1}` in the summary). Use it only to
  stop a retention fault; rows then stop expiring.
