from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_BACKEND_URLS = (
    "web-production-fedb.up.railway.app",
    "pivota-backend-production.up.railway.app",
)

ALLOWED_FILES = {
    "config/settings.py",
    "scripts/smoke_pdp_governance_production.py",
    "tests/test_public_api_base_url.py",
    "tests/test_legacy_backend_url_guard.py",
    # A post-cutover SECURITY AUDIT that reports the legacy host as a finding — it names the
    # URL in order to say it must not be used, and even documents this guard. The guard exists
    # to stop the old backend reappearing in runtime config, not to stop us writing about it.
    # Added because it turned main red: the audit landed in #1826 without an entry here.
    "docs/security/POST_CUTOVER_AUDIT_2026-08-22.md",
}

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
}


def _iter_text_files():
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if not path.is_file():
            continue
        if rel in ALLOWED_FILES:
            continue
        try:
            yield rel, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_old_web_backend_urls_do_not_reappear_outside_guardrails():
    findings = []
    for rel, text in _iter_text_files():
        for legacy in LEGACY_BACKEND_URLS:
            if legacy in text:
                findings.append(f"{rel}: {legacy}")

    assert findings == []
