"""GET /agents/{agent_id}/merchants contact_email rule on the PRODUCTION dialect.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/<fresh db> \\
        .venv/bin/python -m pytest tests/test_agent_merchant_authorizations_contact_email_postgres.py

Every case in tests/test_agent_merchant_authorizations_contact_email.py is imported here so a
case added there runs on both dialects. The restricted branch (allowed_merchants set) binds a
list to `= ANY(:merchant_ids)`, which only Postgres accepts, so its case lives here only.
This module must not import `main`.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL; see the module docstring",
)

if _IS_PG:
    from tests.test_agent_merchant_authorizations_contact_email import *  # noqa: F401,F403,E402
    from tests.test_agent_merchant_authorizations_contact_email import (  # noqa: E402
        AGENT,
        M_DELETED_ORDER,
        M_FRESH,
        M_ORDERED,
        M_OTHERS,
        M_PENDING,
        _agent_token,
        _email,
        _get,
        _ours,
        _set_allowed,
        _rows,  # noqa: F401  (fixture by name)
    )

    async def test_a_restricted_agent_sees_a_contact_email_only_where_it_has_ordered(_rows):
        allowed = [M_ORDERED, M_FRESH, M_PENDING, M_OTHERS, M_DELETED_ORDER]
        await _set_allowed(AGENT, allowed)
        resp = await _get(AGENT, _agent_token(AGENT))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_type"] == "restricted"
        ours = _ours(body)
        # Restricted lists the named merchants whatever their status.
        assert set(ours) == set(allowed)
        assert ours[M_ORDERED]["contact_email"] == _email(M_ORDERED)
        assert ours[M_PENDING]["contact_email"] == _email(M_PENDING)  # AGENT ordered there too
        # Never ordered, only another agent ordered, or only a deleted order: no address.
        for mid in (M_FRESH, M_OTHERS, M_DELETED_ORDER):
            assert ours[mid]["contact_email"] is None, mid
            assert ours[mid]["total_orders"] == 0, mid
