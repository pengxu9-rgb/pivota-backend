"""utils.secret_fingerprint: log handles that carry no characters of the secret."""

from __future__ import annotations

import pytest

from utils.secret_fingerprint import redact_url_credentials, secret_fingerprint


def test_fingerprint_is_a_short_sha256_prefix_and_never_the_value() -> None:
    # Not shaped like any real provider's key: push protection rejects even fakes.
    key = "example-credential-AbCdEfGhIjKlMnOpQrStUvWx"
    fp = secret_fingerprint(key)
    assert fp == "sha256:" + __import__("hashlib").sha256(key.encode()).hexdigest()[:8]
    assert "example" not in fp and "AbCdEf" not in fp
    assert secret_fingerprint(key.encode()) == fp


@pytest.mark.parametrize("value", [None, "", b"", 0, 12345, ["sk_live_x"]])
def test_fingerprint_of_a_missing_or_non_string_value_is_absent(value) -> None:
    assert secret_fingerprint(value) == "absent"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgresql://app:hunter2@10.0.0.1:5432/pivota", "postgresql://app:***@10.0.0.1:5432/pivota"),
        ("postgresql+asyncpg://app:pw@/pivota?host=/cloudsql/p:r:i", "postgresql+asyncpg://app:***@/pivota?host=/cloudsql/p:r:i"),
        ("postgresql://app:p@ss/w?rd@10.0.0.1/db", "postgresql://app:***@10.0.0.1/db"),
        ("postgresql://app@h/db?password=hunter2&sslmode=require", "postgresql://app@h/db?password=***&sslmode=require"),
        ("postgresql://app@host/db", "postgresql://app@host/db"),
        ("sqlite+aiosqlite:///./pivota.db", "sqlite+aiosqlite:///./pivota.db"),
        (None, ""),
    ],
)
def test_redact_url_credentials(url, expected) -> None:
    out = redact_url_credentials(url)
    assert out == expected
    for secret in ("hunter2", "p@ss", "w?rd"):
        assert secret not in out
