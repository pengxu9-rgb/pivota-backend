"""Which backend entrypoints can call a content_key writer that folds the barcode in (ADR-032 M11 item 2).

With its intake identity door flag OFF, each writer below builds content_key from brand + title + barcode
instead of resolving it through services/intake_identity.resolve_or_attach_content_identity, so a listing
can land on a key no other door mints:

  services.catalog_sync_service.ingest_standard_products    ENABLE_INTAKE_IDENTITY_SYNC
  services.catalog_enrichment_agent.apply.apply_ingest_plan ENABLE_INTAKE_IDENTITY_ENRICHMENT
  scripts.backfill_content_key._drive                       none: it folds whatever the flags say

This module is the static call graph tests/test_content_key_fold_reach.py reads. Calls resolve by name
through each module's imports (`from M import f`, `import M as m; m.f()`, lazy imports inside functions
included) or to a same-module function (`f()`). Reach is per module (any function of it), so a method's
calls count for its own module; calls on an object (`obj.f()`, `self.f()`), getattr and functions passed
as arguments are not followed across modules, so a module NOT listed is not proven safe; every listed one
has a real call chain (`chain`) to read.
"""
from __future__ import annotations

import ast
import os
from collections import defaultdict, deque
from functools import lru_cache
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

Node = Tuple[str, str]  # (module, function); "<module>" is the module's top-level code

WRITERS: Dict[Node, Optional[str]] = {
    ("services.catalog_sync_service", "ingest_standard_products"): "ENABLE_INTAKE_IDENTITY_SYNC",
    ("services.catalog_enrichment_agent.apply", "apply_ingest_plan"): "ENABLE_INTAKE_IDENTITY_ENRICHMENT",
    ("scripts.backfill_content_key", "_drive"): None,
}
FIRST_PARTY = ("adapters", "config", "core", "db", "jobs", "main", "models", "routes", "scripts", "services",
               "utils")


class _Module:
    def __init__(self, repo: str, mod: str, path: str) -> None:
        self.mod = mod
        self.names: Dict[str, tuple] = {}
        self.calls: Dict[str, set] = defaultdict(set)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            return
        pkg = mod if path.endswith("__init__.py") else mod.rpartition(".")[0]
        for node in ast.walk(tree):  # every import binds its name module-wide, lazy ones too
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in FIRST_PARTY:
                        if a.asname:
                            self.names[a.asname] = ("mod", a.name)
                        else:
                            self.names[a.name.split(".")[0]] = ("mod", a.name.split(".")[0])
                            self.names[a.name] = ("mod", a.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".")
                    up = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join([*up, base] if base else up)
                if base.split(".")[0] not in FIRST_PARTY:
                    continue
                for a in node.names:
                    sub = f"{base}.{a.name}"
                    is_mod = _path_of(repo, sub) is not None
                    self.names[a.asname or a.name] = ("mod", sub) if is_mod else ("fn", base, a.name)
        self._walk(tree, "<module>")

    def _walk(self, node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk(child, child.name)
                continue
            if isinstance(child, ast.Call):
                ref = _ref(child.func)
                if ref:
                    self.calls[owner].add(ref)
            self._walk(child, owner)


def _ref(f: ast.AST) -> Optional[tuple]:
    if isinstance(f, ast.Name):
        return ("name", f.id)
    if isinstance(f, ast.Attribute):
        chain, x = [f.attr], f.value
        while isinstance(x, ast.Attribute):
            chain.append(x.attr)
            x = x.value
        if isinstance(x, ast.Name):
            chain.append(x.id)
            return ("attr", tuple(reversed(chain)))
    return None


def _path_of(repo: str, mod: str) -> Optional[str]:
    base = os.path.join(repo, *mod.split("."))
    for p in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(p):
            return p
    return None


def _modules(repo: str):
    for top in FIRST_PARTY:
        if os.path.isfile(os.path.join(repo, top + ".py")):
            yield top, os.path.join(repo, top + ".py")
        for root, dirs, files in os.walk(os.path.join(repo, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root, f)
                    mod = os.path.relpath(path, repo)[:-3].replace(os.sep, ".")
                    yield (mod[: -len(".__init__")] if mod.endswith(".__init__") else mod), path


def _resolve(mods: Dict[str, _Module], m: str, ref: tuple) -> List[Node]:
    M = mods[m]
    if ref[0] == "name":
        bound = M.names.get(ref[1])
        if bound and bound[0] == "fn":
            return [(bound[1], bound[2])]
        return [(m, ref[1])]  # a same-module function (a name defined nowhere is a node nothing reaches)
    chain = ref[1]
    for cut in range(len(chain) - 1, 0, -1):  # the longest dotted prefix an import bound to a module
        bound = M.names.get(".".join(chain[:cut]))
        if bound and bound[0] == "mod" and bound[1] in mods:
            return [(bound[1], chain[cut])]
        if bound is None and cut > 1:
            head = M.names.get(chain[0])
            if head and head[0] == "mod":
                target = ".".join([head[1], *chain[1:cut]])
                if target in mods:
                    return [(target, chain[cut])]
    return []


@lru_cache(maxsize=4)
def reach(repo: str) -> Dict[Node, Dict[Node, Node]]:
    """{writer: {node that can call it: the next hop towards it}}."""
    mods = {m: _Module(repo, m, p) for m, p in _modules(repo)}
    callers: Dict[Node, Set[Node]] = defaultdict(set)
    for m, M in mods.items():
        for owner, refs in M.calls.items():
            for ref in refs:
                for target in _resolve(mods, m, ref):
                    callers[target].add((m, owner))
    out: Dict[Node, Dict[Node, Node]] = {}
    for writer in WRITERS:
        nxt: Dict[Node, Node] = {writer: writer}
        q = deque([writer])
        while q:
            t = q.popleft()
            for c in callers[t]:
                if c not in nxt:
                    nxt[c] = t
                    q.append(c)
        out[writer] = nxt
    return out


def writers_reached(repo: str, module: str) -> FrozenSet[Node]:
    """The writers any function of `module` (or its top-level code) can call."""
    return frozenset(w for w, nxt in reach(repo).items() if any(n[0] == module for n in nxt))


def chain(repo: str, module: str, writer: Node) -> List[str]:
    """The shortest call chain from `module`'s own code to `writer`, as module:function hops."""
    nxt = reach(repo)[writer]
    best: Optional[List[str]] = None
    for start in (n for n in nxt if n[0] == module):
        hops, node = [], start
        while node != writer:
            hops.append(f"{node[0]}:{node[1]}")
            node = nxt[node]
        hops.append(f"{writer[0]}:{writer[1]}")
        if best is None or len(hops) < len(best):
            best = hops
    return best or []


def entrypoints(repo: str) -> List[str]:
    """Every scripts.* and jobs.* module."""
    return sorted(m for m, _ in _modules(repo) if m.startswith(("scripts.", "jobs.")))
