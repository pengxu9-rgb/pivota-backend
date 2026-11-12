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

SCOPE has a hand-kept part and a computed part. Hand-kept: the partner route files, named by
ROUTE_SEED_GLOBS and ROUTE_SEED_FILES. So that this list fails closed, EVERY other file in
routes/ is filed in NON_PARTNER_ROUTES under a reason, and a route file filed nowhere fails.
Computed: every repo module the seed routes import, transitively, including imports inside
functions. A new services/ or db/ module that a partner route imports is therefore scanned without
anyone adding it anywhere. jobs/ and scripts/ are out unless a route imports them. See
OUT_OF_SCOPE and EXCLUDED_FILES for what is not scanned and why.

A LEGITIMATE DEFAULT is marked on its own line, with a reason, and stays greppable:

    currency = row_currency or "USD"  # market-default-ok: <why this one is not a silent default>

REGENERATING. The baseline is meant to be edited DOWN by hand (delete the entry, lower the pin).
When code moved or a file entered scope and entries must be replaced, this rewrites the file in
its one canonical form (sorted, no line numbers, hand-written reasons kept), byte-identically
however often it is run:

    python tests/test_market_currency_defaults_ratchet.py --write-baseline

It REFUSES (exit 1, nothing written) when the code holds a default the baseline does not. With
--accept-new it writes them, and each added entry is either `moved` (it could be paired with a
removed entry of the same file, kind, shape and literal, whose fingerprint it names) or carries
an empty `reason` that a test rejects until a person fills it in. Entries from the day the
ratchet landed carry `origin` instead and are counted by GRANDFATHERED_COUNT. The command never
touches a pin: raising one is a separate, visible edit in this file.

FINGERPRINTS are built from the syntax tree written back out (ast.unparse), so a line move, a
re-wrap, a trailing comma, the quote style or a comment inside the expression do not change
them. Renaming a variable inside the expression, or moving it to another function, does.

Marked findings are not compared with the baseline; they are counted in the summary this file
prints on every run. The repository starts with zero markers.

WHAT THIS CANNOT SEE (stated limits; each was checked and is NOT detected):
  * a default computed at runtime (`or some_function()`), set by an environment variable or a
    config file, or behind `importlib.import_module`; anything in the gateway repository;
  * a default in jobs/ or scripts/ that no seed route imports, and in the NON_PARTNER_ROUTES files;
  * context taken from the assignment target: `currency = value or "usd"` and `country = cc or
    "DE"` are missed, because a lower-case or ambiguous code counts only when the EXPRESSION names
    a currency or a market, and the name on the left is not read;
  * an aliased constant import: `from m import DEFAULT_CCY as CCY` then `x or CCY` (the binding is
    seen, the use is not); and a constant bound to different codes in different files;
  * SQL: a cast (`COALESCE(currency, 'USD'::text)`), SQLAlchemy `func.coalesce(col, "USD")`, a
    statement assembled by string concatenation, and a bound parameter (`COALESCE(currency, :c)`);
  * `next(iterator, "USD")`, `defaultdict(lambda: "USD")`, `Field(default_factory=lambda: "USD")`,
    and an unannotated class attribute `currency = "USD"`;
  * a fall-through return (`if m: return m` then `return "US"`), and a code assigned in an
    `except:` branch when the name is assigned only once;
  * a map written as a list of pairs (`[("US", "USD"), ...]`) or built by `M["DE"] = "EUR"`;
  * markers are per LINE: one `# market-default-ok:` covers every default on that line.
It counts places where a literal stands in for a missing value; it does not prove a price is in
the buyer's currency.
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

#: EVERY other file in routes/, filed under the reason it is not a partner door. The seeds above
#: are a hand-kept list, so this is what makes them fail closed: a route file that is in neither
#: place fails test_every_route_file_is_filed_as_a_partner_door_or_not, and so does one that is in
#: both. A pattern with `*` files a whole family; a plain name files one file and must exist.
#: Being listed here does NOT mean "never scanned": a file a seed route imports is scanned anyway.
NON_PARTNER_ROUTES: Dict[str, Tuple[str, ...]] = {
    "admin console and one-off maintenance endpoints, behind the admin key or an employee login; "
    "no partner credential reaches them": (
        "admin_*", "ap2_agent_registration.py", "ap2_trusted_issuers_admin.py", "funnel_metrics_routes.py",
        "payment_grant_issuers.py", "substantiation_admin_routes.py",
    ),
    "staff (employee login) tools: review queues, finance, governance and dashboards. "
    "employee_products.py is the one staff route among the seeds, so this family is named file by file": (
        "employee_agent_mgmt.py", "employee_content.py", "employee_dashboard_routes.py", "employee_finance.py",
        "employee_kb_monitoring.py", "employee_missing_endpoints.py", "employee_payouts.py",
        "employee_pdp_governance.py", "employee_reviews.py", "employee_routing_dashboard.py",
        "employee_settlement_rules.py", "employee_store_psp_fixes.py", "employees_security.py",
        "channel_graph_routes.py", "questions_api.py", "routing_governance.py",
    ),
    "merchant portal and dashboard, behind a merchant or dashboard-user login: a merchant looking "
    "at its own store, audits, billing, payouts, PSPs and settings": (
        "merchant_*", "psp_*", "store_audit_*", "audit_runs_routes.py", "auth.py", "auth_routes.py",
        "billing_routes.py", "brand_claim_routes.py", "catalog_routes.py", "citation_operator_routes.py",
        "dashboard_api.py", "gsc_oauth_routes.py", "manage_integrations.py", "mcp_mgmt.py",
        "operations_routes.py", "partner_invite_redeem.py", "payment_routing_routes.py", "pivot_routes.py",
        "platform_onboarding_routes.py", "protocol_routes.py", "store_readiness.py",
    ),
    "commerce-platform integrations: inbound webhooks and product sync from a store platform. "
    "They write catalogue and order data; no partner request enters by them": (
        "shopify_*", "bigcommerce_webhooks.py", "cafe24_webhooks.py", "prestashop_webhooks.py",
        "shopline_family_webhooks.py", "squarespace_webhooks.py", "webflow_webhooks.py", "wix_webhooks.py",
        "woocommerce_webhooks.py", "adobe_commerce_events.py", "cafe24_integration.py", "magento_integration.py",
        "platform_connector_prefix.py", "platform_products_sync_api.py", "product_enrichment_routes.py",
        "product_quality_routes.py", "product_sync.py", "product_sync_monitoring.py", "refund_webhook_routes.py",
        "sfcc_events.py", "sfcc_integration.py", "shopline_integrations.py", "sync_all_platforms.py",
        "universal_product_sync.py", "webhook_routes.py", "wix_sync.py",
    ),
    "operator endpoints: debug views, bootstrap and data-repair scripts exposed as routes, health, "
    "metrics, queues and internal probes": (
        "debug_*", "fix_*", "init_*", "ops_*", "__catalog_health.py", "__trust_health.py", "cleanup_test_data.py",
        "crawl_identity.py", "demo_data_routes.py", "direct_db_check.py", "mcp_e2e_test.py",
        "migrate_employees_password.py", "performance_optimization.py", "pool_health.py",
        "products_cache_maintenance.py", "prometheus_metrics.py", "queue_routes.py", "quick_index_setup.py",
        "readiness_internal.py", "scheduler_health.py", "shakeout_debug.py", "simple_test_orders.py",
    ),
    "agent-, buyer- or protocol-facing routes that are NOT among the seeds: kept out to hold the "
    "scope reviewed on 2026-11-12, when each held no finding. A default added to one of them is not "
    "seen unless a seed imports the file. These are the first candidates to move to the seeds": (
        "after_sales_cases.py", "ap2_routes.py", "buyer_api.py", "buyer_reviews.py", "fulfillment_api.py",
        "mcp_oauth_as.py", "photos.py", "pivota_canonical_routes.py", "reviews_invitation_issuer.py",
        "reviews_invitation_shortlink.py", "reviews_proof_issuer.py", "subject_resolve.py",
    ),
}

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

#: Entries that were already in the code when the ratchet landed carry `"origin": "2026-11-12"`
#: and need no reason. Every entry added later must say why (`reason`) or name the entry it
#: replaces (`moved`). This is how many grandfathered entries are left. ONLY EVER LOWER THIS.
GRANDFATHER_ORIGIN = "2026-11-12"
GRANDFATHERED_COUNT = 278
#: The fingerprints of those 278, frozen. An entry may say `origin` only if its fingerprint is in
#: this file, and the file is pinned by its sha256, so nobody can grandfather a new default by
#: typing `origin` on it. NEVER EDIT THE FILE OR THIS DIGEST: a removed default simply stops
#: being in the baseline; its line stays here.
GRANDFATHERED_PATH = REPO / "tests" / "fixtures" / "market_currency_defaults_grandfathered.txt"
GRANDFATHERED_SHA256 = "c5429d7be47b0a52f221780c001699eea7b796d83b77763988014974a8f6b818"
_PROVENANCE_KEYS = ("origin", "moved", "reason")
_MIN_ENTRY_REASON = 40

#: How many (map, market) pairs name a market the registry does not list. ONLY EVER LOWER THIS.
UNKNOWN_MAP_MARKETS_COUNT = 10
_MIN_UNKNOWN_REASON = 40

MARKER = "market-default-ok"
_MARKER_RE = re.compile(r"#.*?\b" + MARKER + r"\b:?\s*(?P<reason>.*)$")
_MIN_MARKER_REASON = 12

# --------------------------------------------------------------------------------------------
# What counts as a currency code or a market code
# --------------------------------------------------------------------------------------------

#: The code lists are not typed here: they are read from a checked-in reference file that names
#: where each list was taken from and when (tests/fixtures/iso_codes_reference.json).
ISO_REFERENCE_PATH = REPO / "tests" / "fixtures" / "iso_codes_reference.json"
_ISO_REFERENCE = json.loads(ISO_REFERENCE_PATH.read_text(encoding="utf-8"))
#: The currency list is CLDR's (what ICU ships), NOT the ISO 4217 table: it is the right list to
#: DETECT a currency literal with, because it is wider (it keeps withdrawn codes).
_CURRENCIES = frozenset(_ISO_REFERENCE["cldr_currency_codes"]["codes"].split())
#: Codes in that list that no market may be registered with: withdrawn, or not a national currency.
_NOT_A_MARKET_CURRENCY = frozenset(
    (_ISO_REFERENCE["not_valid_for_a_market"]["withdrawn"] + " "
     + _ISO_REFERENCE["not_valid_for_a_market"]["not_a_national_currency"]).split())
#: Codes that are also ordinary words or keywords. They count only where the expression itself
#: names a currency (`sort or "TOP"` is not a currency default; `currency or "TRY"` is).
_CURRENCY_WORDS = frozenset("ALL ANG BAM BOB COP CUP GEL GIP LAK MAD MOP PEN PHP RON SOS TOP TRY".split())

_ISO_MARKETS = frozenset(_ISO_REFERENCE["iso_3166_1_alpha_2"]["codes"].split())
#: NOT ISO 3166-1 alpha-2 country codes, but written as markets in this codebase, so the scan must
#: see them: UK (the ISO code for the United Kingdom is GB) and EU (a union, not a country).
NON_ISO_MARKET_ALIASES = frozenset({"UK", "EU"})
_MARKETS = _ISO_MARKETS | NON_ISO_MARKET_ALIASES
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
    def text(node: ast.AST) -> str:
        # The syntax tree written back out, not the source characters: quote style, a trailing
        # comma, redundant parentheses, line wraps and comments inside the expression all vanish,
        # so none of them can change a fingerprint or lend a comment's words to the context.
        return ast.unparse(node)

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
    unparseable: Tuple[str, ...]           # reached by the walk but not valid Python: never skipped quietly


def seed_routes() -> List[str]:
    names = sorted(p.name for p in (REPO / "routes").glob("*.py"))
    seeds = {f"routes/{n}" for n in names for g in ROUTE_SEED_GLOBS if fnmatch.fnmatch(f"routes/{n}", g)}
    seeds.update(ROUTE_SEED_FILES)
    return sorted(seeds)


@lru_cache(maxsize=1)
def scan_repo() -> Scan:
    parsed: Dict[str, Tuple[str, ast.Module]] = {}
    unparseable: Set[str] = set()
    todo = [s for s in seed_routes() if (REPO / s).is_file()]
    while todo:
        rel = todo.pop()
        if rel in parsed or rel in unparseable:
            continue
        source = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        try:
            with warnings.catch_warnings():
                # A production file's own invalid-escape warning is not this test's to report.
                warnings.simplefilter("ignore")
                tree = ast.parse(source)
        except SyntaxError:
            unparseable.add(rel)
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
    return Scan(files, tuple(findings), tuple(marked), tuple(stale), tuple(sorted(unparseable)))


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


def unknown_map_markets(findings: Iterable[Finding]) -> List[dict]:
    """Every (map, market) where a hand-kept map names a market the registry does not list at
    all. Parity cannot judge these (there is no registry currency to compare), so each one is
    listed on its own: the map is deciding a currency for a market nobody registered."""
    registry = registry_currencies()
    out = []
    for f in findings:
        if f.kind != "c":
            continue
        _direction, _, rendered = f.literal.partition(" ")
        for pair in rendered.split(","):
            market, _, currency = pair.partition("=")
            if market not in registry:
                out.append({"file": f.file, "scope": f.scope, "market": market, "map_currency": currency,
                            "iso_3166_1_alpha_2": market in _ISO_MARKETS})
    return sorted(out, key=_unknown_key)


def _unknown_key(row: dict) -> Tuple[str, str, str, str]:
    return (row["file"], row["scope"], row["market"], row["map_currency"])


def _finding_key(entry: dict) -> Tuple:
    return (entry["file"], entry["scope"], entry["kind"], entry["shape"], entry["expr"],
            entry["occurrence"], entry["literal"], entry["fingerprint"])


_ENTRY_KEYS = ("fingerprint", "file", "scope", "kind", "shape", "literal", "expr", "occurrence") + _PROVENANCE_KEYS


def _ordered_entry(entry: dict) -> dict:
    """One key order for every entry, so a hand-added `reason` lands where the writer puts it."""
    unknown = set(entry) - set(_ENTRY_KEYS)
    if unknown:
        raise ValueError("unknown baseline entry field(s): %s" % sorted(unknown))
    return {k: entry[k] for k in _ENTRY_KEYS if k in entry}


def _bare(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in _PROVENANCE_KEYS}


def serialise_baseline(doc: dict) -> str:
    """The one way the baseline file is written: fixed key order, entries sorted by a key with
    no line number in it, one-space indent, ASCII, trailing newline. Writing the same content
    twice, or after code moved inside a file, gives the same bytes."""
    canonical = {
        "_doc": doc["_doc"],
        "version": doc["version"],
        "findings": sorted((_ordered_entry(e) for e in doc["findings"]), key=_finding_key),
        "parity_disagreements": sorted(
            doc["parity_disagreements"], key=lambda d: (d["file"], d["scope"], d["market"], d["map_currency"])),
        "map_markets_unknown_to_registry": sorted(doc["map_markets_unknown_to_registry"], key=_unknown_key),
    }
    return json.dumps(canonical, indent=1, ensure_ascii=True) + "\n"


def plan_baseline() -> Tuple[dict, List[dict]]:
    """(the baseline for the code as it is now, the entries it would ADD to the file on disk).

    An entry already in the file keeps its provenance (`origin`, `moved`, `reason`). An added
    entry is paired with a removed one when the two share file, kind, shape and literal (a map:
    file and scope): that is a default that moved or was re-worded, and it gets
    `"moved": "<the old fingerprint>"`. Any other added entry gets `"reason": ""`, which fails
    test_every_entry_added_after_the_ratchet_landed_says_why until a person writes the reason.
    Reasons for unknown map markets are carried over the same way."""
    scan = scan_repo()
    previous = load_baseline() if BASELINE_PATH.is_file() else {}
    old = {e["fingerprint"]: e for e in previous.get("findings", [])}
    current = entries(scan.findings)
    freed = sorted((e for fp, e in old.items() if fp not in current), key=_finding_key)

    def move_key(e: dict) -> Tuple:
        return (e["file"], e["scope"], "c") if e["kind"] == "c" else (e["file"], e["kind"], e["shape"], e["literal"])

    findings, added = [], []
    for fp, entry in current.items():
        entry = dict(entry)
        if fp in old:
            entry.update({k: old[fp][k] for k in _PROVENANCE_KEYS if k in old[fp]})
        else:
            source = next((e for e in freed if move_key(e) == move_key(entry)), None)
            if source is not None:
                freed.remove(source)
                entry["moved"] = source["fingerprint"]
            else:
                entry["reason"] = ""
            added.append(entry)
        findings.append(entry)
    reasons = {_unknown_key(r): r.get("reason", "") for r in previous.get("map_markets_unknown_to_registry", [])}
    unknown = [dict(r, reason=reasons.get(_unknown_key(r), "")) for r in unknown_map_markets(scan.findings)]
    doc = {
        "_doc": previous.get("_doc") or _BASELINE_DOC,
        "version": 1,
        "findings": findings,
        "parity_disagreements": parity_disagreements(scan.findings),
        "map_markets_unknown_to_registry": unknown,
    }
    return doc, added


def render_baseline() -> str:
    return serialise_baseline(plan_baseline()[0])


_BASELINE_DOC = (
    "Every silent market/currency default on partner-reachable code on the day the ratchet was added. "
    "ONLY EVER DELETE ENTRIES. See tests/test_market_currency_defaults_ratchet.py.")


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
        "  map markets the registry does not list: %d" % len(unknown_map_markets(scan.findings)),
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
    wrong = [e for e in stored if e["fingerprint"] in current and _bare(e) != current[e["fingerprint"]]]
    assert not wrong, "baseline entries whose fields do not match the code:\n" + json.dumps(wrong, indent=2)


def test_the_baseline_file_is_in_its_one_canonical_form():
    """Sorted by a key that holds no line number, fixed formatting. So a hand edit that deletes
    an entry is a diff of that entry only, and --write-baseline never reorders the rest."""
    assert BASELINE_PATH.read_text(encoding="utf-8") == serialise_baseline(load_baseline()), (
        "tests/fixtures/market_currency_defaults_baseline.json is not in canonical form (sorted by "
        "file, scope, kind, shape, expr, occurrence; indent 1; trailing newline). Fix the order by "
        "hand, or run: python tests/test_market_currency_defaults_ratchet.py --write-baseline")


def test_the_repo_scan_runs_once_per_process_and_probes_are_never_cached():
    """The repo scan parses ~570 files, so it is cached for the life of the process; every test
    above reads the same result. scan_source is the uncached, pure entry point: the shape probes
    below, and anything that needs a fresh look at changed text, call it directly."""
    assert scan_repo() is scan_repo()
    assert scan_repo.cache_info().currsize == 1 and scan_repo.cache_info().misses == 1
    assert not hasattr(scan_source, "cache_info")
    before = scan_source('c = x or "USD"', "routes/probe.py")
    after = scan_source('c = x', "routes/probe.py")
    assert len(before) == 1 and after == []


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


def test_every_entry_added_after_the_ratchet_landed_says_why():
    """Without this, deleting one default and adding another of the same kind somewhere else, then
    regenerating, would pass with nothing said. The 278 entries that were in the code on the day
    the ratchet landed are marked `origin` and counted; every other entry must carry a reason a
    reviewer can judge, or `moved` naming the fingerprint of the entry it replaced."""
    stored = load_baseline()["findings"]
    present = {e["fingerprint"] for e in stored}
    grandfathered = [e for e in stored if "origin" in e]
    frozen_text = GRANDFATHERED_PATH.read_text(encoding="utf-8")
    assert hashlib.sha256(frozen_text.encode("utf-8")).hexdigest() == GRANDFATHERED_SHA256, (
        "tests/fixtures/market_currency_defaults_grandfathered.txt was edited. It is the frozen list of "
        "the defaults that existed on %s and is never changed." % GRANDFATHER_ORIGIN)
    forged = [e for e in grandfathered if e["fingerprint"] not in set(frozen_text.split())]
    assert not forged, (
        "these entries say `origin` but were not in the code on %s. A new entry needs a `reason`:\n%s"
        % (GRANDFATHER_ORIGIN, json.dumps(forged, indent=2)))
    assert all(e["origin"] == GRANDFATHER_ORIGIN for e in grandfathered), "the only origin is %s" % GRANDFATHER_ORIGIN
    assert len(grandfathered) == GRANDFATHERED_COUNT, (
        "the baseline holds %d grandfathered entries; GRANDFATHERED_COUNT pins %d. A grandfathered "
        "entry can only be deleted (then lower the pin); a new entry is never grandfathered -- "
        "give it a `reason`." % (len(grandfathered), GRANDFATHERED_COUNT))
    unexplained, bad_moves = [], []
    moved_from: Counter = Counter()
    for e in stored:
        if "origin" in e:
            continue
        if "moved" in e:
            moved_from[e["moved"]] += 1
            if not re.fullmatch(r"[0-9a-f]{16}", str(e["moved"])) or e["moved"] in present:
                bad_moves.append(e)
        elif len(str(e.get("reason", "")).strip()) < _MIN_ENTRY_REASON:
            unexplained.append(e)
    assert not unexplained, (
        "baseline entries added after %s with no reason. Remove the default, mark its line "
        "`# %s: <reason>`, or write a `reason` of at least %d characters on the entry:\n%s"
        % (GRANDFATHER_ORIGIN, MARKER, _MIN_ENTRY_REASON, json.dumps(unexplained, indent=2)))
    assert not bad_moves, (
        "`moved` must name the 16-hex fingerprint of an entry that is no longer in the baseline:\n"
        + json.dumps(bad_moves, indent=2))
    twice = sorted(fp for fp, n in moved_from.items() if n > 1)
    assert not twice, "one removed entry cannot be the source of two moved entries: %s" % twice


def test_no_file_in_scope_is_skipped_because_it_does_not_parse():
    """A file that is not valid Python cannot be scanned, and neither can anything reached only
    through it. That must be a failure with the file's name, never a smaller scope."""
    assert not scan_repo().unparseable, (
        "reached by the partner-route import walk but not parseable, so not scanned: %s"
        % list(scan_repo().unparseable))


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


def test_map_markets_the_registry_does_not_know_are_each_registered():
    """Parity above can only compare markets BOTH tables know. A market that only a hand-kept map
    knows is worse, not better: the map alone decides its currency. Each one is registered in the
    baseline with a reason, the set only shrinks, and a new one fails."""
    current = unknown_map_markets(scan_repo().findings)
    stored = load_baseline()["map_markets_unknown_to_registry"]
    bare = [{k: v for k, v in row.items() if k != "reason"} for row in stored]
    new = [row for row in current if row not in bare]
    gone = [row for row in bare if row not in current]
    assert not new, (
        "a hand-kept map names a market config/market_registry.json does not list. Add the market "
        "to the registry (and read its currency from services/market_registry.py), or drop it "
        "from the map:\n" + json.dumps(new, indent=2))
    assert not gone, (
        "these markets are no longer in a hand-kept map, or the registry now lists them -- delete "
        "them from `map_markets_unknown_to_registry` in the baseline and lower "
        "UNKNOWN_MAP_MARKETS_COUNT:\n" + json.dumps(gone, indent=2))
    assert len(stored) == UNKNOWN_MAP_MARKETS_COUNT, (
        "the baseline registers %d unknown map markets; UNKNOWN_MAP_MARKETS_COUNT pins %d. The set "
        "only shrinks: lower the pin when an entry is deleted, never raise it."
        % (len(stored), UNKNOWN_MAP_MARKETS_COUNT))
    thin = [row for row in stored if len(str(row.get("reason", "")).strip()) < _MIN_UNKNOWN_REASON]
    assert not thin, "each unknown map market needs a reason a reviewer can judge:\n" + json.dumps(thin, indent=2)
    for row in stored:
        assert row["iso_3166_1_alpha_2"] == (row["market"] in _ISO_MARKETS)
        if row["market"] in NON_ISO_MARKET_ALIASES:
            assert "not an ISO 3166-1 alpha-2" in row["reason"], (
                "%s is not an ISO 3166-1 alpha-2 country code; its reason must say so" % row["market"])


def test_the_code_lists_are_the_reference_file_and_the_file_says_where_it_came_from():
    """The scanner's lists ARE the checked-in reference (read above, not retyped), so this pins
    the reference itself: its shape, its size, its stated source, and the two aliases."""
    for name, width in (("iso_3166_1_alpha_2", 2), ("cldr_currency_codes", 3)):
        block = _ISO_REFERENCE[name]
        codes = block["codes"].split()
        assert codes == sorted(set(codes)) and len(codes) == block["count"], name
        assert all(len(c) == width and c.isalpha() and c.isupper() for c in codes), name
        assert len(block["source"]) > 60, "%s must say where the list was taken from" % name
    assert _ISO_MARKETS == frozenset(_ISO_REFERENCE["iso_3166_1_alpha_2"]["codes"].split()) and len(_ISO_MARKETS) == 249
    assert _CURRENCIES == frozenset(_ISO_REFERENCE["cldr_currency_codes"]["codes"].split()) and len(_CURRENCIES) == 162
    assert "iso_4217" not in _ISO_REFERENCE, "the currency list is CLDR's; do not label it as the ISO 4217 table"
    assert "GB" in _ISO_MARKETS and not (NON_ISO_MARKET_ALIASES & _ISO_MARKETS)
    assert _MARKETS == _ISO_MARKETS | {"UK", "EU"}
    assert _MARKETS_ON_SIGHT <= _MARKETS and _CURRENCY_WORDS <= _CURRENCIES
    assert set(registry_currencies()) <= _ISO_MARKETS
    # Detection uses the wide list; VALIDITY does not: a market registered with a withdrawn code
    # (the list still holds HRK) or a non-national one (XDR) must fail here.
    assert _NOT_A_MARKET_CURRENCY <= _CURRENCIES and {"HRK", "SLL", "ZWL", "CUC"} <= _NOT_A_MARKET_CURRENCY
    assert len(_ISO_REFERENCE["not_valid_for_a_market"]["source"]) > 60
    invalid = {m: c for m, c in registry_currencies().items() if c not in _CURRENCIES - _NOT_A_MARKET_CURRENCY}
    assert not invalid, "registry markets priced in a withdrawn or unknown currency code: %s" % invalid


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


def test_every_route_file_is_filed_as_a_partner_door_or_not():
    """The seeds are a hand-kept list; this is what makes the list fail closed. Every routes/*.py
    is a seed or sits under exactly one NON_PARTNER_ROUTES reason. A new route file fails here
    until someone decides which it is -- and deciding "not a partner door" is a line in this diff."""
    names = sorted(p.name for p in (REPO / "routes").glob("*.py") if p.name != "__init__.py")
    seeds = {s.split("/", 1)[1] for s in seed_routes()}
    filed: Dict[str, List[str]] = {n: [] for n in names}
    for reason, patterns in NON_PARTNER_ROUTES.items():
        assert len(reason) > 60, "a non-partner group must say WHY its routes are not partner doors: %r" % reason
        assert len(set(patterns)) == len(patterns), "a pattern is listed twice under: %r" % reason
        for pattern in patterns:
            hits = [n for n in names if fnmatch.fnmatch(n, pattern)]
            assert hits, "NON_PARTNER_ROUTES names %r, which matches no file in routes/" % pattern
            for n in hits:
                filed[n].append(reason[:40])
    missing_seeds = sorted(seeds - set(names))
    assert not missing_seeds, "seed routes that do not exist: %s" % missing_seeds
    unfiled = [n for n in names if n not in seeds and not filed[n]]
    assert not unfiled, (
        "route file(s) filed nowhere: %s. If a partner's request can enter by it, add it to "
        "ROUTE_SEED_FILES (it is then scanned); if not, add it to NON_PARTNER_ROUTES under the "
        "reason that fits." % unfiled)
    both = [n for n in names if n in seeds and filed[n]]
    assert not both, "route file(s) filed as a partner door AND as not one: %s" % both
    twice = {n: groups for n, groups in filed.items() if len(groups) > 1}
    assert not twice, "route file(s) under more than one NON_PARTNER_ROUTES reason: %s" % twice


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


def test_a_fingerprint_survives_a_trailing_comma_the_quote_style_and_an_inner_comment():
    plain = 'def f(row):\n    return pick(row.get("currency", "USD"), row)\n'
    comma = 'def f(row):\n    return pick(\n        row.get("currency", "USD"),\n        row,\n    )\n'
    quotes = "def f(row):\n    return pick(row.get('currency', 'USD'), row)\n"
    comment = ('def f(row):\n    return pick(\n        row.get(  # the currency, or else\n'
               '            "currency", "USD"),\n        row)\n')
    parens = 'def f(row):\n    return pick((row.get("currency", ("USD"))), row)\n'
    renamed = 'def f(row):\n    return pick(other.get("currency", "USD"), row)\n'
    fp = lambda src: set(entries(scan_source(src, "routes/probe.py")))  # noqa: E731
    assert len(fp(plain)) == 1
    assert fp(plain) == fp(comma) == fp(quotes) == fp(comment) == fp(parens)
    assert fp(plain) != fp(renamed), "a different expression is a different default"


def test_a_marker_needs_a_real_comment():
    marked = 'c = x or "USD"  # market-default-ok: the rail settles in USD by contract\n'
    assert _marker_lines(marked) == {1: "the rail settles in USD by contract"}
    assert _marker_lines('c = x or "USD"  # market-default-ok\n') == {1: ""}


if __name__ == "__main__":
    import sys

    _args = sys.argv[1:]
    if not _args or _args[0] != "--write-baseline" or set(_args[1:]) - {"--accept-new"}:
        raise SystemExit("usage: python tests/test_market_currency_defaults_ratchet.py --write-baseline [--accept-new]")
    _doc, _added = plan_baseline()
    if _added and "--accept-new" not in _args:
        print("NOT WRITTEN. The code holds %d default(s) the baseline does not:" % len(_added))
        for _e in _added:
            print("  [%s/%s] %s in %s: %s" % (_e["kind"], _e["shape"], _e["file"], _e["scope"], _e["expr"]))
        print("The baseline only shrinks. Remove them or mark them `# %s: <reason>`. To register them "
              "anyway, re-run with --accept-new, then write a `reason` on every entry it adds with an "
              "empty one (an entry it could pair with a removed one is written as `moved`)." % MARKER)
        raise SystemExit(1)
    BASELINE_PATH.write_text(serialise_baseline(_doc), encoding="utf-8")
    for _e in _added:
        print("added %s %s (%s)" % (_e["fingerprint"], _e["file"],
                                    "moved from " + _e["moved"] if "moved" in _e else "NEEDS A REASON"))
    print(summary())
