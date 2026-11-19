"""No Python code calls a function whose defaults are FastAPI parameters and leaves one of them out.

A route function's `Query(...)` / `Depends(...)` default is resolved by FastAPI only for an HTTP request.
Called as a plain function, an argument left out is the parameter OBJECT itself, which is truthy and
reads as text. That is how the SDK search, the beauty search and the /products/resolve fallback, which
called `routes.agent_api.agent_search_products` and left `external_seed_only` out, searched external seeds
only from 2026-02-26 (`bool(Query(default=False))` is True), and how their payment context became the
text of a Query object.

The rule, over every module outside tests/. A TARGET is a function (top level, a class method, or a def
nested in another function) with a FastAPI parameter default. Every reference to a target must be one of:
  * a call that passes each such parameter by keyword or position, reached as `f(...)`, through an alias
    (`from M import f as g`, `g = f`), an attribute chain (`M.f`, `pkg.mod.f`, `self.f` / `cls.f` / `Cls.f`
    inside its class), `getattr(M, "f")(...)`, or `functools.partial(f, ...)` (the partial must pass them);
  * a hand-off to FastAPI, which resolves the defaults itself: an argument of `Depends(...)` / `Security(...)`,
    a decorator, an endpoint given to `add_api_route` / `api_route` / `add_api_websocket_route` /
    `add_route`, or a `dependency_overrides` key;
  * a dependency FACTORY's `return inner` (a nested target returned by the function that defines it): then
    every call of the factory must itself be handed to FastAPI (`Depends(factory(...))`);
  * `inspect.signature(f)`, which reads the function without calling it.
Anything else (the function stored, passed or returned as a value) could be called later with the defaults
left in, and fails here. A call that splats `**kwargs` cannot be checked; each one is named below with why it
is safe, and the list must match exactly.
"""
from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
FASTAPI_PARAMS = {"Query", "Body", "Path", "Header", "Cookie", "Depends", "Form", "File", "Security"}
HANDOFF_CALLS = {"Depends", "Security"}
#: Calls that read a function without calling it.
INTROSPECTION_CALLS = {"signature"}
ROUTE_REGISTRARS = {"add_api_route", "api_route", "add_api_websocket_route", "add_route", "add_websocket_route"}
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

Key = Tuple[str, str]  # (owner: module, module.Class or module.<function>.<nested>, function name)


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


def _dotted(node: ast.AST) -> Optional[str]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Scanner:
    def __init__(self, root: Path):
        self.modules = _modules(root)
        self.targets: Dict[Key, Tuple[str, int, List[str], List[str]]] = {}
        self._factory_candidates: Dict[Key, Set[str]] = {}
        for mod, (rel, tree) in self.modules.items():
            self._collect(tree.body, mod, rel)
        # A factory: a function that returns a nested target it defines (a dependency factory).
        self.factories: Set[Key] = set()
        for (owner, name) in list(self.targets):
            if ".<" in owner:
                parent_owner, _, parent = owner.rpartition(".<")
                self._factory_candidates.setdefault((parent_owner, parent.rstrip(">")), set()).add(name)
        for mod, (rel, tree) in self.modules.items():
            self._find_factories(tree.body, mod)
        self.missing: List[Tuple[str, int, str, List[str]]] = []
        self.splats: List[Tuple[str, int, str]] = []
        self.values: List[Tuple[str, int, str]] = []
        for mod, (rel, tree) in self.modules.items():
            self._check_module(mod, rel, tree)

    def _collect(self, body: List[ast.stmt], owner: str, rel: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params, positional = _fastapi_defaulted(node)
                if params:
                    self.targets[(owner, node.name)] = (rel, node.lineno, params, positional)
                self._collect(node.body, f"{owner}.<{node.name}>", rel)
            elif isinstance(node, ast.ClassDef):
                self._collect(node.body, f"{owner}.{node.name}", rel)

    def _find_factories(self, body: List[ast.stmt], owner: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = self._factory_candidates.get((owner, node.name), set())
                for ret in ast.walk(node):
                    if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Name) and ret.value.id in inner:
                        self.factories.add((owner, node.name))
                self._find_factories(node.body, f"{owner}.<{node.name}>")
            elif isinstance(node, ast.ClassDef):
                self._find_factories(node.body, f"{owner}.{node.name}")

    # -- name resolution -------------------------------------------------------------------------------

    def _imports(self, mod: str, tree: ast.Module) -> Tuple[Dict[str, Key], Dict[str, str]]:
        names: Dict[str, Key] = {}
        aliases: Dict[str, str] = {}
        package = mod.rsplit(".", 1)[0] if "." in mod else ""
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    if node.level > 1:
                        parts = parts[: len(parts) - (node.level - 1)]
                    base = ".".join([p for p in parts + ([node.module] if node.module else []) if p])
                for alias in node.names:
                    names[alias.asname or alias.name] = (base, alias.name)
                    aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        aliases[alias.asname] = alias.name
                    else:
                        aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]
        return names, aliases

    def _key_for_dotted(self, dotted: str, aliases: Dict[str, str]) -> Optional[Key]:
        head, _, rest = dotted.partition(".")
        if not rest or head not in aliases:
            return None
        full = f"{aliases[head]}.{rest}"
        owner, _, name = full.rpartition(".")
        return (owner, name)

    def _resolve(self, node: ast.AST, scope: Dict[str, Key], aliases: Dict[str, str], cls: Optional[str]) -> Optional[Key]:
        if isinstance(node, ast.Name):
            return scope.get(node.id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"} and cls:
                return (cls, node.attr)
            dotted = _dotted(node)
            if dotted:
                key = self._key_for_dotted(dotted, aliases)
                if key in self.targets:
                    return key
                # A class in scope: `Cls.method`.
                head, _, attr = dotted.rpartition(".")
                if head in scope and "." not in attr:
                    owner_key = scope[head]
                    return (f"{owner_key[0]}.{owner_key[1]}", attr)
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
            obj, attr = node.args[0], node.args[1]
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                dotted = _dotted(obj)
                if dotted:
                    if dotted in aliases:
                        owner, _, name = f"{aliases[dotted]}.{attr.value}".rpartition(".")
                        return (owner, name)
                    key = self._key_for_dotted(f"{dotted}.{attr.value}", aliases)
                    if key:
                        return key
        return None

    # -- checks ----------------------------------------------------------------------------------------

    def _check_call(self, rel: str, node: ast.Call, key: Key, extra_args: int = 0, keywords: Optional[List[ast.keyword]] = None) -> None:
        kws = node.keywords if keywords is None else keywords
        args = node.args[extra_args:]
        if any(k.arg is None for k in kws) or any(isinstance(a, ast.Starred) for a in args):
            self.splats.append((rel, node.lineno, key[1]))
            return
        _, _, params, positional = self.targets[key]
        if positional and positional[0] in {"self", "cls"}:
            positional = positional[1:]
        given: Set[str] = {k.arg for k in kws if k.arg} | set(positional[: len(args)])
        left_out = [p for p in params if p not in given]
        if left_out:
            self.missing.append((rel, node.lineno, key[1], left_out))

    def _check_module(self, mod: str, rel: str, tree: ast.Module) -> None:
        names, aliases = self._imports(mod, tree)
        scope: Dict[str, Key] = dict(names)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope[node.name] = (mod, node.name)
        self._walk_body(tree.body, rel, scope, aliases, owner=mod, cls=None)

    def _walk_body(self, body, rel, scope, aliases, owner, cls) -> None:
        scope = dict(scope)
        aliases = dict(aliases)
        if ".<" in owner:
            # Imports inside a function bind in that function, over the module's names.
            for node in self._own_nodes(body):
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    for alias in node.names:
                        scope[alias.asname or alias.name] = (node.module, alias.name)
                        aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        bound = alias.asname or alias.name.split(".")[0]
                        aliases[bound] = alias.name if alias.asname else bound
                        scope.pop(bound, None)
        # Defs and aliases bound in this body are visible to all of it.
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner != scope.get(node.name, ("",))[0]:
                scope[node.name] = (owner, node.name)
        # An alias (`g = f`) binds in its whole function (or module) body, wherever it sits: inside an if/with/try/for
        # block too, not only at the top of the body. Nested functions and classes keep their own scopes.
        for node in self._own_nodes(body):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                key = self._resolve(node.value, scope, aliases, cls)
                if key in self.targets:
                    scope[node.targets[0].id] = key
        for node in body:
            self._visit(node, rel, scope, aliases, owner, cls)

    @staticmethod
    def _own_nodes(body):
        """Every node of this body, not descending into nested functions or classes."""
        stack = list(body)
        while stack:
            node = stack.pop()
            yield node
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.extend(ast.iter_child_nodes(node))

    def _visit(self, node, rel, scope, aliases, owner, cls) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                self._visit_handoff(dec, rel, scope, aliases, owner, cls)
            for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
                self._visit(d, rel, scope, aliases, owner, cls)
            self._walk_body(node.body, rel, scope, aliases, f"{owner}.<{node.name}>", cls)
            return
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                self._visit_handoff(dec, rel, scope, aliases, owner, cls)
            inner = f"{owner}.{node.name}"
            self._walk_body(node.body, rel, scope, aliases, inner, inner)
            return
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if self._resolve(node.value, scope, aliases, cls) in self.targets:
                return  # an alias, checked where it is called
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute) and t.value.attr == "dependency_overrides":
                    self._visit(t.value, rel, scope, aliases, owner, cls)
                    self._visit(node.value, rel, scope, aliases, owner, cls)
                    if self._resolve(t.slice, scope, aliases, cls) not in self.targets:
                        self._visit(t.slice, rel, scope, aliases, owner, cls)
                    return
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and ".<" in owner:
            parent_owner, _, parent = owner.rpartition(".<")
            if (parent_owner, parent.rstrip(">")) in self.factories and scope.get(node.value.id) == (owner, node.value.id):
                return  # a dependency factory handing back its dependency; its callers are checked
        if isinstance(node, ast.Call):
            self._visit_call(node, rel, scope, aliases, owner, cls)
            return
        key = self._resolve(node, scope, aliases, cls) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if key in self.targets and isinstance(getattr(node, "ctx", ast.Load()), ast.Load):
            self.values.append((rel, node.lineno, key[1]))
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child, rel, scope, aliases, owner, cls)

    def _visit_handoff(self, node, rel, scope, aliases, owner, cls) -> None:
        """A decorator: the target named in it is handed to FastAPI, its other parts are code."""
        if self._resolve(node, scope, aliases, cls) in self.targets:
            return
        if isinstance(node, ast.Call):
            if self._resolve(node.func, scope, aliases, cls) in self.targets:
                self._visit_call(node, rel, scope, aliases, owner, cls)
                return
            for child in list(node.args) + [k.value for k in node.keywords]:
                self._visit(child, rel, scope, aliases, owner, cls)
            self._visit(node.func, rel, scope, aliases, owner, cls)
            return
        self._visit(node, rel, scope, aliases, owner, cls)

    def _visit_call(self, node: ast.Call, rel, scope, aliases, owner, cls) -> None:
        func_name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        overrides = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "dependency_overrides"
        )
        # A hand-off to FastAPI (it resolves the target's defaults itself), or a read of its signature.
        if func_name in HANDOFF_CALLS or func_name in ROUTE_REGISTRARS or func_name in INTROSPECTION_CALLS or overrides:
            for child in list(node.args) + [k.value for k in node.keywords]:
                if self._resolve(child, scope, aliases, cls) in self.targets:
                    continue
                if (
                    func_name in HANDOFF_CALLS
                    and isinstance(child, ast.Call)
                    and self._resolve(child.func, scope, aliases, cls) in self.factories
                ):
                    for grandchild in list(child.args) + [k.value for k in child.keywords]:
                        self._visit(grandchild, rel, scope, aliases, owner, cls)
                    continue
                self._visit(child, rel, scope, aliases, owner, cls)
            self._visit(node.func, rel, scope, aliases, owner, cls)
            return
        factory = self._resolve(node.func, scope, aliases, cls)
        if factory in self.factories:
            # A dependency factory called outside Depends(...): its result is a target held as a value.
            self.values.append((rel, node.lineno, f"{factory[1]}()"))
            for child in list(node.args) + [k.value for k in node.keywords]:
                self._visit(child, rel, scope, aliases, owner, cls)
            return
        # functools.partial(f, ...): the partial must pass every FastAPI-defaulted parameter.
        if func_name == "partial" and node.args:
            key = self._resolve(node.args[0], scope, aliases, cls)
            if key in self.targets:
                self._check_call(rel, node, key, extra_args=1)
                for child in node.args[1:] + [k.value for k in node.keywords]:
                    self._visit(child, rel, scope, aliases, owner, cls)
                return
        key = self._resolve(node.func, scope, aliases, cls)
        if key in self.targets:
            self._check_call(rel, node, key)
            for child in list(node.args) + [k.value for k in node.keywords]:
                self._visit(child, rel, scope, aliases, owner, cls)
            if isinstance(node.func, ast.Call):  # getattr(M, "f")(...): visit getattr's own arguments as code
                for child in node.func.args[2:]:
                    self._visit(child, rel, scope, aliases, owner, cls)
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child, rel, scope, aliases, owner, cls)


@functools.lru_cache(maxsize=4)
def _scan_root(root: Path) -> _Scanner:
    return _Scanner(root)


def _scan() -> _Scanner:
    return _scan_root(ROOT)


def test_no_direct_call_leaves_a_fastapi_default_in() -> None:
    scan = _scan()
    assert len(scan.targets) > 100, "the scan found the route functions"
    assert scan.missing == [], "pass every FastAPI-defaulted parameter explicitly, or make the callee a plain function:\n" + "\n".join(
        f"  {rel}:{line} {name} leaves out {params}" for rel, line, name, params in scan.missing
    )


def test_no_such_function_is_used_as_a_plain_value() -> None:
    scan = _scan()
    assert scan.values == [], "a target stored, passed or returned can be called later with its defaults left in:\n" + "\n".join(
        f"  {rel}:{line} {name}" for rel, line, name in scan.values
    )


def test_every_splat_call_into_such_a_function_is_reviewed() -> None:
    scan = _scan()
    assert {(rel, name) for rel, _, name in scan.splats} == set(SPLAT_CALLS_REVIEWED), scan.splats
    assert len(scan.splats) == SPLAT_CALL_COUNT, scan.splats


def test_the_search_function_the_routes_call_has_no_fastapi_defaults() -> None:
    assert ("routes.agent_api", "agent_search_products") not in _scan().targets


# --- the rule's own mutants: one fixture per call shape -------------------------------------------------

_DOOR = (
    "from fastapi import Depends, Query\n"
    "async def search(q: str = Query(default=''), only_seeds: bool = Query(default=False)):\n"
    "    return only_seeds\n"
)


def _fixture(tmp_path: Path, monkeypatch, files: Dict[str, str]) -> _Scanner:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        for parent in list(path.relative_to(tmp_path).parents)[:-1]:
            (tmp_path / parent / "__init__.py").touch()
        path.write_text(text)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    return _Scanner(tmp_path)


def test_fixture_a_direct_call(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {"routes/door.py": _DOOR + "async def caller():\n    return await search(q='x')\n"})
    assert scan.missing == [("routes/door.py", 5, "search", ["only_seeds"])]


def test_fixture_a_call_passing_everything_is_clean(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {"routes/door.py": _DOOR + "async def caller():\n    return await search('x', only_seeds=False)\n"})
    assert scan.missing == [] and scan.values == []


def test_fixture_an_import_alias(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR,
        "services/user.py": "from routes.door import search as find\nasync def caller():\n    return await find(q='x')\n",
    })
    assert scan.missing == [("services/user.py", 3, "search", ["only_seeds"])]


def test_fixture_an_assigned_alias(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR + "find = search\nasync def caller():\n    return await find(q='x')\n",
    })
    assert scan.missing == [("routes/door.py", 6, "search", ["only_seeds"])]


def test_fixture_a_module_attribute_and_an_attribute_chain(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR,
        "services/one.py": "import routes.door\nasync def caller():\n    return await routes.door.search(q='x')\n",
        "services/two.py": "from routes import door as d\nasync def caller():\n    return await d.search(q='x')\n",
    })
    assert sorted(scan.missing) == [("services/one.py", 3, "search", ["only_seeds"]), ("services/two.py", 3, "search", ["only_seeds"])]


def test_fixture_getattr(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR,
        "services/user.py": "import routes.door as door\nasync def caller():\n    return await getattr(door, 'search')(q='x')\n",
    })
    assert scan.missing == [("services/user.py", 3, "search", ["only_seeds"])]


def test_fixture_functools_partial(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR,
        "services/user.py": (
            "import functools\nfrom functools import partial\nfrom routes.door import search\n"
            "a = functools.partial(search, q='x')\nb = partial(search, q='x', only_seeds=True)\n"
        ),
    })
    assert scan.missing == [("services/user.py", 4, "search", ["only_seeds"])]


def test_fixture_nested_defs_and_methods(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": (
            "from fastapi import Query\n"
            "def outer():\n"
            "    async def inner(q: str = Query(default='')):\n"
            "        return q\n"
            "    return inner()\n"
            "class Door:\n"
            "    async def search(self, q: str = Query(default='')):\n"
            "        return q\n"
            "    async def caller(self):\n"
            "        return await self.search()\n"
        ),
    })
    assert sorted(scan.missing) == [("routes/door.py", 5, "inner", ["q"]), ("routes/door.py", 10, "search", ["q"])]


def test_fixture_a_target_used_as_a_value_but_not_a_fastapi_hand_off(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR + (
            "from fastapi import APIRouter, FastAPI\n"
            "router = APIRouter()\napp = FastAPI()\n"
            "router.add_api_route('/s', search)\n"
            "async def needs(dep=Depends(search)):\n    return dep\n"
            "app.dependency_overrides[search] = lambda: None\n"
            "@router.get('/x')\nasync def x():\n    return None\n"
            "callbacks = [search]\n"
        ),
    })
    assert scan.values == [("routes/door.py", 14, "search")]
    assert scan.missing == []


def test_fixture_a_splat_is_reported_not_passed(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {"routes/door.py": _DOOR + "async def caller(**kw):\n    return await search(**kw)\n"})
    assert scan.splats == [("routes/door.py", 5, "search")] and scan.missing == []


def test_fixture_a_dependency_factory(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": (
            "from fastapi import Depends, Query\n"
            "def require(role):\n"
            "    async def _dep(q: str = Query(default='')):\n"
            "        return q\n"
            "    return _dep\n"
            "async def route(x=Depends(require('admin'))):\n    return x\n"
            "held = require('admin')\n"
        ),
    })
    assert scan.values == [("routes/door.py", 8, "require()")]
    assert scan.missing == []


def test_fixture_a_local_import_shadows_the_route_of_the_same_name(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR + (
            "async def history():\n"
            "    from services.collector import search\n"
            "    return await search('x')\n"
        ),
        "services/collector.py": "async def search(q):\n    return q\n",
    })
    assert scan.missing == []


def test_fixture_dependency_overrides_and_signature_reads(tmp_path, monkeypatch) -> None:
    scan = _fixture(tmp_path, monkeypatch, {
        "routes/door.py": _DOOR + (
            "import inspect\nfrom fastapi import FastAPI\napp = FastAPI()\n"
            "app.dependency_overrides.pop(search, None)\n"
            "params = inspect.signature(search).parameters\n"
        ),
    })
    assert scan.values == [] and scan.missing == []


@pytest.mark.parametrize("block", [
    "    if flag:\n        find = search\n    return await find(q='x')\n",
    "    with ctx():\n        find = search\n    return await find(q='x')\n",
    "    try:\n        find = search\n    except Exception:\n        find = search\n    return await find(q='x')\n",
    "    for _ in range(1):\n        find = search\n    return await find(q='x')\n",
    "    if flag:\n        pass\n    else:\n        while True:\n            find = search\n            break\n    return await find(q='x')\n",
], ids=["if", "with", "try", "for", "else-while"])
def test_fixture_an_alias_bound_inside_a_nested_block(tmp_path, monkeypatch, block) -> None:
    scan = _fixture(tmp_path, monkeypatch, {"routes/door.py": _DOOR + "async def caller(flag=True, ctx=None):\n" + block})
    assert [(m[2], m[3]) for m in scan.missing] == [("search", ["only_seeds"])], scan.missing
    assert scan.values == []
