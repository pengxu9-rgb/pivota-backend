"""GUARD: a seed's `market` partition is not its market or its currency.

`external_product_seeds.market` is a storage partition. Every seed is written with 'US'
(services/catalog_enrichment_agent/ingestion.SEED_PARTITION_MARKET), the SGD seeds and the AUD/JPY
acquisition seeds included, and ADR-026 (resolved question 2) drops the partition: a seed is served by
its currency. Code that reads the partition as the row's market or currency gets every non-USD seed
wrong: an SG buyer's click minted as US, an SG store probed as US, an SGD seed that refresh cannot
select. The 2026-11-06 audit found ~19 such sites; each was fixed (or not) one consumer at a time,
because the wrong reading is the obvious one and nothing failed CI.

This scanner fails CI on a NEW site. The known sites are on tests/fixtures/seed_partition_allowlist.json,
which later PRs shrink as they move each consumer onto services.seed_serving. Two shapes:

  P  a row-market read, `<x>.get("market")` or `<x>["market"]`, reaching a currency/market/link/probe
     sink in the same function, directly or through local names assigned from it (`or "US"` and any other
     wrapper stay tainted). Only in modules that handle seed rows: the source names
     `external_product_seeds`, the module is a seed module, or it imports a seed function from one.
  S  a string literal or f-string naming `external_product_seeds` (or its `eps.market` alias) that binds the
     partition (`market = :x`, `market = 'US'`, `upper(trim(market)) = ...`), or, inside a function, SQL
     assembled there from a literal that names the table and another that binds `market`. Docstrings are prose.

A hit is keyed (file, enclosing qualname, rule), never a line, so unrelated edits do not churn the list.
The test fails on a hit not on the list, and on a list entry that no longer hits (a fix PR deletes its entry).

KNOWN GAPS (pinned by test_the_known_gaps_are_not_caught): a BUYER market passed as a partition
(`fetch_external_seed_rows(market=request.market)`) is not a row read and neither rule sees it; nor does either
rule see a Python comparison of the partition (`row.get("market") == wanted`, `"JPY" if market == "JP"`). Those
sites are tracked in the audit, not here.
"""

from __future__ import annotations

import ast
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = ROOT / "tests" / "fixtures" / "seed_partition_allowlist.json"
SCAN_DIRS = ("routes", "services", "jobs", "scripts", "db")
SEED_TABLE = "external_product_seeds"
FIX_HINT = "use services.seed_serving, or add an allowlist entry with a reason"

# ------------------------------------------------------------------------------------------------------------------
# Sinks
# ------------------------------------------------------------------------------------------------------------------

#: A call to one of these with a partition-tainted argument reads the partition as a market or currency, or keys
#: a link, a probe or an allowlist by it.
SINK_CALLS: FrozenSet[str] = frozenset(
    {
        # market -> currency maps
        "seed_serving_currency",
        "expected_currency_for_market",
        "pricing_currency_for_region",
        "pricing_currency_for_region_or_none",
        "_declared_buyer_currency",
        "market_currency",
        "validate_market_currency",
        # link / probe / allowlist
        "make_redirect_token",
        "get_allowed_domains_for_market",
        "fetch_url_for_market",
        "market_read_currency",
        "apply_utm",
    }
)
#: Any callee whose name says it maps to a currency (`currency_for`, an injected `serving_currency`): a
#: partition handed to it is read as a currency. Beyond the named list, so the next spelling is caught too.
SINK_CALL_PATTERN: Optional["re.Pattern[str]"] = re.compile(r"currency", re.IGNORECASE)
#: `<MAP>.get(tainted)` or `<MAP>[tainted]` on one of these market-keyed maps, or on any `*_by_market` map
#: (a per-market allowlist or rule table).
SINK_MAPS: FrozenSet[str] = frozenset({"_MARKET_CURRENCY", "MARKET_EXPECTED_CURRENCY", "MARKET_LOCALE_SEGMENT"})
SINK_MAP_SUFFIX = "_by_market"
#: `<any call>(..., market=<tainted>)`.
SINK_KEYWORD = "market"
#: `<any call>(..., json=/params=/data=<tainted>)`: an outbound request that carries the partition as a market.
SINK_REQUEST_KEYWORDS: FrozenSet[str] = frozenset({"json", "params", "data"})

# Rule S: the partition bound in SQL text. `eps` is the repo's alias for the seed table; SQL that only uses the
# alias (a CTE fragment, a window ORDER BY) names the table elsewhere.
_SEED_TABLE_TEXT = re.compile(r"\bexternal_product_seeds\b|\beps\.market\b", re.IGNORECASE)
_SQL_BIND = re.compile(r"(\b\w+\.)?\bmarket\s*=\s*(:|CAST\(:|'|\$\d)", re.IGNORECASE)
_SQL_TRIM_BIND = re.compile(r"upper\(\s*trim\(\s*(\w+\.)?market\s*\)\s*\)\s*=", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Hit:
    file: str
    qualname: str
    rule: str
    line: int = 0  # for the failure message only; never part of the key
    sink: str = ""  # likewise

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.file, self.qualname, self.rule)


# ------------------------------------------------------------------------------------------------------------------
# Rule P: taint from a row-market read to a sink, per function
# ------------------------------------------------------------------------------------------------------------------


def _is_market_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "market"


def is_row_market_read(node: ast.AST) -> bool:
    """`<x>.get("market", ...)` or `<x>["market"]` (a read, not a store)."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return bool(node.args) and _is_market_const(node.args[0])
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return _is_market_const(node.slice)
    return False


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def scope_nodes(tree: ast.Module) -> List[Tuple[str, List[ast.AST]]]:
    """Every node of the tree, once, under its innermost scope: (qualname, nodes in source order).

    A function is its own scope (`f`, `C.f`, `f.<locals>.g`); lambdas and comprehensions belong to the enclosing
    one. A def node itself (its decorators and defaults) belongs to the enclosing scope. A class body's own
    statements are the class's scope (`C`); the module's are `<module>`."""
    scopes: Dict[str, List[ast.AST]] = {"<module>": []}
    # (node, scope qualname, prefix for a def found under it)
    stack: List[Tuple[ast.AST, str, str]] = [(child, "<module>", "") for child in reversed(list(ast.iter_child_nodes(tree)))]
    while stack:
        node, qual, prefix = stack.pop()
        scopes.setdefault(qual, []).append(node)
        if isinstance(node, _DEFS):
            inner = f"{prefix}{node.name}"
            scopes.setdefault(inner, [])
            children = [(c, inner, f"{inner}.<locals>.") for c in node.body]
            outer = [c for c in ast.iter_child_nodes(node) if c not in node.body]
            stack.extend(reversed(children))
            stack.extend((c, qual, prefix) for c in reversed(outer))
        elif isinstance(node, ast.ClassDef):
            inner = f"{prefix}{node.name}"
            scopes.setdefault(inner, [])
            stack.extend((c, inner, f"{inner}.") for c in reversed(node.body))
        else:
            stack.extend((c, qual, prefix) for c in reversed(list(ast.iter_child_nodes(node))))
    return list(scopes.items())


def _tainted(expr: Optional[ast.AST], names: Set[str]) -> bool:
    if expr is None:
        return False
    for node in ast.walk(expr):
        if is_row_market_read(node):
            return True
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            return True
    return False


def _target_names(target: ast.AST) -> List[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


def _tainted_names(nodes: Sequence[ast.AST], *, propagate: bool = True) -> Set[str]:
    """Local names assigned (anywhere in the scope, to a fixpoint) from a row-market read or another
    tainted name. Order-insensitive on purpose: simple and conservative."""
    assigns: List[Tuple[List[str], ast.AST]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            assigns.append(([n for t in node.targets for n in _target_names(t)], node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            assigns.append((_target_names(node.target), node.value))
        elif isinstance(node, ast.NamedExpr):
            assigns.append((_target_names(node.target), node.value))
    names: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for targets, value in assigns:
            if set(targets) <= names:
                continue
            if _tainted(value, names if propagate else set()):
                names.update(targets)
                changed = True
        if not propagate:
            break
    return names


def _call_name(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _map_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_sink_map(node: ast.AST, maps: FrozenSet[str]) -> bool:
    name = _map_name(node)
    return name is not None and (name in maps or (bool(SINK_MAP_SUFFIX) and name.endswith(SINK_MAP_SUFFIX)))


def _is_sink_call(name: Optional[str], sinks: FrozenSet[str]) -> bool:
    if name is None:
        return False
    return name in sinks or (SINK_CALL_PATTERN is not None and bool(SINK_CALL_PATTERN.search(name)))


def _sink_line(
    nodes: Sequence[ast.AST], names: Set[str], sinks: FrozenSet[str], maps: FrozenSet[str]
) -> Optional[Tuple[int, str]]:
    """(line, sink kind) where a tainted value first reaches a sink in this scope, or None."""
    for node in nodes:
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if _is_sink_call(name, sinks) and any(_tainted(a, names) for a in [*node.args, *(k.value for k in node.keywords)]):
                return node.lineno, "call"
            if (
                name == "get"
                and isinstance(node.func, ast.Attribute)
                and _is_sink_map(node.func.value, maps)
                and node.args
                and _tainted(node.args[0], names)
            ):
                return node.lineno, "map_get"
            if any(k.arg == SINK_KEYWORD and _tainted(k.value, names) for k in node.keywords):
                return node.lineno, "market_kwarg"
            if any(k.arg in SINK_REQUEST_KEYWORDS and _tainted(k.value, names) for k in node.keywords):
                return node.lineno, "request_kwarg"
        elif isinstance(node, ast.Subscript):
            if isinstance(node.ctx, ast.Load) and _is_sink_map(node.value, maps) and _tainted(node.slice, names):
                return node.lineno, "map_subscript"
            # A dict-key write keyed by the partition: hosts[(host, market)] = ...
            if (
                isinstance(node.ctx, ast.Store)
                and isinstance(node.slice, ast.Tuple)
                and any(_tainted(e, names) for e in node.slice.elts)
            ):
                return node.lineno, "key_write"
    return None


def scan_rule_p(
    source: str,
    file: str,
    *,
    sinks: FrozenSet[str] = SINK_CALLS,
    maps: FrozenSet[str] = SINK_MAPS,
    propagate: bool = True,
    scopes: Optional[List[Tuple[str, List[ast.AST]]]] = None,
) -> List[Hit]:
    scopes = scope_nodes(_parse(source)) if scopes is None else scopes
    hits = []
    for qualname, nodes in scopes:
        if not any(is_row_market_read(node) for node in nodes):
            continue
        names = _tainted_names(nodes, propagate=propagate)
        found = _sink_line(nodes, names, sinks, maps)
        if found is not None:
            hits.append(Hit(file, qualname, "P", found[0], found[1]))
    return hits


# ------------------------------------------------------------------------------------------------------------------
# Rule S: the partition bound in SQL that names the seed table
# ------------------------------------------------------------------------------------------------------------------


def _literal_text(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _docstring_nodes(tree: ast.Module) -> Set[int]:
    """Docstrings are prose, not SQL: an example `market = :market` in one is not a bind."""
    out: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def _names_seed_table(sql: str) -> bool:
    return bool(_SEED_TABLE_TEXT.search(sql))


def _binds_market(sql: str) -> bool:
    return bool(_SQL_BIND.search(sql) or _SQL_TRIM_BIND.search(sql))


def binds_partition(sql: str) -> bool:
    return _names_seed_table(sql) and _binds_market(sql)


def scan_rule_s(
    source: str,
    file: str,
    *,
    assembled: bool = True,
    tree: Optional[ast.Module] = None,
    scopes: Optional[List[Tuple[str, List[ast.AST]]]] = None,
) -> List[Hit]:
    """A literal that names the seed table and binds `market`. In a function, also SQL ASSEMBLED there:
    one literal names the seed table and another binds `market` (`where.append("market = :market")`).
    Module-level constants are read one literal at a time."""
    tree = _parse(source) if tree is None else tree
    scopes = scope_nodes(tree) if scopes is None else scopes
    docstrings = _docstring_nodes(tree)
    hits = []
    for qualname, nodes in scopes:
        names_table = binds = None
        for node in nodes:
            text = None if id(node) in docstrings else _literal_text(node)
            if text is None:
                continue
            if binds_partition(text):
                hits.append(Hit(file, qualname, "S", node.lineno, "sql"))
                break
            if assembled and qualname != "<module>":
                names_table = names_table or (node.lineno if _names_seed_table(text) else None)
                binds = binds or (node.lineno if _binds_market(text) else None)
                if names_table and binds:
                    hits.append(Hit(file, qualname, "S", binds, "assembled_sql"))
                    break
    return hits


# ------------------------------------------------------------------------------------------------------------------
# Which files, and which of them handle seed rows
# ------------------------------------------------------------------------------------------------------------------


def _scan_files() -> List[Path]:
    out = []
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT)
            if any(part in {"tests", "__pycache__"} for part in rel.parts) or rel.name.startswith("test_"):
                continue
            out.append(path)
    return out


def _module_name(rel: Path) -> str:
    return ".".join(rel.with_suffix("").parts)


_FROM_IMPORT = re.compile(r"^[ \t]*from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", re.MULTILINE)


def seed_row_modules(sources: Dict[str, str]) -> Set[str]:
    """Modules (repo-relative paths) that handle seed rows: the source names the seed table, the module is a
    seed module (`seed` in its file name), or it imports a seed function (`seed` in the name) from one of those.
    The file-level stand-in for "functions that take or iterate rows from a function that reads the table"."""
    direct = {f for f, src in sources.items() if SEED_TABLE in src or "seed" in Path(f).stem}
    direct_modules = {_module_name(Path(f)) for f in direct}
    scoped = set(direct)
    for f, src in sources.items():
        if f in scoped:
            continue
        for module, names in _FROM_IMPORT.findall(src):
            if module in direct_modules and "seed" in names:
                scoped.add(f)
                break
    return scoped


def _parse(source: str) -> ast.Module:
    # Repo files with old escape sequences warn on parse; that is not this guard's business.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source)


def read_sources() -> Dict[str, str]:
    return {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in _scan_files()}


def scan_repo(sources: Optional[Dict[str, str]] = None) -> List[Hit]:
    sources = read_sources() if sources is None else sources
    seed_modules = seed_row_modules(sources)
    hits: List[Hit] = []
    for file, src in sources.items():
        names_table = _names_seed_table(src)
        if not names_table and file not in seed_modules:
            continue
        tree = _parse(src)
        scopes = scope_nodes(tree)
        if names_table:
            hits.extend(scan_rule_s(src, file, tree=tree, scopes=scopes))
        if file in seed_modules:
            hits.extend(scan_rule_p(src, file, scopes=scopes))
    return sorted(hits)


def load_allowlist() -> List[dict]:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))["entries"]


def reconcile(hits: Sequence[Hit], entries: Sequence[dict]) -> Tuple[List[Hit], List[dict]]:
    """(hits not on the list, list entries that no longer hit)."""
    allowed = {(e["file"], e["qualname"], e["rule"]) for e in entries}
    found = {h.key for h in hits}
    new = sorted({h.key: h for h in hits if h.key not in allowed}.values())
    stale = [e for e in entries if (e["file"], e["qualname"], e["rule"]) not in found]
    return new, stale


# ------------------------------------------------------------------------------------------------------------------
# The guard
# ------------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repo_sources() -> Dict[str, str]:
    return read_sources()


@pytest.fixture(scope="module")
def repo_hits(repo_sources) -> List[Hit]:
    return scan_repo(repo_sources)


def test_the_scanner_found_files(repo_sources) -> None:
    # Paths are judged relative to the repo root, so a checkout under a `.claude` or `tests` directory still scans.
    assert len(repo_sources) > 500, f"only {len(repo_sources)} files scanned: the sweep is not looking where the code is"
    assert {"routes", "services", "jobs", "scripts", "db"} <= {f.split("/", 1)[0] for f in repo_sources}
    assert len(seed_row_modules(repo_sources)) > 50


def test_no_new_seed_partition_reads(repo_hits) -> None:
    new, _ = reconcile(repo_hits, load_allowlist())
    assert not new, (
        "A seed's `market` is a storage partition, not its market or currency (SGD/AUD/JPY seeds sit in 'US'). "
        f"{FIX_HINT}:\n" + "\n".join(f"  {h.file}:{h.line} {h.qualname} rule {h.rule} ({h.sink})" for h in new)
    )


def test_no_stale_allowlist_entries(repo_hits) -> None:
    _, stale = reconcile(repo_hits, load_allowlist())
    assert not stale, (
        "These allowlist entries no longer hit: the site was fixed or moved. Delete the entry (that is how the "
        "list shrinks):\n" + "\n".join(f"  {e['file']} {e['qualname']} rule {e['rule']} ({e['finding']})" for e in stale)
    )


#: An audit ID (G1, A1/S1, ...), N<k> for a site found beyond the audit, OK-partition for a legitimate partition use,
#: or N/A for a hit that is not the seed partition at all (a buyer, offer or config market).
_FINDING = re.compile(r"^(OK-partition|N/A|[A-Z]{1,2}\d+(/[A-Z]{1,2}\d+)?)$")


def test_every_allowlist_entry_is_well_formed() -> None:
    entries = load_allowlist()
    keys = [(e["file"], e["qualname"], e["rule"]) for e in entries]
    assert len(keys) == len(set(keys)), "duplicate allowlist keys"
    for e in entries:
        assert set(e) == {"file", "qualname", "rule", "finding", "reason"}, e
        assert e["rule"] in {"P", "S"}, e
        assert _FINDING.match(e["finding"]), e
        assert (ROOT / e["file"]).is_file(), e
        reason = e["reason"].strip()
        assert len(reason) >= 25 and reason.lower() not in {"legacy", "known", "todo"}, e


def test_removing_an_entry_or_adding_a_bogus_one_fails(repo_hits) -> None:
    entries = load_allowlist()
    assert entries
    new, stale = reconcile(repo_hits, entries[1:])
    assert [h.key for h in new] == [(entries[0]["file"], entries[0]["qualname"], entries[0]["rule"])] and not stale
    bogus = {"file": "services/seed_serving.py", "qualname": "seed_row_market", "rule": "P",
             "finding": "OK-partition", "reason": "a bogus entry that matches no site at all"}
    new, stale = reconcile(repo_hits, [*entries, bogus])
    assert not new and stale == [bogus]


def test_the_helper_module_itself_is_clean(repo_hits) -> None:
    assert not [h for h in repo_hits if h.file == "services/seed_serving.py"]


# ------------------------------------------------------------------------------------------------------------------
# Self-tests: the scanner on inline sources, and mutants of the scanner
# ------------------------------------------------------------------------------------------------------------------

CAUGHT = {
    "direct": """
def f(row):
    return seed_serving_currency(row["market"])
""",
    "get": """
def f(row):
    return expected_currency_for_market(row.get("market"))
""",
    "tainted_local": """
def f(row):
    m = str(row.get("market") or "").upper()
    cur = m
    return pricing_currency_for_region_or_none(cur)
""",
    "or_default": """
def f(seed):
    market = seed.get("market") or "US"
    return _declared_buyer_currency(market)
""",
    "or_constant": """
def f(seed):
    market = seed["market"] or DEFAULT_SEED_SERVING_MARKET
    return validate_market_currency(market, seed["price_currency"])
""",
    "market_kwarg": """
def f(row):
    return make_link(url=row["url"], market=row.get("market"))
""",
    "token_dict": """
def f(row):
    payload = {"url": row["url"], "market": row["market"]}
    return make_redirect_token(payload)
""",
    "map_get": """
def f(row):
    return MARKET_LOCALE_SEGMENT.get(row.get("market"), "")
""",
    "map_module_attr": """
def f(row):
    return reap._MARKET_CURRENCY.get(row["market"])
""",
    "map_subscript": """
def f(row):
    return MARKET_EXPECTED_CURRENCY[row["market"]]
""",
    "probe_key": """
def f(rows):
    hosts = {}
    for row in rows:
        m = row.get("market")
        hosts[(row["host"], m)] = row
    return hosts
""",
    "walrus": """
def f(row):
    if (m := row.get("market")):
        return apply_utm(row["url"], {"market": m})
""",
    "method": """
class C:
    async def f(self, row):
        return await fetch_url_for_market(row["url"], row["market"])
""",
    "injected_currency_map": """
def classify(row, *, serving_currency):
    market = row.get("market")
    return serving_currency(market)
""",
    "by_market_allowlist": """
def f(seed_row, allowlist_by_market):
    return allowlist_by_market.get(seed_row.get("market"), [])
""",
    "request_body": """
async def f(client, row):
    body = {"domain": row["url"], "market": row.get("market")}
    return await client.post("/extract", json=body)
""",
}

NOT_CAUGHT = {
    "request_attr": """
def f(request, payload, market_hint):
    a = seed_serving_currency(request.market)
    b = make_link(market=payload.market)
    return expected_currency_for_market(market_hint), a, b
""",
    "label_only": """
def f(row):
    return {"market": row.get("market"), "currency": row.get("price_currency")}
""",
    "other_key": """
def f(row):
    return seed_serving_currency(row["serving_market"])
""",
    "store_not_read": """
def f(row):
    row["market"] = "US"
    return make_link(market="US")
""",
    "nested_scope_does_not_leak": """
def f(row):
    m = row.get("market")
    def g(m2):
        return seed_serving_currency(m2)
    return g
""",
}


@pytest.mark.parametrize("name", sorted(CAUGHT))
def test_the_scanner_catches(name) -> None:
    hits = scan_rule_p(CAUGHT[name], "x.py")
    assert hits, f"{name} must be caught"


@pytest.mark.parametrize("name", sorted(NOT_CAUGHT))
def test_the_scanner_ignores(name) -> None:
    assert scan_rule_p(NOT_CAUGHT[name], "x.py") == [], name


def test_hits_are_keyed_by_qualname_not_line() -> None:
    src = CAUGHT["method"]
    (hit,) = scan_rule_p(src, "x.py")
    assert hit.key == ("x.py", "C.f", "P")
    (moved,) = scan_rule_p("\n\n\n# unrelated edit\n" + src, "x.py")
    assert moved.key == hit.key and moved.line != hit.line
    nested = "def outer(rows):\n    def inner(row):\n        return seed_serving_currency(row['market'])\n    return inner\n"
    assert [h.key for h in scan_rule_p(nested, "x.py")] == [("x.py", "outer.<locals>.inner", "P")]
    module = "rows = []\nCUR = [seed_serving_currency(r['market']) for r in rows]\n"
    assert [h.key for h in scan_rule_p(module, "x.py")] == [("x.py", "<module>", "P")]


SQL_CAUGHT = {
    "named_bind": 'def f():\n    return "SELECT id FROM external_product_seeds WHERE market = :market"\n',
    "aliased_cast": 'def f():\n    return "SELECT 1 FROM external_product_seeds eps WHERE eps.market = CAST(:m AS text)"\n',
    "literal": "def f():\n    return \"UPDATE external_product_seeds SET x = 1 WHERE market='US'\"\n",
    "dollar": 'def f():\n    return "SELECT 1 FROM external_product_seeds WHERE market = $1"\n',
    "trim": 'def f():\n    return "SELECT 1 FROM external_product_seeds WHERE upper(trim(market)) = :refresh_market"\n',
    "fstring": 'def f(t):\n    return f"SELECT 1 FROM external_product_seeds {t} WHERE {t}.market = :market"\n',
    "implicit_concat": 'def f():\n    return ("SELECT 1 FROM external_product_seeds"\n            " WHERE market = :market")\n',
    "assembled": (
        "def f(market):\n"
        "    where = ['status = :status']\n"
        "    if market:\n"
        "        where.append('market = :market')\n"
        "    return f\"SELECT * FROM external_product_seeds WHERE {' AND '.join(where)}\"\n"
    ),
    "eps_alias_constant": "ORDER_BY = \"CASE WHEN eps.market = 'US' THEN 0 ELSE 1 END, eps.id\"\n",
}

SQL_NOT_CAUGHT = {
    "other_table": 'def f():\n    return "SELECT 1 FROM catalog_offers WHERE market = :market"\n',
    "select_only": 'def f():\n    return "SELECT market FROM external_product_seeds"\n',
    "serving_market": 'def f():\n    return "SELECT 1 FROM external_product_seeds WHERE serving_market = :m"\n',
    "insert_values": 'def f():\n    return "INSERT INTO external_product_seeds (id, market) VALUES (:id, :market)"\n',
    "docstring": 'def f():\n    """Reads external_product_seeds WHERE market = :market, as an example."""\n    return 1\n',
    "module_fragments_apart": 'A = "SELECT 1 FROM external_product_seeds"\nB = "SELECT 1 FROM catalog_offers WHERE market = :m"\n',
}


@pytest.mark.parametrize("name", sorted(SQL_CAUGHT))
def test_rule_s_catches(name) -> None:
    assert scan_rule_s(SQL_CAUGHT[name], "x.py"), name


@pytest.mark.parametrize("name", sorted(SQL_NOT_CAUGHT))
def test_rule_s_ignores(name) -> None:
    assert scan_rule_s(SQL_NOT_CAUGHT[name], "x.py") == [], name


def _self_test_failures(**mutant) -> List[str]:
    return [n for n, src in CAUGHT.items() if not scan_rule_p(src, "x.py", **mutant)]


@pytest.mark.parametrize(
    "mutant, must_miss",
    [
        ({"sinks": SINK_CALLS - {"seed_serving_currency"}}, "direct"),
        ({"sinks": SINK_CALLS - {"make_redirect_token"}}, "token_dict"),
        ({"sinks": SINK_CALLS - {"_declared_buyer_currency"}}, "or_default"),
        ({"maps": SINK_MAPS - {"MARKET_LOCALE_SEGMENT"}}, "map_get"),
        ({"propagate": False}, "tainted_local"),
    ],
    ids=["drop_currency_sink", "drop_token_sink", "drop_buyer_currency_sink", "drop_locale_map", "no_propagation"],
)
def test_a_mutated_scanner_fails_a_self_test(monkeypatch, mutant, must_miss) -> None:
    import sys

    assert _self_test_failures() == []
    # The name pattern would still catch a dropped *currency* sink; a mutant that drops a named sink drops both.
    monkeypatch.setattr(sys.modules[__name__], "SINK_CALL_PATTERN", None)
    assert must_miss in _self_test_failures(**mutant)


def test_a_scanner_whose_wrappers_drop_the_taint_fails_a_self_test(monkeypatch) -> None:
    """`x or "US"`, `str(x).upper()`: taint must survive any wrapper. A mutant that taints only a bare read or a
    bare name misses them."""
    import sys

    def shallow(expr, names):
        return expr is not None and (is_row_market_read(expr) or (isinstance(expr, ast.Name) and expr.id in names))

    monkeypatch.setattr(sys.modules[__name__], "_tainted", shallow)
    missed = set(_self_test_failures())
    assert {"or_default", "or_constant", "tainted_local", "token_dict"} <= missed


def test_a_scanner_without_the_keyword_or_key_sinks_fails_a_self_test(monkeypatch) -> None:
    import sys

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "SINK_KEYWORD", "no_such_keyword")
    assert "market_kwarg" in _self_test_failures()
    monkeypatch.undo()
    monkeypatch.setattr(module, "is_row_market_read", lambda node: False)
    assert set(_self_test_failures()) == set(CAUGHT)


@pytest.mark.parametrize(
    "attr, value, must_miss",
    [
        ("SINK_CALL_PATTERN", None, "injected_currency_map"),
        ("SINK_MAP_SUFFIX", "", "by_market_allowlist"),
        ("SINK_REQUEST_KEYWORDS", frozenset(), "request_body"),
    ],
)
def test_dropping_an_added_sink_fails_a_self_test(monkeypatch, attr, value, must_miss) -> None:
    import sys

    monkeypatch.setattr(sys.modules[__name__], attr, value)
    assert must_miss in _self_test_failures()


def test_a_rule_s_without_the_trim_shape_fails_a_self_test(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys.modules[__name__], "_SQL_TRIM_BIND", re.compile(r"(?!)"))
    assert scan_rule_s(SQL_CAUGHT["trim"], "x.py") == []


def test_a_rule_s_without_assembly_or_the_alias_fails_a_self_test(monkeypatch) -> None:
    import sys

    assert scan_rule_s(SQL_CAUGHT["assembled"], "x.py", assembled=False) == []
    monkeypatch.setattr(sys.modules[__name__], "_SEED_TABLE_TEXT", re.compile(r"\bexternal_product_seeds\b"))
    assert scan_rule_s(SQL_CAUGHT["eps_alias_constant"], "x.py") == []


# KNOWN GAPS, pinned so a scanner change that closes one updates this file and the PR that lists them.
KNOWN_GAPS = {
    # P1: a BUYER market passed as the partition. Not a row read, so rule P has no source.
    "buyer_market_as_partition": """
async def f(request):
    return await fetch_external_seed_rows(query=request.query, market=request.market)
""",
    # R3: a Python partition filter, `row.get("market") != wanted`. No comparison sink: it was 19 extra hits on
    # 2026-11-06, almost all catalog_offers or config markets, not the seed partition.
    "python_partition_filter": """
def f(rows, market):
    return [r for r in rows if str(r.get("market") or "").upper() == market]
""",
    # J5 (refresh_seeds_with_unverified_variants): a partition-decided default currency.
    "partition_decided_currency": """
def f(row, extracted):
    market = str(row.get("market") or "US").upper()
    return extracted.get("price_currency") or ("JPY" if market == "JP" else "USD")
""",
}


@pytest.mark.parametrize("name", sorted(KNOWN_GAPS))
def test_the_known_gaps_are_not_caught(name) -> None:
    assert scan_rule_p(KNOWN_GAPS[name], "x.py") == [], f"{name} is now caught: update KNOWN_GAPS and the docstring"
