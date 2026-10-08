"""No non-test module may print or log a credential, or any slice of one.

WHY. Cloud Run ships stdout to Cloud Logging, so a `print` is a production log
line. On 2026-10-08 main printed a prefix of the MERCHANT's PSP secret key on
every payment-session create (Checkout.com `[:20]` and `[:15]`, Adyen `[:12]`)
and on every onboarding validation (Stripe `[:15]` / `[:20]`, i.e. `sk_live_`
plus 12 characters of key material), plus a Shopify access-token prefix and a
rejected agent API key prefix. Each was written by someone who believed a
prefix was harmless. It is not: it is a partial disclosure of someone else's
credential to everyone with log-viewer access. Log `secret_fingerprint(x)`
(utils/secret_fingerprint.py) instead.

WHAT COUNTS. A call to `print`, `<anything with "log" in its name>.<level>()`,
`sys.stdout/stderr.write` or `*.echo`, any of whose arguments RENDERS a value
whose name looks like a credential: bare (`{api_key}`, `"%s" % token`,
`.format(secret)`, a positional logger arg), sliced or indexed
(`api_key[:20]`, `token[-6:]`), passed through a string method
(`api_key.strip()[:10]`), or chosen by a conditional (`x if ok else api_key`).
The name is a variable, an attribute (`self.api_key`, `settings.STRIPE_SECRET_KEY`),
a string subscript (`row["secret_key"]`) or a `.get("password")`.

WHAT DOES NOT. Anything that consumes the value without rendering it:
`len(api_key)`, `bool(token)`, `api_key is None`, `api_key.startswith("sk_live_")`,
`secret_fingerprint(api_key)`, and the test of a conditional. Names that only
LOOK like credentials (`has_secret`, `next_page_token`, `CONFIRM_TOKEN`,
`max_tokens`) are excluded by `_NOT_A_CREDENTIAL` — see the matcher tests below
for both sides of the rule.

KNOWN BLIND SPOTS (deliberate; review catches these, not this file). A slice
first assigned to a neutrally named variable (`prefix = api_key[:12]` then
`log(prefix)`) is not traced: about 25 legitimate `key_prefix = api_key[:10]`
STORAGE sites would trip a rule that flagged every credential slice. Output
through a receiver without "log" in its name (`console.print`, `audit.info`,
`warnings.warn`) and exception messages are not modelled either.

It parses the AST rather than grepping because the Adyen leak was a multi-line
`print(` the one-line grep missed. It walks the whole repo (minus tests/,
`test_*.py`, conftest.py and dot/venv dirs), because the gate runs it as
`pytest tests` from the repo root and a new top-level package must not be a
blind spot.
"""

from __future__ import annotations

import ast
import functools
import os
import pathlib
import re
import warnings

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SKIP_DIRS = {"tests", "__pycache__", "node_modules", "venv", "site-packages"}

# Matched against the name after camelCase -> snake_case and `-` -> `_`, so
# `stripeApiKey` and the `X-API-Key` header both reach it. `psp_key` is this
# repo's own name for the merchant's PSP secret (merchant_onboarding_routes).
_CREDENTIAL = re.compile(
    r"(?:^|_)(?:api_?key|secret|secret_key|client_secret|password|passwd|"
    r"token|access_token|refresh_token|private_key|webhook_secret|"
    r"psp_key|signing_key|hmac_key|encryption_key|jwt|bearer|authorization)$",
    re.IGNORECASE,
)
# Names that end like a credential but hold a flag, a counter, a pagination
# cursor or a typed confirmation phrase.
_NOT_A_CREDENTIAL = re.compile(
    r"^(?:has|is|num|n|max|min)_|(?:^|_)(?:page|continuation)_token$|(?:^|_)confirm_token$",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_LOG_LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
_STR_METHODS = {"strip", "lstrip", "rstrip", "lower", "upper", "casefold", "title", "replace", "encode", "decode"}

# (repo-relative path, enclosing function, name) -> why printing it is the
# point or it is not a credential. Scoped to the FUNCTION so a new leak of the
# same name elsewhere in the file is still caught. Every entry must still match
# a site (see the stale-entry test), so this list can only shrink.
ALLOWED = {
    ("middleware/cors.py", "parse_origins", "token"): "loop variable over ALLOWED_ORIGINS entries: an origin string, not a credential",
    ("scripts/mint_employee_jwt.py", "main", "token"): "the script's job is to print the JWT it just minted for the operator",
    ("scripts/ops/reap_local_e2e.py", "cmd_seed", "jwt_token"): "local-only rig; prints the agent-user JWT it minted so the operator can curl the local server",
    ("scripts/ops/reap_local_e2e.py", "cmd_seed", "agent_api_key"): "local-only rig; prints the agent key it seeded into a local SQLite/localhost DB",
}


def _is_credential_name(name: str | None) -> bool:
    if not name:
        return False
    name = _CAMEL_BOUNDARY.sub("_", name).replace("-", "_")
    return bool(_CREDENTIAL.search(name)) and not _NOT_A_CREDENTIAL.search(name)


def _credential_name(node: ast.AST) -> str | None:
    """The credential-like name `node` evaluates to, if any."""
    name = None
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        name = node.slice.value
    elif (
        isinstance(node, ast.Call)
        and _callee_name(node.func) in {"get", "getenv"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        name = node.args[0].value
    return name if _is_credential_name(name) else None


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _rendered_credentials(node: ast.AST | None) -> list[str]:
    """Credential names whose characters end up in the rendered text of `node`."""
    if node is None:
        return []
    direct = _credential_name(node)
    if direct:
        return [direct]
    if isinstance(node, ast.Subscript):  # api_key[:20], token[-6:], row["k"][:4]
        return _rendered_credentials(node.value)
    if isinstance(node, ast.JoinedStr):
        return [n for v in node.values for n in _rendered_credentials(v)]
    if isinstance(node, ast.FormattedValue):
        return _rendered_credentials(node.value)
    if isinstance(node, ast.BinOp):
        return _rendered_credentials(node.left) + _rendered_credentials(node.right)
    if isinstance(node, ast.IfExp):
        return _rendered_credentials(node.body) + _rendered_credentials(node.orelse)
    if isinstance(node, ast.BoolOp):
        return [n for v in node.values for n in _rendered_credentials(v)]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [n for v in node.elts for n in _rendered_credentials(v)]
    if isinstance(node, ast.Dict):
        return [n for v in node.values for n in _rendered_credentials(v)]
    if isinstance(node, ast.Starred):
        return _rendered_credentials(node.value)
    if isinstance(node, ast.Call):
        func = node.func
        args = [*node.args, *(k.value for k in node.keywords)]
        if isinstance(func, ast.Name) and func.id in {"str", "repr", "format"}:
            return [n for a in args for n in _rendered_credentials(a)]
        if isinstance(func, ast.Attribute):
            if func.attr == "format":
                return _rendered_credentials(func.value) + [n for a in args for n in _rendered_credentials(a)]
            if func.attr == "join":
                return [n for a in args for n in _rendered_credentials(a)]
            if func.attr in _STR_METHODS:
                return _rendered_credentials(func.value)
    return []


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):  # logging.getLogger(__name__).info(...)
        return _receiver_name(node.func)
    return ""


def _is_output_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _LOG_LEVELS:
        return "log" in _receiver_name(func.value).lower()
    if func.attr == "write":
        return isinstance(func.value, ast.Attribute) and func.value.attr in {"stdout", "stderr"}
    return func.attr == "echo"


def _leaks_in_source(source: str) -> list[tuple[int, str, str]]:
    """(line, innermost enclosing function or `<module>`, credential name) per leak."""
    with warnings.catch_warnings():
        # invalid escapes in unrelated strings (DeprecationWarning on 3.11, SyntaxWarning on 3.12+)
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(source)
    leaks = []

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
        if isinstance(node, ast.Call) and _is_output_call(node):
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                for name in _rendered_credentials(arg):
                    leaks.append((node.lineno, scope, name))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, "<module>")
    return leaks


def _production_python_files() -> list[pathlib.Path]:
    """Prunes skipped dirs during the walk: a dev checkout holds a .venv and nested
    worktrees with ~150k .py files that an rglob would list before filtering."""
    out = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".py") and not name.startswith("test_") and name != "conftest.py":
                out.append((pathlib.Path(dirpath) / name).relative_to(REPO_ROOT))
    return sorted(out)


@functools.lru_cache(maxsize=1)
def _all_leaks() -> tuple[tuple[str, int, str, str], ...]:
    out = []
    for rel in _production_python_files():
        for lineno, scope, name in _leaks_in_source((REPO_ROOT / rel).read_text(encoding="utf-8")):
            out.append((rel.as_posix(), lineno, scope, name))
    return tuple(out)


def test_no_production_module_prints_or_logs_a_credential() -> None:
    offenders = [
        f"{path}:{lineno} ({scope}) renders `{name}`"
        for path, lineno, scope, name in _all_leaks()
        if (path, scope, name) not in ALLOWED
    ]
    assert not offenders, (
        "These lines print or log a credential (or a slice of one). Cloud Run ships stdout to "
        "Cloud Logging; log utils.secret_fingerprint.secret_fingerprint(value) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_every_allowlist_entry_still_matches_a_site() -> None:
    seen = {(path, scope, name) for path, _, scope, name in _all_leaks()}
    stale = sorted(key for key in ALLOWED if key not in seen)
    assert not stale, f"Remove these ALLOWED entries; nothing matches them any more: {stale}"


def test_the_scan_actually_reaches_the_modules_that_leaked() -> None:
    """A walk that silently skips a directory passes vacuously."""
    scanned = {rel.as_posix() for rel in _production_python_files()}
    for expected in (
        "main.py",
        "adapters/checkout_adapter.py",
        "adapters/psp_adapter.py",
        "routes/merchant_onboarding_routes.py",
        "routes/agent_auth.py",
        "routes/mcp_e2e_test.py",  # a production router despite the name
        "scripts/shakeout/b_webhook_mirror.py",
    ):
        assert expected in scanned, expected
    assert not any(p.startswith("tests/") for p in scanned)


# --- the rule, both sides -----------------------------------------------------------------

FLAGGED = [
    'print(f"   API Key: {self.api_key[:20]}... (len={len(self.api_key)})")',
    'print(f"🔍 Validating Stripe key via HTTP: {api_key[:20]}...")',
    "print(\n    f\"merchant={m} | \"\n    f\"key_prefix={self.api_key[:12]}\"\n)",
    'logger.warning(f"Invalid API key attempted: {api_key[:10]}...")',
    'logger.info("token=%s", access_token)',
    'logger.info("token=%s" % token)',
    'logger.info("key={}".format(secret_key))',
    'logging.getLogger(__name__).error(f"{client_secret}")',
    'self.logger.debug(f"{settings.STRIPE_SECRET_KEY}")',
    'print(row["secret_key"][:6])',
    'print(creds.get("password"))',
    'print(f"{api_key.strip()[:10]}")',
    'print(f"{token[-6:]}")',
    'print(f"{x if ok else webhook_secret}")',
    'sys.stderr.write(f"{private_key}\\n")',
    'click.echo(api_key)',
    'print(f"psp={psp_key[:20]}")',
    'logger.info("k=%s", signing_key)',
    'logger.info("k=%s", self._adyen_hmac_key)',
    'print(os.getenv("STRIPE_SECRET_KEY")[:12])',
    'print(os.environ["STRIPE_SECRET_KEY"][:12])',
    'logger.info(f"{headers[\'Authorization\'][:20]}")',
    'logger.info(f"{request.headers.get(\'X-API-Key\')}")',
    'print(f"{stripeApiKey[:6]}")',
    'logger.info(f"{bearer}")',
]

NOT_FLAGGED = [
    'print(f"len={len(api_key)}")',
    'print(f"key={secret_fingerprint(api_key)}")',
    'print(f"{\'set\' if api_key else \'missing\'}")',
    'print(f"live={api_key.startswith(\'sk_live_\')}")',
    'logger.info(f"has_secret={has_secret}")',
    'logger.info("next_page_token=%s", next_page_token)',
    'print(f"--confirm {CONFIRM_TOKEN}")',
    'logger.info(f"max_tokens={max_tokens}")',
    'logger.info(f"tokens={tokens}")',
    'logger.info(f"--confirm {PROD_CONFIRM_TOKEN}")',
    'logger.info(f"cursor={continuation_token}")',
    'logger.info(f"has_api_key={hasApiKey}")',
    'headers = {"Authorization": f"Bearer {api_key}"}',  # not an output call
    'raise ValueError(f"{api_key}")',  # out of scope here: not a log line
]


@pytest.mark.parametrize("source", FLAGGED)
def test_the_matcher_flags(source: str) -> None:
    assert _leaks_in_source(source), source


@pytest.mark.parametrize("source", NOT_FLAGGED)
def test_the_matcher_does_not_flag(source: str) -> None:
    assert not _leaks_in_source(source), source
