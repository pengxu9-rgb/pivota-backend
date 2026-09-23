"""One writer_audit_log row per category RE-FILE batch.

A re-file rewrites `catalog_products.category_path` on rows that already had one, because a
classification RULE changed. It deliberately does NOT touch `category_label_source`: migration 069
defines that column as the ORIGIN of the label, it is the only evidence of which lane wrote the bad
path, and `enrichment_agent_v1` additionally decides pdp_scope
(services/pdp_scope_classifier.py rule 1), so re-stamping it would demote a row to merchant_owned.

The batch is therefore what carries the provenance of a re-file, and it belongs in the audit rail
the offer writer already uses (`writer_audit_log`, migration 132) rather than in a second spelling
of it. Two batches were recorded retroactively on 2026-09-23 -- the non-face-leaf repair (#2248 /
#2253, 188 rows re-filed + 60 cleared) and the acid-pad repair (#2254, 6 rows) -- neither of which
had left any trace at all.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from services.catalog_offer_writer_guard import (
    WriterAuditAccumulator,
    write_writer_audit_log,
)

# Detail rows are capped: `reasons` is one jsonb value, and a five-figure re-file would otherwise
# put a megabyte of keys in a single row. The counts and the hash stay exact either way.
MAX_DETAIL_ROWS = 2000


def build_refile_audit(
    *,
    writer_name: str,
    batch_id: str,
    rule: str,
    moves: Iterable[Mapping[str, Any]],
    manifest_sha: Optional[str] = None,
    skipped: int = 0,
) -> WriterAuditAccumulator:
    """Accumulator for a re-file batch. `moves` is the rows that ACTUALLY landed, each
    {product_key, from, to} -- `to` None for a cleared path. Pure; does not touch the database."""
    landed = [
        {"product_key": m["product_key"], "from": m.get("from"), "to": m.get("to")}
        for m in moves
    ]
    audit = WriterAuditAccumulator(
        writer_name=writer_name, batch_id=batch_id, dry_run_report_hash=manifest_sha
    )
    audit.record_applied(len(landed))
    if skipped:
        audit.record_skips({"not_at_target": int(skipped)})
    transitions: dict = {}
    for m in landed:
        key = f"{m['from'] or 'NULL'} -> {m['to'] or 'NULL'}"
        transitions[key] = transitions.get(key, 0) + 1
    audit.reasons.update(
        {
            "rule": rule,
            "category_label_source_left_as_origin": True,
            "moves": transitions,
            "rows": landed[:MAX_DETAIL_ROWS],
            "rows_truncated": len(landed) > MAX_DETAIL_ROWS,
        }
    )
    return audit


async def record_category_refile(
    *,
    writer_name: str,
    batch_id: str,
    rule: str,
    moves: Iterable[Mapping[str, Any]],
    manifest_sha: Optional[str] = None,
    skipped: int = 0,
    actor: Optional[str] = None,
    db: Any = None,
) -> Optional[str]:
    """Write the batch. Returns the batch_id, or None when nothing landed (an empty re-file is not
    an event). Never raises: losing the audit row must not fail a repair that already committed."""
    audit = build_refile_audit(
        writer_name=writer_name, batch_id=batch_id, rule=rule, moves=moves,
        manifest_sha=manifest_sha, skipped=skipped,
    )
    if not audit.applied_rows:
        return None
    try:
        await write_writer_audit_log(audit, actor=actor, db=db)
    except Exception as exc:  # noqa: BLE001 — the rows are already written; say so and move on.
        import logging

        logging.getLogger(__name__).warning(
            "category re-file audit row failed for batch %s: %s", batch_id, str(exc)[:200]
        )
        return None
    return batch_id
