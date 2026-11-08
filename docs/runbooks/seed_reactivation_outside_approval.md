# Seed reactivation outside the draft-approval rule

A seed goes in front of buyers only when its `status` is `active`. Staff turn a seed active through
the employee routes. Those all run `routes.employee_products._publish_seed`, which enforces:

- only a staff role (`employee`, `admin`, `super_admin`) may publish;
- nobody publishes a seed they created;
- the conflict check, the supersede and the per-destination lock.

**These operator tools bypass that rule.** Each one sets seeds back to `active` while undoing its
own earlier run. It reactivates only the seeds that same run deactivated.

| Tool | When it reactivates |
|---|---|
| `scripts/withdraw_catalog_rows.py --revert` | the seed ids its withdrawal recorded |
| `scripts/remediate_unpublished_crawl_rows.py --revert` | a seed whose recorded prior status was `active` |
| `scripts/repair_same_title_listings.py` (revert of a run) | the seeds that run deactivated |
| `services/identity_resolution.revert_run` / `revert_proposal` | the seeds the apply events recorded |

The owner decided on 2026-11-08 that these stay ops-only and are not gated.

- **Run them only by hand,** by an operator with production database access. No staff route,
  portal action or scheduler job calls them.
- **Every reactivation is logged.** Each tool writes one WARNING line per seed, through the `pivota`
  logger (`services/seed_reactivation_audit.py`), with event `external_seed_reactivated_outside_approval`.
  The line carries `seed_id`, `tool`, `previous_status` and `row_confirmed`, and no person's identity.
  - `row_confirmed: false` means the tool's UPDATE has no `RETURNING`. The seed changed only if it
    still had `previous_status`.
  - The line never changes what the tool does: a logging failure is swallowed.
- **Revisit this if any of them is ever scheduled or wired to a route or job** (a scheduler entry, a
  Cloud Run job, an HTTP route). At that point it is no longer an operator's deliberate undo, and the
  reactivation must go through the publish rule (`_publish_seed`) or get its own reviewed exception.

To find these lines in Cloud Logging, search for `external_seed_reactivated_outside_approval`.
