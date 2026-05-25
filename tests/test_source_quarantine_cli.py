from __future__ import annotations

from datetime import datetime, timezone

import pytest

import scripts.manage_source_quarantine as cli


class FakeDb:
    def __init__(self, _database_url):
        self.rows = [
            {
                "quarantine_id": 7,
                "match_type": "domain",
                "match_value": "example.com",
                "state": "active",
                "reason": "test",
                "expires_at": None,
                "created_by": "codex",
                "created_at": datetime(2026, 5, 25, tzinfo=timezone.utc),
                "revoked_at": None,
                "revoked_by": None,
                "metadata": None,
            }
        ]
        self.fetch_one_calls = []

    async def fetch_all(self, query, values=None):
        if "FROM catalog_source_quarantine" in query:
            return list(self.rows)
        return []

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls.append((query, values or {}))
        if "INSERT INTO catalog_source_quarantine" in query:
            return {
                **self.rows[0],
                "quarantine_id": 8,
                "match_type": values["match_type"],
                "match_value": values["match_value"],
                "reason": values["reason"],
                "created_by": values["created_by"],
            }
        if "UPDATE catalog_source_quarantine" in query:
            return {**self.rows[0], "state": "revoked", "revoked_by": values["revoked_by"]}
        if "FROM catalog_source_quarantine" in query:
            return self.rows[0]
        if "COUNT(*)::int AS count" in query:
            return {"count": 0}
        return None


def run_cli(args, capsys):
    code = cli.main(["--database-url", "postgresql://test"] + args, db_factory=FakeDb)
    captured = capsys.readouterr()
    return code, captured.out


def test_create_dry_run_does_not_require_confirm(capsys):
    code, output = run_cli(
        [
            "create",
            "--match-type",
            "domain",
            "--match-value",
            "example.com",
            "--reason",
            "test",
            "--created-by",
            "codex",
        ],
        capsys,
    )

    assert code == 0
    assert '"dry_run": true' in output
    assert '"would_create"' in output


def test_create_apply_requires_confirm():
    with pytest.raises(SystemExit, match="SOURCE_QUARANTINE_CREATE"):
        cli.main(
            [
                "--database-url",
                "postgresql://test",
                "create",
                "--match-type",
                "domain",
                "--match-value",
                "example.com",
                "--reason",
                "test",
                "--created-by",
                "codex",
                "--apply",
            ],
            db_factory=FakeDb,
        )


def test_revoke_dry_run_smoke(capsys):
    code, output = run_cli(
        [
            "revoke",
            "--quarantine-id",
            "7",
            "--revoked-by",
            "codex",
        ],
        capsys,
    )

    assert code == 0
    assert '"action": "revoke"' in output
    assert '"dry_run": true' in output


def test_list_smoke(capsys):
    code, output = run_cli(["list", "--state", "active"], capsys)

    assert code == 0
    assert '"count": 1' in output
    assert '"match_value": "example.com"' in output


def test_dry_run_proposed_smoke(capsys):
    code, output = run_cli(
        [
            "dry-run-proposed",
            "--match-type",
            "domain",
            "--match-value",
            "example.com",
        ],
        capsys,
    )

    assert code == 0
    assert '"catalog_products"' in output
    assert '"external_product_seeds"' in output
