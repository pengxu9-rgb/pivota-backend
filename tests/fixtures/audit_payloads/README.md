# Recorded audit-payload fixtures (W7 verification harness)

Merchant-facing audit payloads recorded from real (or real-shaped) runs, used
by report-wide invariant tests (`tests/test_audit_rendered_copy_invariant.py`
and successors). The goal: every payload class that ever leaked machine text,
contradicted itself, or mis-stated a number becomes a permanent fixture here,
so the whole class stays dead.

Conventions:
- One JSON file per incident/scenario, named `<incident>_<yyyy_mm_dd>.json`.
- Trim to the subtree that matters plus enough envelope to exercise
  `resolve_merchant_identity` — these are regression probes, not full dumps.
- NEVER include merchant PII or API keys; these files are in the repo.

Fixtures:
- `rahua_json_leak_2026_07_03.json` — the "WHAT THE CATEGORY WINNER DOES
  RIGHT" panel rendered a raw ```json probe envelope (unterminated fence) in
  `competitor_intel.known_for` on the DamDam Shiso shampoo run (Jul 3 8:59 PM).
  Fixed at the parse site in PR #1145; the rendered-copy invariant keeps the
  whole class alarmed.
