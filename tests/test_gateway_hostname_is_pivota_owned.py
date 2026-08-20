"""Runtime defaults for the gateway must name a Pivota-owned host, not a PaaS hostname.

Why this guard exists
---------------------
The gateway's non-MCP API surface had NO pivota.cc name at all until 2026-08-20 — every consumer
hardcoded `pivota-agent-production.up.railway.app`. Once a partner (Minds, Antom) copies a URL into
their configuration it is frozen, so a PaaS hostname in a runtime default becomes a hostname Pivota
cannot move off without a partner-coordination exercise. `gateway.pivota.cc` now serves the same
service, so the DNS record is the only thing that has to change at the Railway -> Cloud Run cutover.

Scope: RUNTIME defaults only (config/ and routes/). Tests, scripts, workflows and prose may still
name the PaaS host — they pin or describe today's deployment and cannot leak into a partner config.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Any host that belongs to a platform provider rather than to Pivota. A default naming one of these
# is unmovable once a partner has copied it.
PAAS_HOST_RE = re.compile(
    r"https?://[A-Za-z0-9._-]*\.(?:up\.railway\.app|railway\.app|run\.app|vercel\.app|fly\.dev|onrender\.com)"
)

# Directories whose contents are reachable on a live request path.
RUNTIME_DIRS = ("config", "routes", "services", "adapters", "middleware", "core", "psp", "orchestrator")

# Narrow, reviewed exceptions. Each entry must say why the PaaS host is correct THERE.
ALLOWED = {
    # Pins the OAuth *resource identifier* currently advertised in production. Changing this string
    # changes an issued-token audience, so it moves with the MCP resource migration, not with R1.
    "services/agent_center_bd_report_service.py",
}


def _runtime_files():
    for d in RUNTIME_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWED or "__pycache__" in rel:
                continue
            try:
                yield rel, path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


def test_no_paas_hostname_in_runtime_defaults():
    offenders = []
    for rel, text in _runtime_files():
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Prose in comments/docstrings is not a default; only real string values matter.
            if stripped.startswith("#"):
                continue
            for match in PAAS_HOST_RE.finditer(line):
                offenders.append(f"{rel}:{line_no}: {match.group(0)}")
    assert not offenders, (
        "Runtime default names a platform-provider hostname. Use a Pivota-owned name "
        "(e.g. https://gateway.pivota.cc) so the host survives a platform migration:\n  "
        + "\n  ".join(offenders)
    )


def test_gateway_defaults_point_at_the_pivota_owned_name():
    """The specific defaults R1 fixed, pinned by value so a revert is loud."""
    from config.settings import settings

    assert settings.pivota_agent_internal_url.startswith("https://gateway.pivota.cc")
    assert settings.outbound_warm_handoff_resolve_url.startswith("https://gateway.pivota.cc/")

    from routes.accounts_orders_api import DEFAULT_AURORA_BFF_BASE
    from routes.employee_kb_monitoring import DEFAULT_BASE_URL

    assert DEFAULT_AURORA_BFF_BASE == "https://gateway.pivota.cc"
    assert DEFAULT_BASE_URL == "https://gateway.pivota.cc"


def test_defaults_remain_env_overridable():
    """The rename must not turn a configurable default into a hardcoded value.

    Asserted against the SOURCE rather than by reloading config.settings: reloading rebinds the
    module-level `settings` singleton while every already-imported module keeps the old object,
    which silently breaks unrelated tests later in the run (it cost 14 failures in this file's
    first version).
    """
    settings_src = (REPO_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    for env_var in ("PIVOTA_AGENT_INTERNAL_URL", "OUTBOUND_WARM_HANDOFF_RESOLVE_URL", "GOOGLE_OAUTH_REDIRECT_URI"):
        assert f'os.getenv(\n        "{env_var}"' in settings_src or f'os.getenv("{env_var}"' in settings_src, (
            f"{env_var} default is no longer read through os.getenv - it is now hardcoded"
        )

    kb_src = (REPO_ROOT / "routes" / "employee_kb_monitoring.py").read_text(encoding="utf-8")
    assert "DEFAULT_BASE_URL" in kb_src and "os.getenv" in kb_src

    orders_src = (REPO_ROOT / "routes" / "accounts_orders_api.py").read_text(encoding="utf-8")
    assert 'os.getenv("AURORA_BFF_BASE", DEFAULT_AURORA_BFF_BASE)' in orders_src
