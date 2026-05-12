"""P5.3+ — verifier package.

Importing this module triggers registration of all verifiers
in the verification_run_worker via each submodule's
register_verifier(verifier_id, run_verifier) call at import time.

services/audit_scheduler.py imports this package as a side-effect
so the registry is populated by the time the worker tick fires.

Verifier modules:
  - pdp_renders          (P5.3) — HTTP GET the canonical PDP page
  - pdp_in_sitemap       (P5.3) — HTTP GET sitemap-products.xml + check
  - pivota_internal_retrieval (P5.3) — backend /products/{sig} round-trip
  - gsc_url_submitted    (P5.4) — read gsc_url_submissions table
  - gsc_indexing_status  (P5.4) — call Indexing API
  - frontend_agent_cite  (P5.5) — agent-style discovery probe
  - public_llm_citation_movement (P5.6) — 30-day-delayed re-probe

Each module is self-contained: when its register_verifier call
runs, the worker picks it up on the next tick.
"""

# Import side-effects: each module registers itself.
from services.verifiers import (  # noqa: F401
    pdp_renders,
    pdp_in_sitemap,
    pivota_internal_retrieval,
    gsc_url_submitted,
    gsc_indexing_status,
    frontend_agent_cite,
)
