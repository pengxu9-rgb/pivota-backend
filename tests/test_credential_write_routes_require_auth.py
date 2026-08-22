"""Routes that write credentials or run DDL must be guarded by a real dependency.

Two live routes were reachable unauthenticated from the public internet:

  POST /merchant/onboarding/setup-psp   wrote merchant_psps rows (api_key,
      secret_key, account_id) for a CALLER-SUPPLIED merchant_id. Its dependency
      was a no-op callable returning None, so `current_user` was None on every
      request and the role check below it was dead code.

  POST /setup/create-all-indexes        ran 12 CREATE INDEX statements. Its user
      parameter had no dependency attached, so FastAPI bound it as a request
      BODY field - the caller supplied it, and any truthy value passed.

  POST /setup/create-usage-logs-table   ran CREATE TABLE with no check at all.

Neither a role comparison nor an `if not user` test is a guard when the value it
reads is supplied by the caller, or is always None. The guard has to be a
dependency, because a dependency resolves BEFORE the handler and cannot be
satisfied by request content.

This test asserts the shape rather than the behaviour on purpose: it reads the
AST, so it cannot be satisfied by a handler that merely looks correct, and it
does not need a database, a JWT, or a running app to fail loudly in CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# route module -> {handler name -> acceptable auth dependencies}
GUARDED_HANDLERS = {
    "routes/employee_store_psp_fixes.py": {
        "setup_merchant_psp": {"get_current_user", "require_admin", "require_admin_or_key"},
    },
    "routes/quick_index_setup.py": {
        "create_all_indexes": {"require_admin", "require_admin_or_key"},
        "create_usage_logs_table": {"require_admin", "require_admin_or_key"},
    },
}


def _auth_dependencies(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    """Names passed to Depends(...) in this handler's own signature."""
    found: set[str] = set()
    for default in list(fn.args.defaults) + list(fn.args.kw_defaults):
        if not isinstance(default, ast.Call):
            continue
        func = default.func
        if not (isinstance(func, ast.Name) and func.id == "Depends"):
            continue
        if not default.args:
            continue
        dep = default.args[0]
        if isinstance(dep, ast.Name):
            found.add(dep.id)
        elif isinstance(dep, ast.Lambda):
            # A lambda dependency is what made setup-psp unauthenticated.
            found.add("<lambda>")
    return found


def _find(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


@pytest.mark.parametrize(
    "rel_path,handler,allowed",
    [
        (rel, handler, allowed)
        for rel, handlers in GUARDED_HANDLERS.items()
        for handler, allowed in handlers.items()
    ],
)
def test_handler_is_guarded_by_a_real_dependency(rel_path: str, handler: str, allowed: set[str]) -> None:
    path = REPO_ROOT / rel_path
    assert path.is_file(), f"{rel_path} has moved - update this test rather than deleting it"

    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = _find(tree, handler)
    assert fn is not None, (
        f"{rel_path}: handler {handler}() not found. If it was renamed or removed, update this "
        f"test deliberately - it exists because this route was once open to the internet."
    )

    deps = _auth_dependencies(fn)

    assert "<lambda>" not in deps, (
        f"{rel_path}:{fn.lineno} {handler}() depends on a lambda. A lambda returning None makes the "
        f"parameter None on every request, so any role check below it is dead code. This is exactly "
        f"how POST /merchant/onboarding/setup-psp shipped unauthenticated while appearing guarded."
    )

    assert deps & allowed, (
        f"{rel_path}:{fn.lineno} {handler}() has no authentication dependency "
        f"(found {sorted(deps) or 'none'}; expected one of {sorted(allowed)}).\n"
        f"A parameter with a plain default is bound by FastAPI as a REQUEST BODY FIELD - the caller "
        f"supplies it, so `if not current_user` is not a guard."
    )


def test_no_route_handler_uses_a_lambda_as_its_auth_dependency() -> None:
    """Repo-wide: catch the next handler that tries this, not just the two we fixed."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "routes").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for default in list(node.args.defaults) + list(node.args.kw_defaults):
                if (
                    isinstance(default, ast.Call)
                    and isinstance(default.func, ast.Name)
                    and default.func.id == "Depends"
                    and default.args
                    and isinstance(default.args[0], ast.Lambda)
                ):
                    arg_names = [a.arg for a in node.args.args]
                    # Only auth-shaped parameters matter. order_routes.py legitimately uses
                    # lambda dependencies for `precomputed_*` values that internal callers
                    # override; those are not guards and are not credentials.
                    idx = len(node.args.args) - len(node.args.defaults)
                    for offset, d in enumerate(node.args.defaults):
                        if d is default and idx + offset < len(arg_names):
                            pname = arg_names[idx + offset]
                            if any(k in pname for k in ("user", "admin", "auth", "principal", "caller")):
                                offenders.append(
                                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name}({pname}=Depends(lambda...))"
                                )
    assert not offenders, (
        "auth-shaped parameter bound to a lambda dependency - it will be None on every request, "
        "making any check that reads it dead code:\n  " + "\n  ".join(offenders)
    )
