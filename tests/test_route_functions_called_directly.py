"""No Python code calls a function whose defaults are FastAPI parameters and leaves one of them out.

A route function's `Query(...)` / `Depends(...)` default is resolved by FastAPI only for an HTTP request.
Called as a plain function, an argument left out is the parameter OBJECT itself, which is truthy and
reads as text. That is how the SDK search, the beauty search and the /products/resolve fallback, which
called `routes.agent_api.agent_search_products` and left `external_seed_only` out, searched external seeds
only from 2026-02-26 (`bool(Query(default=False))` is True), and how their payment context became the
text of a Query object.

The rule, over every module outside tests/: for every top-level function with a FastAPI parameter
default, every call to it that resolves to it (same module, `from M import f [as g]`, or `M.f` after
`import M [as A]`) passes each such parameter by keyword or position. A call that splats `**kwargs`
cannot be checked here; each one is named below with why it is safe, and the list must match exactly.
"""
from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
FASTAPI_PARAMS = {"Query", "Body", "Path", "Header", "Cookie", "Depends", "Form", "File", "Security"}
#: Not scanned: virtualenvs and caches, and test code (tests/ fakes call routes on purpose).
SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "tests", "__pycache__"}

#: Calls that splat `**kwargs` into such a function, each with the reason it is safe. Exact: a new one fails.
SPLAT_CALLS_REVIEWED = {
    ("routes/agent_api.py", "create_new_order"): (
        "routes.order_routes.create_new_order: the agent create-order path passes order_request and "
        "background_tasks positionally and the three precomputed_* values through **create_new_order_kwargs; "
        "create_new_order turns any non-value back into None at its top (`if not isinstance(..., tuple)` and "
        "the two checks after it), and current_user is not read in its body."
    ),
}
SPLAT_CALL_COUNT = 2


def _is_fastapi_param(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id in FASTAPI_PARAMS) or (isinstance(f, ast.Attribute) and f.attr in FASTAPI_PARAMS)


def _modules(root: Path) -> Dict[str, Tuple[str, ast.Module]]:
    out: Dict[str, Tuple[str, ast.Module]] = {}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        name = ".".join(rel.with_suffix("").parts)
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        out[name] = (rel.as_posix(), tree)
    return out


def _fastapi_defaulted(fn: ast.AST) -> Tuple[List[str], List[str]]:
    a = fn.args  # type: ignore[attr-defined]
    pos = a.posonlyargs + a.args
    off = len(pos) - len(a.defaults)
    params = [pos[i].arg for i in range(off, len(pos)) if _is_fastapi_param(a.defaults[i - off])]
    params += [k.arg for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None and _is_fastapi_param(d)]
    return params, [p.arg for p in pos]


def _scan():
    return _scan_root(ROOT)


@functools.lru_cache(maxsize=4)
def _scan_root(root: Path):
    modules = _modules(root)
    targets = {}
    for mod, (rel, tree) in modules.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params, positional = _fastapi_defaulted(node)
                if params:
                    targets[(mod, node.name)] = (rel, node.lineno, params, positional)
    missing: List[Tuple[str, int, str, List[str]]] = []
    splats: List[Tuple[str, int, str]] = []
    for mod, (rel, tree) in modules.items():
        names: Dict[str, Tuple[str, str]] = {}
        aliases: Dict[str, str] = {}
        package = mod.rsplit(".", 1)[0] if "." in mod else ""
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names[node.name] = (mod, node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join([p for p in parts + ([node.module] if node.module else []) if p])
                for alias in node.names:
                    names[alias.asname or alias.name] = (base, alias.name)
                    aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            key = None
            if isinstance(node.func, ast.Name):
                key = names.get(node.func.id)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id in aliases:
                    key = (aliases[node.func.value.id], node.func.attr)
            if key not in targets:
                continue
            if any(k.arg is None for k in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args):
                splats.append((rel, node.lineno, key[1]))
                continue
            _, _, params, positional = targets[key]
            given: Set[str] = {k.arg for k in node.keywords if k.arg} | set(positional[: len(node.args)])
            left_out = [p for p in params if p not in given]
            if left_out:
                missing.append((rel, node.lineno, key[1], left_out))
    return targets, missing, splats


def test_no_direct_call_leaves_a_fastapi_default_in() -> None:
    targets, missing, _ = _scan()
    assert len(targets) > 100, "the scan found the route functions"
    assert missing == [], "pass every FastAPI-defaulted parameter explicitly, or make the callee a plain function:\n" + "\n".join(
        f"  {rel}:{line} {name} leaves out {params}" for rel, line, name, params in missing
    )


def test_every_splat_call_into_such_a_function_is_reviewed() -> None:
    _, _, splats = _scan()
    assert {(rel, name) for rel, _, name in splats} == set(SPLAT_CALLS_REVIEWED), splats
    assert len(splats) == SPLAT_CALL_COUNT, splats


def test_the_search_function_the_routes_call_has_no_fastapi_defaults() -> None:
    targets, _, _ = _scan()
    assert ("routes.agent_api", "agent_search_products") not in targets


def test_the_scan_catches_a_call_that_leaves_a_query_default_out(tmp_path, monkeypatch) -> None:
    # The rule's own mutant: a route function called without one of its Query parameters.
    pkg = tmp_path / "routes"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "door.py").write_text(
        "from fastapi import Query\n"
        "async def search(q: str = Query(default=''), only_seeds: bool = Query(default=False)):\n"
        "    return only_seeds\n"
        "async def caller():\n"
        "    return await search(q='x')\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    _, missing, _ = _scan()
    assert missing == [("routes/door.py", 5, "search", ["only_seeds"])]
