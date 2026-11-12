"""No NEW silent market or currency default on code a partner can reach. The baseline only shrinks.

THE RULE (owner, 2026-09-24): a market's price is in that market's currency. Never convert, relabel
or default a currency to fit a market, and never default a market silently.

WHY A RATCHET. An audit on 2026-11-12 counted the `or "USD"` and `or "US"` defaults on the partner
route files at three commits a month apart and the numbers had not moved; a review the same week
found a brand-new default-market constant in code one day old. Each default had a local reason and
nothing made the next one visible. This file is where they become visible: every existing default
is listed in tests/fixtures/market_currency_defaults_baseline.json, and

  * a finding that is NOT in the baseline fails (a new default was added, or a file with old
    defaults was newly imported by a partner route);
  * a baseline entry that is no longer found fails too, until the entry is deleted in the same
    change -- so the list cannot keep a slot open for the next default;
  * BASELINE_COUNTS below pins the size per kind, so growing the baseline is an edit in two files.

WHAT IS A FINDING (found by AST; comments, docstrings and tests never count):

  kind a  a literal ISO-4217 code supplied when a currency is missing
  kind b  a literal ISO-3166 alpha-2 code supplied when a market / country is missing
          shapes for a and b:
            or          x or "USD"                      get         d.get("currency", "USD"), getattr(o, n, "US")
            param       def f(currency="USD")           field       class M: currency: str = "USD"
            kwdefault   Field(default="USD"), Query("US"), Column(..., default="USD")
            const       DEFAULT_MARKET = "US"           ifnot       if not currency: currency = "USD"
                                                        (also `if not market: return "US"`)
            ifexp       x if x else "USD"               init        currency = "USD" ... currency = other
            sql         COALESCE(currency, 'USD'), `currency TEXT DEFAULT 'USD'`, ELSE 'USD' END
          A module constant bound to a code (`DEFAULT_MARKET = "US"`) is followed by name, in any
          scanned file, so `x or DEFAULT_MARKET` is a finding as well.
  kind c  a hand-kept market->currency (or currency->market) mapping literal with two or more
          pairs, anywhere but the registry module. config/market_registry.json is the one table.

  The three `sql` shapes are the only regex in here: SQL lives inside string constants, where there
  is no syntax tree to walk. The regex runs on string CONSTANTS located by AST (f-string parts are
  joined first), never on raw lines, so a comment or a docstring that mentions COALESCE is not read.

SCOPE is computed, not listed by hand: the partner route files named by ROUTE_SEED_GLOBS and
ROUTE_SEED_FILES, plus every repo module they import, transitively, including imports inside
functions. A new services/ or db/ module that a partner route imports is therefore scanned without
anyone adding it anywhere. jobs/ and scripts/ are out unless a route imports them. See
OUT_OF_SCOPE and EXCLUDED_FILES for what is not scanned and why.

A LEGITIMATE DEFAULT is marked on its own line, with a reason, and stays greppable:

    currency = row_currency or "USD"  # market-default-ok: <why this one is not a silent default>

Marked findings are not compared with the baseline; they are counted in the summary this file
prints on every run. The repository starts with zero markers.

WHAT THIS CANNOT SEE: a default computed at runtime (`or some_function()`), a constant bound in a
module this scan does not reach, SQL assembled from pieces at runtime, a default set by an
environment variable or a config file, a default behind `importlib.import_module`, and anything in
the gateway repository. It counts places where a literal stands in for a missing value; it does not
prove a price is in the buyer's currency.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import io
import json
import pathlib
import re
import tokenize
import warnings
from collections import Counter
from functools import lru_cache
from typing import Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO / "tests" / "fixtures" / "market_currency_defaults_baseline.json"

# --------------------------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------------------------

#: Every route file a partner's request can enter by. The agent_* family holds the agent API
#: (v1, v2, SDK), the invoke gateway, product view, commerce and purchases, checkout intents,
#: payments and the agent-side account routes.
ROUTE_SEED_GLOBS = ("routes/agent_*.py",)

#: Partner-reachable routes outside the agent_* family, named one by one: the outbound redirect,
#: and the order / quote / offer / payment / refund / attribution routes behind the partner doors.
#: routes/employee_products.py is the one staff route here: its seed import writes the seed rows
#: that partner search serves, and it keeps its own market->currency table.
ROUTE_SEED_FILES = (
    "routes/accounts_orders_api.py",
    "routes/attribution_conversions.py",
    "routes/card_rail_outcomes.py",
    "routes/employee_products.py",
    "routes/external_offers.py",
    "routes/offer_routes.py",
    "routes/order_routes.py",
    "routes/outbound_links.py",
    "routes/payment_execution_routes.py",
    "routes/payment_routes.py",
    "routes/product_routes.py",
    "routes/product_routes_v2.py",
    "routes/quote_routes.py",
    "routes/reap_return.py",
    "routes/reap_webhooks.py",
    "routes/refund_api.py",
)

#: Trees the import walk never enters, each with the reason. Everything else in the repository is
#: in scope exactly when a seed route imports it (directly or through another module).
OUT_OF_SCOPE = {
    "tests": "test code; a fixture that says USD is not a serving default",
    ".venv": "third-party code",
    "venv": "third-party code",
    "node_modules": "third-party code",
}

#: jobs/ and scripts/ carry no entry above on purpose: they are out of scope by the IMPORT RULE
#: (no partner route imports them), not by name. The moment a route imports one, it is scanned.
#: A default added to a job or a script that no route imports is therefore NOT seen by this file
#: -- a known limit, stated here so nobody reads a green run as covering the write side.

#: Files the import walk reaches that are deliberately not scanned. Adding a line here removes a
#: file from the ratchet, so it must be argued for here, in the diff. Empty on 2026-11-12.
EXCLUDED_FILES: Dict[str, str] = {}

#: The one module allowed to own a market->currency table (ADR-026 decision 2). It loads
#: config/market_registry.json; kind c is not reported inside it. Kinds a and b still are.
REGISTRY_MODULE = "services/market_registry.py"
REGISTRY_TABLE = REPO / "config" / "market_registry.json"

#: The size of the baseline per kind. ONLY EVER LOWER THESE. When a default is removed, delete
#: its entry from the baseline file and lower the number here in the same change.
BASELINE_COUNTS = {"a": 184, "b": 90, "c": 4}

MARKER = "market-default-ok"
_MARKER_RE = re.compile(r"#.*?\b" + MARKER + r"\b:?\s*(?P<reason>.*)$")
_MIN_MARKER_REASON = 12

# --------------------------------------------------------------------------------------------
# What counts as a currency code or a market code
# --------------------------------------------------------------------------------------------

_CURRENCIES = frozenset(
    """AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP
    BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL
    GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF
    KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR
    MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG
    SEK SGD SHP SLE SOS SRD SSP STN SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS
    VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL""".split()
)
#: Codes that are also ordinary words or keywords. They count only where the expression itself
#: names a currency (`sort or "TOP"` is not a currency default; `currency or "TRY"` is).
_CURRENCY_WORDS = frozenset("ALL ANG BAM BOB COP CUP GEL GIP LAK MAD MOP PEN PHP RON SOS TOP TRY".split())

_MARKETS = frozenset(
    """AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR
    BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC
    EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK
    HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB
    LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU
    RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN
    TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW UK EU""".split()
)
#: Two capital letters are a market on sight only for these. Every other alpha-2 code collides
#: with a US state, a SQL keyword or an abbreviation (CA, IN, ID, NA, OR ...), so it counts only
#: where the expression itself names a market, a country or a region.
_MARKETS_ON_SIGHT = frozenset("US GB UK SG AU JP FR KR HK NZ CN EU SE FI HR ES NL".split())

_CURRENCY_CONTEXT = re.compile(r"curr|ccy", re.I)
_MARKET_CONTEXT = re.compile(r"market|country|countries|region|nation|locale|ship_to|geo", re.I)
#: A lower-case pair ("na", "eu", "us") is a market only beside the word market or country:
#:  is a marketplace API shard, not a buyer's market.
_MARKET_CONTEXT_STRICT = re.compile(r"market|country|countries|nation", re.I)
_DEFAULTISH_NAME = re.compile(r"DEFAULT|FALLBACK|ASSUM", re.I)
_CONSTANT_NAME = re.compile(r"^_{0,2}[A-Z][A-Z0-9_]*$")

_GET_LIKE = {"get", "pop", "setdefault", "getenv"}
_DEFAULT_KEYWORDS = {"default", "server_default", "fallback", "default_value"}
_DEFAULT_WRAPPERS = {"Field", "Query", "Form", "Body", "Header", "Cookie", "Path", "Option", "Argument"}

_SQL_COALESCE = re.compile(
    r"\b(?:COALESCE|IFNULL|NVL)\s*\((?P<ctx>[^;]{0,240}?),\s*'(?P<lit>[A-Za-z]{2,3})'\s*\)", re.I | re.S)
_SQL_DDL_DEFAULT = re.compile(
    r"\b(?P<ctx>\w*(?:currency|ccy|market|country|region)\w*)\s+(?:TEXT|VARCHAR|CHAR|CHARACTER|STRING|CITEXT)\b"
    r"[^,;']{0,60}?\bDEFAULT\s+'(?P<lit>[A-Za-z]{2,3})'",
    re.I | re.S)
_SQL_ELSE = re.compile(r"(?P<ctx>\bCASE\b[^;]{0,240}?)\bELSE\s+'(?P<lit>[A-Za-z]{2,3})'\s+END\b", re.I | re.S)
_SQL_HINT = re.compile(r"COALESCE|IFNULL|NVL|DEFAULT|ELSE", re.I)


def _classify(value: object, context: str) -> Optional[str]:
    """'a' for a currency code, 'b' for a market code, None for anything else."""
    if not isinstance(value, str):
        return None
    upper = value.upper()
    if len(value) == 3 and value.isalpha() and upper in _CURRENCIES:
        if value == upper and upper not in _CURRENCY_WORDS:
            return "a"
        return "a" if _CURRENCY_CONTEXT.search(context) else None
    if len(value) == 2 and value.isalpha() and upper in _MARKETS:
        if value == upper and upper in _MARKETS_ON_SIGHT:
            return "b"
        return "b" if (_MARKET_CONTEXT if value == upper else _MARKET_CONTEXT_STRICT).search(context) else None
    return None


class Finding(NamedTuple):
    file: str
    scope: str
    kind: str
    shape: str
    literal: str
    expr: str
    line: int
    end_line: int
    col: int


_SPACE = re.compile(r"\s+")
_SPACE_AT_PUNCT = re.compile(r"\s*([()\[\]{},])\s*")


def _normalise(text: str) -> str:
    """Whitespace-insensitive text of an expression: moving or re-wrapping a line keeps it."""
    return _SPACE_AT_PUNCT.sub(r"\1", _SPACE.sub(" ", text)).strip()


def _terminal_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


def _is_negative_test(test: ast.AST) -> bool:
    """`not x`, `x is None`, `x == ""`, `x in (None, "")`, and any and/or of those."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Is, ast.Eq, ast.In)):
        right = test.comparators[0]
        if isinstance(right, ast.Constant) and right.value in (None, ""):
            return True
        if isinstance(right, (ast.Tuple, ast.List, ast.Set)):
            return all(isinstance(e, ast.Constant) and e.value in (None, "") for e in right.elts)
    if isinstance(test, ast.BoolOp):
        return all(_is_negative_test(v) for v in test.values)
    return False


def module_constants(tree: ast.Module) -> Dict[str, str]:
    """NAME -> code for every module-level `NAME = "<code>"` binding of an ALL-CAPS name."""
    out: Dict[str, str] = {}
    for stmt in tree.body:
        target, value = None, None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target, value = stmt.target, stmt.value
        if (isinstance(target, ast.Name) and _CONSTANT_NAME.match(target.id)
                and isinstance(value, ast.Constant) and _classify(value.value, target.id)):
            out[target.id] = value.value
    return out


def _map_pairs(node: ast.AST) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
    """('market->currency' | 'currency->market', pairs) when `node` is such a mapping literal."""
    items: List[Tuple[object, ast.AST]] = []
    if isinstance(node, ast.Dict):
        items = [(k.value, v) for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        items = [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
    if len(items) < 2:
        return None

    def currency_in(value: ast.AST) -> Optional[str]:
        if isinstance(value, ast.Constant):
            v = value.value
            return v.upper() if isinstance(v, str) and len(v) == 3 and v.upper() in _CURRENCIES else None
        if isinstance(value, ast.Dict):
            for k, v in zip(value.keys, value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and _CURRENCY_CONTEXT.search(k.value):
                    return currency_in(v) if isinstance(v, ast.Constant) else None
            return None
        if isinstance(value, (ast.Tuple, ast.List)):
            hits = [c for c in (currency_in(e) for e in value.elts if isinstance(e, ast.Constant)) if c]
            return hits[0] if len(hits) == 1 else None
        return None

    def markets_in(value: ast.AST) -> List[str]:
        elts = value.elts if isinstance(value, (ast.Tuple, ast.List, ast.Set)) else [value]
        out = []
        for e in elts:
            if not (isinstance(e, ast.Constant) and isinstance(e.value, str)
                    and len(e.value) == 2 and e.value.isupper() and e.value in _MARKETS):
                return []
            out.append(e.value)
        return out

    forward = [(str(k).upper(), currency_in(v)) for k, v in items
               if len(str(k)) == 2 and str(k).isalpha() and str(k).upper() in _MARKETS]
    forward = [(m, c) for m, c in forward if c]
    if len(forward) >= 2:
        return "market->currency", sorted(forward)
    reverse: List[Tuple[str, str]] = []
    for k, v in items:
        if len(str(k)) == 3 and str(k).isupper() and str(k) in _CURRENCIES:
            reverse.extend((m, str(k)) for m in markets_in(v))
    if len(reverse) >= 2:
        return "currency->market", sorted(reverse)
    return None


_STATEMENT_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
#: Leaves the expression walk never needs to open.
_LEAVES = (ast.Name, ast.expr_context, ast.operator, ast.boolop, ast.unaryop, ast.cmpop,
           ast.Pass, ast.Break, ast.Continue, ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom)


def statements(body: Iterable[ast.AST], into_scopes: bool = True) -> Iterable[ast.stmt]:
    """Every statement under `body`, without opening a single expression (imports, `if`s and
    assignments are statements, and a file has far fewer statements than nodes)."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not into_scopes and isinstance(node, _SCOPE_NODES):
            continue
        for name in _STATEMENT_FIELDS:
            children = getattr(node, name, None)
            if children:
                stack.extend(children)


def scan_source(source: str, rel: str, shared_constants: Optional[Dict[str, str]] = None,
                tree: Optional[ast.Module] = None) -> List[Finding]:
    """Every finding in one file, in source order. Pure: no import of the file, no I/O."""
    tree = tree if tree is not None else ast.parse(source)
    constants = dict(shared_constants or {})
    constants.update(module_constants(tree))
    found: List[Finding] = []
    claimed: Set[int] = set()
    raw_lines = source.encode("utf-8").splitlines(keepends=True)

    def text(node: ast.AST) -> str:
        # ast.get_source_segment re-splits the whole file on every call; this is the same slice.
        first, last = node.lineno - 1, node.end_lineno - 1
        if first == last:
            raw = raw_lines[first][node.col_offset:node.end_col_offset]
        else:
            raw = b"".join([raw_lines[first][node.col_offset:], *raw_lines[first + 1:last],
                            raw_lines[last][:node.end_col_offset]])
        return _normalise(raw.decode("utf-8", errors="replace"))

    def literal(node: ast.AST, context: object) -> Optional[Tuple[str, str]]:
        """(kind, literal as written) when `node` is a code or a constant bound to one.
        `context` is the text that may name a currency or a market, or the node to read it from
        (read only when `node` could be a code at all: this runs on every call in the file)."""
        cls = node.__class__
        if cls is ast.Constant:
            value = node.value
            if value.__class__ is not str or len(value) not in (2, 3):
                return None
            kind = _classify(value, context if isinstance(context, str) else text(context))
            return (kind, repr(value)) if kind else None
        if cls is ast.Name:
            name = node.id
        elif cls is ast.Attribute:
            name = node.attr
        else:
            return None
        if name in constants and _CONSTANT_NAME.match(name):
            context = context if isinstance(context, str) else text(context)
            kind = _classify(constants[name], context + " " + name)
            return (kind, f"{name}={constants[name]!r}") if kind else None
        return None

    def add(node: ast.AST, scope: str, kind: str, shape: str, lit: str, expr: str) -> None:
        found.append(Finding(rel, scope, kind, shape, lit, expr, node.lineno,
                             getattr(node, "end_lineno", node.lineno) or node.lineno, node.col_offset))

    def check_if(node: ast.If, scope: str) -> None:
        negative = _is_negative_test(node.test)
        for stmt in (node.body if negative else node.orelse):
            if negative and stmt.__class__ is ast.Return and stmt.value is not None:
                # `if not market: return "US"`: the function answers a code for a missing value.
                hit = literal(stmt.value, node.test)
                if hit:
                    add(stmt, scope, hit[0], "ifnot", hit[1], f"{text(stmt)} <- if {text(node.test)}")
                continue
            target, value = None, None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign):
                target, value = stmt.target, stmt.value
            name = _terminal_name(target) if target is not None else None
            hit = literal(value, name) if name and value is not None else None
            if not hit:
                continue
            test_text = text(node.test)
            if re.search(r"\b" + re.escape(name) + r"\b", test_text):
                claimed.add(id(stmt))
                add(stmt, scope, hit[0], "ifnot", hit[1], f"{text(stmt)} <- if {test_text}")

    def check_defaults(node: ast.AST, scope: str) -> None:
        args = node.args
        if not (args.defaults or args.kw_defaults):
            return
        positional = list(args.posonlyargs) + list(args.args)
        pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        for arg, default in pairs:
            hit = literal(default, arg.arg)
            if hit:
                add(default, scope, hit[0], "param", hit[1], f"{arg.arg}={text(default)}")

    def check_init(node: ast.AST, scope: str) -> None:
        # `init`: a name set to a code and set again elsewhere in the same function, so the code
        # is what the name holds whenever the other assignment does not run.
        assigns: Dict[str, List[ast.AST]] = {}
        for stmt in statements(node.body, into_scopes=False):
            target = None
            if stmt.__class__ is ast.Assign and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif stmt.__class__ is ast.AnnAssign and stmt.value is not None:
                target = stmt.target
            if target.__class__ is ast.Name:
                assigns.setdefault(target.id, []).append(stmt)
        for name, nodes in assigns.items():
            if len(nodes) < 2 or not (_CURRENCY_CONTEXT.search(name) or _MARKET_CONTEXT.search(name)):
                continue
            for stmt in sorted(nodes, key=lambda s: s.lineno):
                if id(stmt) in claimed or stmt.value.__class__ is not ast.Constant:
                    continue
                hit = literal(stmt.value, name)
                if hit:
                    add(stmt, scope, hit[0], "init", hit[1], text(stmt))

    def check_sql(node: ast.AST, scope: str, sql: str) -> None:
        if not _SQL_HINT.search(sql):
            return
        for pattern in (_SQL_COALESCE, _SQL_DDL_DEFAULT, _SQL_ELSE):
            for m in pattern.finditer(sql):
                kind = _classify(m.group("lit"), m.group("ctx"))
                if kind:
                    add(node, scope, kind, "sql", repr(m.group("lit")), _normalise(m.group(0)))

    # Pass 1, statements only: name every scope, and take `ifnot` first so that an assignment it
    # claims is not reported again as `init`.
    scope_of: Dict[int, str] = {}
    todo: List[Tuple[ast.AST, str]] = [(tree, "<module>")]
    while todo:
        owner, scope = todo.pop()
        for stmt in statements(owner.body, into_scopes=False):
            if isinstance(stmt, _SCOPE_NODES):
                inner = stmt.name if scope == "<module>" else f"{scope}.{stmt.name}"
                scope_of[id(stmt)] = inner
                todo.append((stmt, inner))
            elif stmt.__class__ is ast.If:
                check_if(stmt, scope)
    for stmt in statements(tree.body):
        if stmt.__class__ in (ast.FunctionDef, ast.AsyncFunctionDef):
            check_init(stmt, scope_of[id(stmt)])

    # Pass 2, every node once.
    in_registry = rel == REGISTRY_MODULE
    stack: List[Tuple[ast.AST, str, Optional[ast.AST]]] = [(tree, "<module>", None)]
    while stack:
        node, scope, parent = stack.pop()
        cls = node.__class__
        if cls is ast.Constant:
            # A bare string statement is a docstring; an f-string part is read with its f-string.
            value = node.value
            if value.__class__ is str and len(value) > 12 and parent.__class__ not in (ast.Expr, ast.JoinedStr):
                check_sql(node, scope, value)
            continue
        if cls in (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef):
            # A class nested in a function body is named in pass 1 too; anything else (a def
            # inside an expression cannot exist) falls back to the enclosing scope.
            scope = scope_of.get(id(node), scope)
            if cls is not ast.ClassDef:
                check_defaults(node, scope)
        elif cls is ast.Lambda:
            check_defaults(node, scope)
        elif cls is ast.BoolOp:
            if node.op.__class__ is ast.Or:
                hits = [h for h in (literal(v, node) for v in node.values[1:]) if h]
                if hits:
                    add(node, scope, hits[-1][0], "or", hits[-1][1], text(node))
        elif cls is ast.IfExp:
            hit = literal(node.orelse, node)
            if not hit and _is_negative_test(node.test):
                hit = literal(node.body, node)
            if hit:
                add(node, scope, hit[0], "ifexp", hit[1], text(node))
        elif cls is ast.Call:
            func = node.func
            fname = func.attr if func.__class__ is ast.Attribute else getattr(func, "id", "")
            hit = None
            if fname in _GET_LIKE and len(node.args) >= 2:
                hit = literal(node.args[1], node)
            elif fname == "getattr" and len(node.args) == 3:
                hit = literal(node.args[2], node)
            if hit:
                add(node, scope, hit[0], "get", hit[1], text(node))
            for kw in node.keywords:
                hit = literal(kw.value, node) if kw.arg in _DEFAULT_KEYWORDS else None
                if hit:
                    add(node, scope, hit[0], "kwdefault", hit[1], text(node))
            if fname in _DEFAULT_WRAPPERS and node.args:
                hit = literal(node.args[0], node)
                if hit:
                    add(node, scope, hit[0], "kwdefault", hit[1], text(node))
            if fname == "dict" and not in_registry and len(node.keywords) >= 2:
                mapping = _map_pairs(node)
                if mapping:
                    rendered = mapping[0] + " " + ",".join(f"{m}={c}" for m, c in mapping[1])
                    add(node, scope, "c", "map", rendered, rendered)
        elif cls is ast.Dict:
            if not in_registry and len(node.keys) >= 2:
                mapping = _map_pairs(node)
                if mapping:
                    rendered = mapping[0] + " " + ",".join(f"{m}={c}" for m, c in mapping[1])
                    add(node, scope, "c", "map", rendered, rendered)
        elif cls in (ast.Assign, ast.AnnAssign):
            if parent.__class__ in (ast.Module, ast.ClassDef):
                target = node.target if cls is ast.AnnAssign else (
                    node.targets[0] if len(node.targets) == 1 else None)
                value = node.value
                if target.__class__ is ast.Name and value.__class__ is ast.Constant:
                    hit = literal(value, target.id)
                    if hit and _DEFAULTISH_NAME.search(target.id):
                        add(node, scope, hit[0], "const", hit[1], text(node))
                    elif hit and parent.__class__ is ast.ClassDef and cls is ast.AnnAssign:
                        add(node, scope, hit[0], "field", hit[1], text(node))
        elif cls is ast.JoinedStr:
            check_sql(node, scope, "".join(
                v.value if v.__class__ is ast.Constant and v.value.__class__ is str else "{}"
                for v in node.values))
            # The parts are read above; what is left to open is the interpolated expressions.

        for name in node._fields:
            child = getattr(node, name, None)
            if child.__class__ is list:
                for item in child:
                    if isinstance(item, ast.AST) and not isinstance(item, _LEAVES):
                        stack.append((item, scope, node))
            elif isinstance(child, ast.AST) and not isinstance(child, _LEAVES):
                stack.append((child, scope, node))

    found.sort(key=lambda f: (f.line, f.col, f.kind, f.shape, f.expr))
    return found


# --------------------------------------------------------------------------------------------
# Scope walk, markers, fingerprints
# --------------------------------------------------------------------------------------------

def _imports(tree: ast.Module, rel: str) -> Set[str]:
    """Every module `rel` imports, at module level or inside any function or class."""
    out: Set[str] = set()
    package = rel.split("/")[:-1]
    for node in statements(tree.body):
        if node.__class__ is ast.Import:
            out.update(alias.name for alias in node.names)
        elif node.__class__ is ast.ImportFrom:
            base = node.module or ""
            if node.level:
                up = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join(up + ([node.module] if node.module else []))
            if base:
                out.add(base)
                out.update(f"{base}.{alias.name}" for alias in node.names)
    return out


@lru_cache(maxsize=None)
def _resolve(module: str) -> Tuple[str, ...]:
    """Repo files that importing `module` executes: the module and each parent package."""
    parts = module.split(".")
    if parts[0] in OUT_OF_SCOPE:
        return ()
    if not (REPO / parts[0]).is_dir() and not (REPO / f"{parts[0]}.py").is_file():
        return ()  # the standard library or a third-party package
    out = []
    for i in range(1, len(parts) + 1):
        stem = "/".join(parts[:i])
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            if (REPO / candidate).is_file():
                out.append(candidate)
    return tuple(out)


def _marker_lines(source: str) -> Dict[int, str]:
    """line -> reason for every real `# market-default-ok:` COMMENT (not one inside a string)."""
    if MARKER not in source:
        return {}
    out: Dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            m = _MARKER_RE.search(tok.string)
            if m:
                out[tok.start[0]] = m.group("reason").strip()
    return out


def fingerprint(finding: Finding, index: int) -> str:
    """Stable across line moves: file, enclosing function or class, kind, shape, the code, the
    whitespace-normalised expression, and which occurrence of that exact expression it is."""
    raw = "\x1f".join([finding.file, finding.scope, finding.kind, finding.shape,
                       finding.literal, finding.expr, str(index)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def entries(findings: Iterable[Finding]) -> Dict[str, dict]:
    """fingerprint -> the baseline entry for it."""
    seen: Counter = Counter()
    out: Dict[str, dict] = {}
    for f in findings:
        key = (f.file, f.scope, f.kind, f.shape, f.literal, f.expr)
        index = seen[key]
        seen[key] += 1
        out[fingerprint(f, index)] = {
            "fingerprint": fingerprint(f, index), "file": f.file, "scope": f.scope, "kind": f.kind,
            "shape": f.shape, "literal": f.literal, "expr": f.expr[:200], "occurrence": index,
        }
    return out


class Scan(NamedTuple):
    files: Tuple[str, ...]
    findings: Tuple[Finding, ...]          # unmarked: these are compared with the baseline
    marked: Tuple[Tuple[Finding, str], ...]  # carrying a marker, with its reason
    stale_markers: Tuple[Tuple[str, int], ...]


def seed_routes() -> List[str]:
    names = sorted(p.name for p in (REPO / "routes").glob("*.py"))
    seeds = {f"routes/{n}" for n in names for g in ROUTE_SEED_GLOBS if fnmatch.fnmatch(f"routes/{n}", g)}
    seeds.update(ROUTE_SEED_FILES)
    return sorted(seeds)


@lru_cache(maxsize=1)
def scan_repo() -> Scan:
    parsed: Dict[str, Tuple[str, ast.Module]] = {}
    todo = [s for s in seed_routes() if (REPO / s).is_file()]
    while todo:
        rel = todo.pop()
        if rel in parsed:
            continue
        source = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        try:
            with warnings.catch_warnings():
                # A production file's own invalid-escape warning is not this test's to report.
                warnings.simplefilter("ignore")
                tree = ast.parse(source)
        except SyntaxError:
            continue
        parsed[rel] = (source, tree)
        for module in _imports(tree, rel):
            todo.extend(r for r in _resolve(module) if r not in parsed)

    files = tuple(sorted(rel for rel in parsed if rel not in EXCLUDED_FILES))
    # A constant is followed by name across files only when every scanned file that binds the
    # name binds it to the same code.
    bound: Dict[str, Set[str]] = {}
    for rel in files:
        for name, value in module_constants(parsed[rel][1]).items():
            bound.setdefault(name, set()).add(value)
    shared = {name: next(iter(values)) for name, values in bound.items() if len(values) == 1}

    findings: List[Finding] = []
    marked: List[Tuple[Finding, str]] = []
    stale: List[Tuple[str, int]] = []
    for rel in files:
        source, tree = parsed[rel]
        markers = _marker_lines(source)
        used: Set[int] = set()
        for f in scan_source(source, rel, shared, tree):
            lines = [n for n in range(f.line, f.end_line + 1) if n in markers]
            if lines:
                used.update(lines)
                marked.append((f, markers[lines[0]]))
            else:
                findings.append(f)
        stale.extend((rel, n) for n in sorted(set(markers) - used))
    return Scan(files, tuple(findings), tuple(marked), tuple(stale))


def load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def registry_currencies() -> Dict[str, str]:
    doc = json.loads(REGISTRY_TABLE.read_text(encoding="utf-8"))
    return {code: entry["currency"] for code, entry in doc["markets"].items()}


def parity_disagreements(findings: Iterable[Finding]) -> List[dict]:
    """Every (map, market) where a hand-kept map and the registry both know the market and name
    different currencies. One row per map per market."""
    registry = registry_currencies()
    out = []
    for f in findings:
        if f.kind != "c":
            continue
        _direction, _, rendered = f.literal.partition(" ")
        for pair in rendered.split(","):
            market, _, currency = pair.partition("=")
            if market in registry and registry[market] != currency:
                out.append({"file": f.file, "scope": f.scope, "market": market,
                            "map_currency": currency, "registry_currency": registry[market]})
    return sorted(out, key=lambda d: (d["file"], d["scope"], d["market"], d["map_currency"]))


def summary() -> str:
    scan = scan_repo()
    by_kind = Counter(f.kind for f in scan.findings)
    by_shape = Counter((f.kind, f.shape) for f in scan.findings)
    by_file = Counter(f.file for f in scan.findings)
    lines = [
        "market/currency defaults ratchet: %d files in scope (%d seed routes + what they import)"
        % (len(scan.files), len(seed_routes())),
        "  kind a (currency default): %d   kind b (market default): %d   kind c (market<->currency map): %d"
        % (by_kind["a"], by_kind["b"], by_kind["c"]),
        "  by shape: " + ", ".join(f"{k}/{s}={n}" for (k, s), n in sorted(by_shape.items())),
        "  marked `# %s:` (not in the baseline): %d" % (MARKER, len(scan.marked)),
        "  registry parity disagreements: %d" % len(parity_disagreements(scan.findings)),
        "  by file:",
    ]
    lines += ["    %4d  %s" % (n, rel) for rel, n in sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0]))]
    return "\n".join(lines)


def _describe(entry: dict, lines: Dict[str, int]) -> str:
    where = f"{entry['file']}:{lines[entry['fingerprint']]}" if entry["fingerprint"] in lines else entry["file"]
    return f"  [{entry['kind']}/{entry['shape']}] {where} in {entry['scope']}: {entry['expr']}"


# --------------------------------------------------------------------------------------------
# The ratchet
# --------------------------------------------------------------------------------------------

def test_the_summary_is_printed_on_every_run(capsys):
    with capsys.disabled():
        print("\n" + summary())


def test_no_new_market_or_currency_default_on_partner_reachable_code():
    scan = scan_repo()
    current = entries(scan.findings)
    lines = {fp: f.line for fp, f in zip(current, scan.findings)}
    baseline = {e["fingerprint"]: e for e in load_baseline()["findings"]}
    new = [current[fp] for fp in current if fp not in baseline]
    assert not new, (
        "%d silent market/currency default(s) on partner-reachable code are not in the baseline:\n" % len(new)
        + "\n".join(_describe(e, lines) for e in new)
        + "\n\nA market's price is in that market's currency; a missing market or currency is "
        "refused or carried as unknown, never filled with a literal. Resolve the market with "
        "services/request_market.py and read its currency from services/market_registry.py.\n"
        "If this default is genuinely right, mark its line `# %s: <reason>`.\n"
        "If the code only MOVED to another function or was re-worded, or a file with old defaults "
        "was newly imported by a partner route, replace / add the baseline entries by hand in "
        "tests/fixtures/market_currency_defaults_baseline.json (and say so in the PR):\n%s"
        % (MARKER, json.dumps(new, indent=2))
    )


def test_a_removed_default_leaves_the_baseline_in_the_same_change():
    scan = scan_repo()
    current = entries(scan.findings)
    gone = [e for e in load_baseline()["findings"] if e["fingerprint"] not in current]
    assert not gone, (
        "%d baseline entr(ies) no longer match any default in the code. Good -- now delete them from "
        "tests/fixtures/market_currency_defaults_baseline.json and lower BASELINE_COUNTS, so the slot "
        "cannot be reused. (When several identical expressions share a function and one is removed, "
        "the entry reported here is the one with the highest `occurrence`.)\n" % len(gone)
        + "\n".join(_describe(e, {}) for e in gone)
    )


def test_the_baseline_file_says_exactly_what_the_scan_says():
    """Every stored field is recomputed, so the file cannot describe a default it does not hold,
    and it is kept sorted so a diff shows only the lines that changed."""
    current = entries(scan_repo().findings)
    stored = load_baseline()["findings"]
    assert len({e["fingerprint"] for e in stored}) == len(stored), "duplicate fingerprints in the baseline"
    wrong = [e for e in stored if e["fingerprint"] in current and e != current[e["fingerprint"]]]
    assert not wrong, "baseline entries whose fields do not match the code:\n" + json.dumps(wrong, indent=2)
    keys = [(e["file"], e["scope"], e["kind"], e["shape"], e["expr"], e["occurrence"]) for e in stored]
    assert keys == sorted(keys), "keep the baseline sorted by file, scope, kind, shape, expr, occurrence"


def test_the_baseline_has_not_grown():
    counts = Counter(e["kind"] for e in load_baseline()["findings"])
    for kind in ("a", "b", "c"):
        assert counts[kind] <= BASELINE_COUNTS[kind], (
            "the baseline holds %d kind-%s entries, more than the %d pinned in BASELINE_COUNTS. "
            "The baseline only shrinks." % (counts[kind], kind, BASELINE_COUNTS[kind]))
        assert counts[kind] >= BASELINE_COUNTS[kind], (
            "the baseline holds %d kind-%s entries, fewer than the %d pinned in BASELINE_COUNTS. "
            "Lower the number in this file so the ratchet tightens behind you."
            % (counts[kind], kind, BASELINE_COUNTS[kind]))


def test_every_marker_sits_on_a_default_and_gives_a_reason():
    scan = scan_repo()
    assert not scan.stale_markers, (
        "`# %s` on a line that holds no default (remove the comment): %s" % (MARKER, list(scan.stale_markers)))
    thin = [(f.file, f.line, reason) for f, reason in scan.marked if len(reason) < _MIN_MARKER_REASON]
    assert not thin, "`# %s:` needs a reason a reviewer can judge: %s" % (MARKER, thin)


def test_registry_and_hand_kept_maps_agree_on_every_market_both_know():
    """The parity ratchet. A row here is a live inconsistency: two tables in one codebase name
    different currencies for one market. The list only shrinks, like the baseline above."""
    current = parity_disagreements(scan_repo().findings)
    stored = load_baseline()["parity_disagreements"]
    new = [d for d in current if d not in stored]
    gone = [d for d in stored if d not in current]
    assert not new, (
        "a hand-kept map disagrees with config/market_registry.json and the disagreement is not "
        "baselined. Read the currency from services/market_registry.py instead:\n" + json.dumps(new, indent=2))
    assert not gone, (
        "these disagreements are fixed -- delete them from `parity_disagreements` in the baseline:\n"
        + json.dumps(gone, indent=2))


# --------------------------------------------------------------------------------------------
# The scope is part of the ratchet
# --------------------------------------------------------------------------------------------

def test_every_seed_route_exists_and_the_walk_reaches_the_pricing_modules():
    """A renamed route must not drop out of scope silently, and a refactor that blinds the import
    walk (so it reaches almost nothing) must fail here, not pass with an empty scan."""
    missing = [s for s in ROUTE_SEED_FILES if not (REPO / s).is_file()]
    assert not missing, "seed routes that no longer exist (rename them here, do not drop them): %s" % missing
    for glob in ROUTE_SEED_GLOBS:
        assert any(fnmatch.fnmatch(s, glob) for s in seed_routes()), "seed glob matches nothing: %s" % glob
    files = set(scan_repo().files)
    for rel in ("routes/agent_shop_gateway.py", "routes/agent_v2.py", "routes/agent_api.py",
                "services/external_seed_search.py", "services/request_market.py", REGISTRY_MODULE):
        assert rel in files, "%s is no longer reached from the partner routes" % rel
    assert not any(rel.split("/")[0] in OUT_OF_SCOPE for rel in files)


def test_every_exclusion_is_a_reached_file_with_a_reason():
    for rel, reason in EXCLUDED_FILES.items():
        assert (REPO / rel).is_file(), "excluded file does not exist: %s" % rel
        assert len(reason) > 60, "an exclusion must explain WHY %s is not scanned" % rel
    assert REGISTRY_MODULE not in EXCLUDED_FILES, "the registry is scanned for kinds a and b like any module"
    assert REGISTRY_TABLE.is_file()


# --------------------------------------------------------------------------------------------
# The scanner's own floor: what it must see, what it must not
# --------------------------------------------------------------------------------------------

_MUST_SEE = [
    ("or-currency", 'c = row.get("currency") or "USD"', "a", "or"),
    ("or-market", 'm = payload.market or "US"', "b", "or"),
    ("or-lowercase-currency", 'c = (currency or "usd").upper()', "a", "or"),
    ("get-currency", 'c = row.get("currency", "USD")', "a", "get"),
    ("get-market", 'm = body.get("market", "US")', "b", "get"),
    ("getattr-market", 'm = getattr(req, "market", "US")', "b", "get"),
    ("param-currency", 'def f(currency: str = "USD"):\n    return currency', "a", "param"),
    ("param-market-kwonly", 'def f(*, market="US"):\n    return market', "b", "param"),
    ("field-currency", 'class M:\n    currency: str = "USD"', "a", "field"),
    ("field-wrapper", 'class M:\n    market: str = Field("US", max_length=2)', "b", "kwdefault"),
    ("query-default-keyword", 'def f(market: str = Query(default="US")):\n    return market', "b", "kwdefault"),
    ("const", 'DEFAULT_MARKET = "US"', "b", "const"),
    ("const-used-as-fallback", 'DEFAULT_MARKET = "US"\ndef f(m):\n    return m or DEFAULT_MARKET', "b", "or"),
    ("const-as-param", 'FALLBACK_CCY = "USD"\ndef f(ccy=FALLBACK_CCY):\n    return ccy', "a", "param"),
    ("ifnot", 'def f(currency):\n    if not currency:\n        currency = "USD"\n    return currency', "a", "ifnot"),
    ("if-none", 'def f(market):\n    if market is None:\n        market = "US"\n    return market', "b", "ifnot"),
    ("if-else", 'def f(row):\n    if row.currency:\n        currency = row.currency\n    else:\n'
                '        currency = "USD"\n    return currency', "a", "ifnot"),
    ("if-not-return", 'def f(m):\n    if not m:\n        return "US"\n    return m.upper()', "b", "ifnot"),
    ("ifexp", 'c = cur if cur else "USD"', "a", "ifexp"),
    ("init", 'def f(row):\n    currency = "USD"\n    if row:\n        currency = row.c\n    return currency', "a", "init"),
    ("sql-coalesce-currency", "q = \"SELECT COALESCE(o.currency, 'USD') FROM offers o\"", "a", "sql"),
    ("sql-coalesce-nullif", "q = \"SELECT COALESCE(NULLIF(price_currency, ''), 'USD') FROM s\"", "a", "sql"),
    ("sql-coalesce-market-fstring", "q = f\"SELECT COALESCE(market, 'US') FROM {table}\"", "b", "sql"),
    ("sql-ddl-default", "q = \"CREATE TABLE t (currency TEXT NOT NULL DEFAULT 'USD')\"", "a", "sql"),
    ("sql-case-else", "q = \"SELECT CASE WHEN market = 'SG' THEN 'SGD' ELSE 'USD' END\"", "a", "sql"),
    ("other-currency", 'c = currency or "SGD"', "a", "or"),
    ("word-code-with-context", 'c = currency or "TRY"', "a", "or"),
    ("state-code-with-context", 'm = country or "CA"', "b", "or"),
    ("map", 'M = {"US": "USD", "GB": "GBP"}', "c", "map"),
    ("map-nested", 'M = {"US": {"currency": "USD"}, "SG": {"currency": "SGD"}}', "c", "map"),
    ("map-dict-call", 'M = dict(US="USD", SG="SGD")', "c", "map"),
    ("map-reverse", 'M = {"USD": "US", "GBP": "GB"}', "c", "map"),
]

_MUST_NOT_SEE = [
    ("comment", '# currency = x or "USD"\nc = x'),
    ("docstring", 'def f():\n    """falls back: currency or "USD"; COALESCE(currency, \'USD\')"""\n    return 1'),
    ("comparison", 'ok = (currency or "").upper() == "USD"'),
    ("stated-value", 'row = {"currency": "USD", "market": "US"}'),
    ("keyword-argument", 'f(currency="USD", market="US")'),
    ("single-assignment", 'def f():\n    currency = "USD"\n    return currency'),
    ("word-code-without-context", 'mode = sort or "TOP"'),
    ("state-code-without-context", 'state = addr.state or "CA"'),
    ("language-default", 'lang = lang or "en"'),
    ("non-default-constant", 'SEED_PARTITION_MARKET = "US"'),
    ("one-pair-map", 'M = {"US": "USD"}'),
    ("marker-inside-a-string", 's = "# market-default-ok: not a comment"'),
]


@pytest.mark.parametrize("src,kind,shape", [c[1:] for c in _MUST_SEE], ids=[c[0] for c in _MUST_SEE])
def test_the_scan_sees_each_default_shape(src, kind, shape):
    hits = {(f.kind, f.shape) for f in scan_source(src, "routes/probe.py")}
    assert (kind, shape) in hits, "the scan is blind to this shape: %r (saw %s)" % (src, sorted(hits))


@pytest.mark.parametrize("src", [c[1] for c in _MUST_NOT_SEE], ids=[c[0] for c in _MUST_NOT_SEE])
def test_the_scan_does_not_fire_on_a_stated_value(src):
    assert scan_source(src, "routes/probe.py") == [], "false positive: %r" % src
    assert _marker_lines(src) == {}


def test_the_registry_module_may_hold_the_table_and_nothing_else_may():
    src = 'TABLE = {"US": "USD", "GB": "GBP"}\ndef f(m):\n    return m or "US"\n'
    assert [f.kind for f in scan_source(src, REGISTRY_MODULE)] == ["b"]
    assert sorted(f.kind for f in scan_source(src, "services/other.py")) == ["b", "c"]


def test_a_fingerprint_survives_a_line_move_and_a_rewrap_but_not_a_new_occurrence():
    one = 'def f(row):\n    return row.get("currency") or "USD"\n'
    moved = "\n" * 30 + 'def f(row):\n    x = 1\n    return (row.get("currency")\n            or "USD")\n'
    twice = 'def f(row):\n    a = row.get("currency") or "USD"\n    return row.get("currency") or "USD"\n'
    elsewhere = 'def g(row):\n    return row.get("currency") or "USD"\n'
    fp = lambda src: set(entries(scan_source(src, "routes/probe.py")))  # noqa: E731
    assert fp(one) == fp(moved) and len(fp(one)) == 1
    assert fp(one) < fp(twice) and len(fp(twice)) == 2
    assert not (fp(one) & fp(elsewhere))


def test_a_marker_needs_a_real_comment():
    marked = 'c = x or "USD"  # market-default-ok: the rail settles in USD by contract\n'
    assert _marker_lines(marked) == {1: "the rail settles in USD by contract"}
    assert _marker_lines('c = x or "USD"  # market-default-ok\n') == {1: ""}
