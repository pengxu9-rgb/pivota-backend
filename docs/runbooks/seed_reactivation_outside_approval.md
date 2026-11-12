# Seed reactivation outside the draft-approval rule

A seed goes in front of buyers only when its `status` is `active`. Staff turn a seed active through
the employee routes. Those all run `routes.employee_products._publish_seed`, which enforces:

- only a staff role (`employee`, `admin`, `super_admin`) may publish;
- nobody publishes a seed they created or whose content they edited (the seed's authors);
- the conflict check, the supersede and the per-destination lock.

A content edit to a live seed is saved as a revision draft, and that draft needs the same approval.

**The tools below bypass that rule.** The owner decided on 2026-11-08 that they stay ops-only and are
not gated. Each logs every seed it puts (back) to `active`.

| Tool | When it sets a seed `active` | Run by |
|---|---|---|
| `scripts/withdraw_catalog_rows.py --revert` | the seed ids its withdrawal recorded | hand |
| `scripts/remediate_unpublished_crawl_rows.py --revert` | a seed whose recorded prior status was `active` | hand |
| `scripts/repair_same_title_listings.py` (revert of a run) | the seeds that run deactivated | hand |
| the lane-4 mirror-seed deactivation script (`scripts/deactivate_lane4_*_mirror_seeds.py`), `revert` | a seed whose recorded prior status was `active` | hand |
| `scripts/retire_superseded_brand_keys.py` revert | the seeds on the rows that revert restored | hand |
| `services/identity_resolution.revert_run` / `revert_proposal` (via `scripts/identity_engine.py --execute`) | the seeds the apply events recorded | hand |
| `scripts/onboard_external_brand_from_crawl.py` (re-run) | `ON CONFLICT ... status='active'` on a seed it wrote before, even one taken down since | hand |
| the catalog enrichment agent apply (`services/catalog_enrichment_agent/apply.py`) | `status = EXCLUDED.status` re-asserts a planned `active` on a seed taken down since | the ingest pipeline |

## Rules

- **The operator tools are run only by hand,** by an operator with production database access. No
  staff route, portal action or scheduler job calls them.
- **Every reactivation is logged.**
  - **What gets logged:** one line per seed, event `external_seed_reactivated_outside_approval`, written by
    `services/seed_reactivation_audit.py`.
  - **Format:** a bare JSON object on stdout with `"severity": "WARNING"`, so Cloud Logging files it as
    jsonPayload at WARNING.
  - **Fields:** `seed_id`, `tool`, `previous_status` and `row_confirmed`. No person's identity.
  - **`row_confirmed: false`** means the statement has no `RETURNING`: the seed changed only if the
    statement's own guard held.
  - **For the re-ingest upserts,** the status is read just before the write, and only an actual revival
    (a non-`active` status written back as `active`) is logged.
  - **Logging never changes what a tool does:** a failure to read or log is swallowed.
- **Revisit this if any operator tool is ever scheduled or wired to a route or job** (a scheduler
  entry, a Cloud Run job, an HTTP route). At that point it is no longer an operator's deliberate undo,
  and the reactivation must go through the publish rule (`_publish_seed`) or get its own reviewed
  exception.
- **The ingest revival is the same exposure on a schedule.** A seed someone takes down can come back
  on the next ingest of the same product. Taking it down for good therefore means removing it from
  the source the ingest reads (or suppressing its product), and the log line shows when that was
  missed.
- **A ratchet test fails on any new status write that could set a seed active and is neither logged
  nor the publish step** (`tests/test_seed_reactivation_audit_log.py`). It reads any position in the
  SET list, bound values, `EXCLUDED.status`, `ON CONFLICT` arms, aliases and schema prefixes. It
  cannot see an ORM write.

## Finding the lines

```
jsonPayload.event="external_seed_reactivated_outside_approval"
```

Add `severity>=WARNING` to narrow it, or `jsonPayload.tool="<tool>"` for one tool.
