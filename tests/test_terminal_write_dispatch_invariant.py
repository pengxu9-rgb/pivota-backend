"""THE INVARIANT the gateway's terminal copy rests on (the shape exemption on `refused` / `expired`).

Every write that ends a purchase `refused` or `expired`, on either rail, either
  * happens where NO checkout or order can exist yet (no dispatch evidence), or
  * writes one of POST_DISPATCH_CODES, the codes the gateway lists by name.

So a hold-SHAPED code the gateway does not list can sit on a refused / expired row only where nothing
was dispatched, and the state's own words are true there. A new refused / expired write after a
dispatch fails this test until its code is added here AND to the gateway's lists.

ENUMERATED FROM THE SOURCE, not listed by hand:
  * every call to a ledger writer (`transition`, `transition_as_holder`, the Reap service's `_move`)
    in every non-test module, by AST;
  * every SQL string constant in every non-test module whose `SET state = ...` names refused or expired.
A write site whose target or code cannot be read off the source is a failure, except the two dynamic
sites resolved below FROM THEIR OWN SOURCE (the VIC authorization ending, the manual checkout close).

"No dispatch evidence", per rail, as the code establishes it (each fact is pinned below):
  * A2A: no order exists before `awaiting_authorization` (the rail's order is adopted only on the edge
    into it) and the merchant checkout task is recorded only in `paying`. The bulk sweep may also end
    `awaiting_authorization`, but only with no pending write (a VIC order keeps its marker write for
    its whole wait) and no money result seen.
  * Reap: a checkout can exist only once `continuation.begin_dispatch` set the fence (in `quoting`).
    Every refusal from `quoting` precedes any dispatching call in its function, and `advance` parks
    a `quoting` row with any dispatch evidence before it runs a quoting step. The bulk sweep may end
    `awaiting_approval`, but only with no checkout id, no dispatch fence and version-1 tracking (a
    legacy row's missing id is unknown, never negative proof).
"""

from __future__ import annotations

import ast
import functools
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = Path(__file__).resolve().parents[1]

ENDINGS = frozenset({"refused", "expired"})
#: The only codes a refused / expired write may carry where a checkout or order can exist. Each is on a
#: gateway list by name (the gateway's terminal-code parity test).
POST_DISPATCH_CODES = frozenset({"checkout_expired", "manual_provider_expired", "vic_authorization_expired"})
WRITERS = frozenset({"transition", "transition_as_holder", "_move"})
#: The states in which no checkout or order can exist yet (for 'quoting', see the structural tests).
PRE_DISPATCH = {
    "a2a": frozenset({"opened", "needs_payment_method", "quoting"}),
    "reap": frozenset({"resolving", "needs_enrollment", "quoting"}),
}
#: What a bulk SQL ending must require, for each dispatch-capable state it may end, to prove there is no
#: dispatch evidence on the row it ends. Each must appear once per `state IN (` (the outer re-check and
#: the candidate subquery alike).
NO_EVIDENCE = {
    "a2a": {"awaiting_authorization": ("pending_write IS NULL", "NOT LIKE '%money_result_seen%'")},
    "reap": {"awaiting_approval": ("reap_checkout_id IS NULL", "checkout_dispatch_key IS NULL",
                                   "dispatch_tracking_version = 1")},
}
_SKIP_DIRS = {"tests", "node_modules", "venv", "__pycache__"}


@functools.lru_cache(maxsize=4)
def _parsed(repo: Path) -> Tuple[Tuple[str, ast.Module], ...]:
    out = []
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        try:
            out.append((str(rel), ast.parse(path.read_text(encoding="utf-8"))))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return tuple(out)


def _modules() -> Iterator[Tuple[str, ast.Module]]:
    """Every non-test module of the repo, parsed (once per run)."""
    return iter(_parsed(REPO))


def _rail(path: str) -> str:
    name = Path(path).name
    if "a2a" in name:
        return "a2a"
    if "reap" in name:
        return "reap"
    raise AssertionError(f"{path}: a refused/expired purchase write in a module of no known rail")


def _module_name(path: str) -> str:
    return path[:-3].replace("/", ".")


def _functions(tree: ast.Module) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _call_name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _kw(call: ast.Call, name: str) -> Optional[ast.AST]:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _target(call: ast.Call) -> Optional[ast.AST]:
    if _call_name(call) == "_move" and len(call.args) > 3:
        return call.args[3]
    return _kw(call, "to_state")


def _sources(call: ast.Call) -> Optional[ast.AST]:
    if _call_name(call) == "_move" and len(call.args) > 2:
        return call.args[2]
    return _kw(call, "from_states")


def _literal_states(node: Optional[ast.AST]) -> Optional[Set[str]]:
    """The from_states a call names literally, or None (then they are unknown: dispatch-capable)."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return {e.value for e in node.elts}
    return None


def _resolve_code(path: str, node: Optional[ast.AST]) -> Optional[str]:
    """A code expression's value when the source fixes it: a literal, or a module constant (by import)."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        module = importlib.import_module(_module_name(path))
        try:
            value = eval(compile(ast.Expression(node), path, "eval"), vars(module))  # a name, nothing else
        except Exception:  # noqa: BLE001
            return None
        return value if isinstance(value, str) else None
    return None


# ── the two dynamic sites, resolved from their own source ───────────────────────────────────


def _vic_endings(fn: ast.AST, call: ast.Call) -> List[Tuple[Set[Optional[str]], str, Optional[str]]]:
    """`to_state=ended[0]`, `last_error_code=ended[1]`, with `ended = {...}[status]` in the same function."""
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "ended" and isinstance(node.value, ast.Subscript)
                and isinstance(node.value.value, ast.Dict)):
            pairs = [ast.literal_eval(v) for v in node.value.value.values]
            return [(_literal_states(_sources(call)), state, code) for state, code in pairs]
    raise AssertionError("the VIC ending's `ended` table is gone: resolve the new shape here")


def _manual_endings(tree: ast.Module, call: ast.Call) -> List[Tuple[Set[Optional[str]], str, Optional[str]]]:
    """`_move(row, holder, [row["state"]], target, last_error_code="manual_provider_" + status.lower())`, with
    `target = {STATUS: state}.get(status)` in `_evidence`: each provider status's state and its code."""
    code = _kw(call, "last_error_code")
    assert isinstance(code, ast.BinOp) and isinstance(code.left, ast.Constant), "the manual close's code changed shape"
    prefix = code.left.value
    evidence = next(f for f in _functions(tree) if f.name == "_evidence")
    for node in ast.walk(evidence):
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "target"
                and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Dict)):
            table = dict(zip((k.value for k in node.value.func.value.keys),
                             (v.value for v in node.value.func.value.values)))
            return [(None, state, prefix + status.lower()) for status, state in table.items()]
    raise AssertionError("the manual close's status table is gone: resolve the new shape here")


DYNAMIC = {
    ("services/payment_orchestration/a2a_rail_worker.py", "_vic_poll_authorization"): "vic",
    ("services/reap_checkout_recovery.py", "resolve_checkout_manually"): "manual",
}


def _sites() -> Tuple[List[Dict[str, Any]], Set[Tuple[str, str]]]:
    """Every refused / expired write call: {path, line, fn, rail, sources (set or None), state, code}."""
    sites: List[Dict[str, Any]] = []
    dynamic_seen: Set[Tuple[str, str]] = set()
    for path, tree in _modules():
        for fn in _functions(tree):
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call) and _call_name(n) in WRITERS):
                target = _target(call)
                if target is None:
                    continue
                if isinstance(target, ast.Constant):
                    if target.value not in ENDINGS:
                        continue
                    endings = [(_literal_states(_sources(call)), target.value,
                                _resolve_code(path, _kw(call, "last_error_code")))]
                elif isinstance(target, ast.Name) and target.id in params and fn.name in WRITERS:
                    continue  # the writer itself passing its caller's target on: its callers are the sites
                else:
                    literals = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and n.value in ENDINGS}
                    kind = DYNAMIC.get((path, fn.name))
                    if kind is None:
                        if literals:
                            raise AssertionError(
                                f"{path}:{call.lineno} {fn.name}: a write whose target is computed and may be "
                                f"refused/expired; resolve it from the source in this test")
                        continue
                    dynamic_seen.add((path, fn.name))
                    endings = _vic_endings(fn, call) if kind == "vic" else _manual_endings(tree, call)
                for sources, state, code in endings:
                    if state not in ENDINGS:
                        continue
                    sites.append(dict(path=path, line=call.lineno, fn=fn.name, rail=_rail(path),
                                      sources=sources, state=state, code=code))
    return sites, dynamic_seen


_SET_STATE = re.compile(r"\bSET\s+state\s*=\s*(CASE\b.*?\bEND|'\w+')", re.S | re.I)


def _sql_sites() -> List[Dict[str, Any]]:
    """Every SQL constant whose `SET state = ...` can write refused or expired: {path, name, rail, sql}."""
    found = []
    for path, tree in _modules():
        named = {id(node.value): node.targets[0].id for node in tree.body
                 if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
        for node in ast.walk(tree):
            text = node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
            if text is None:
                continue
            for match in _SET_STATE.finditer(text):
                if not re.search(r"'(refused|expired)'", match.group(1)):
                    continue
                name = named.get(id(node))
                assert name, f"{path}:{node.lineno}: a refused/expired SQL write that is not a named module constant"
                found.append(dict(path=path, name=name, rail=_rail(path), sql=text, set=match.group(1)))
    return found


# ── the invariant ────────────────────────────────────────────────────────────────────────────


def _violations(sites: List[Dict[str, Any]]) -> List[str]:
    bad = []
    for site in sites:
        pre = site["sources"] is not None and site["sources"] <= PRE_DISPATCH[site["rail"]]
        if not pre and site["code"] not in POST_DISPATCH_CODES:
            bad.append(f"{site['path']}:{site['line']} {site['fn']} -> {site['state']} from "
                       f"{sorted(site['sources']) if site['sources'] else 'unknown states'} code={site['code']!r}")
    return bad


def test_every_refused_or_expired_write_is_pre_dispatch_or_names_a_listed_code():
    sites, dynamic_seen = _sites()
    # Positive controls: the enumeration really found the rails' writes (24 when this was written).
    assert len(sites) >= 20, len(sites)
    assert {s["rail"] for s in sites} == {"a2a", "reap"}
    assert dynamic_seen == set(DYNAMIC), f"a dynamic site moved or vanished: {set(DYNAMIC) - dynamic_seen}"
    bad = _violations(sites)
    assert bad == [], ("a refused/expired write where a checkout or order may exist, with a code the gateway "
                       "does not list:\n" + "\n".join(bad))
    # The listed codes are really written somewhere (a stale list entry would hide nothing, but lie).
    assert POST_DISPATCH_CODES <= {s["code"] for s in sites}


def _sql_endings(sql_set: str, sql: str) -> List[Tuple[str, str]]:
    """(source state, ending) pairs of one `SET state = ...`."""
    if sql_set.upper().startswith("CASE"):
        pairs = re.findall(r"WHEN\s+state\s*=\s*'(\w+)'\s+THEN\s+'(\w+)'", sql_set, re.I)
        assert pairs, sql_set
        return [(src, end) for src, end in pairs if end in ENDINGS]
    end = sql_set.strip("'")
    states = re.search(r"\bWHERE\s+state\s+IN\s*\(([^)]*)\)", sql, re.I)
    assert states, "a sweep whose source states are not one `WHERE state IN (...)` list: parse it here"
    return [(src.strip().strip("'"), end) for src in states.group(1).split(",")]


#: SQL that ends rows on a database that is never production, with the guard that makes it so (pinned below).
STAGING_ONLY = {("scripts/ops/reap_staging_preflight.py", "_SCRUB_PURCHASES")}


def test_the_staging_scrub_is_staging_only():
    # It refuses every live Reap row (`staging_preflight_scrub`) of a restored copy of production, and its
    # target check aborts unless PIVOTA_ENV is staging and the host is staging's private IP.
    source = (REPO / "scripts/ops/reap_staging_preflight.py").read_text(encoding="utf-8")
    assert 'EXPECTED_ENV = "staging"' in source and 'EXPECTED_HOST = "10.122.0.3"' in source
    assert "THE TARGET CHECK RUNS FIRST AND ABORTS" in source


def test_every_sql_ending_is_pre_dispatch_or_proves_no_dispatch_evidence():
    found = [s for s in _sql_sites() if (s["path"], s["name"]) not in STAGING_ONLY]
    # Positive controls: the A2A sweep and the Reap sweep and lapse, in both dialects where they are literal.
    names = {(s["rail"], s["name"]) for s in found}
    assert {("a2a", "_EXPIRE_SQL"), ("a2a", "_EXPIRE_SQL_SQLITE"), ("reap", "_EXPIRE_OVERDUE_SQL"),
            ("reap", "_EXPIRE_OVERDUE_SQL_SQLITE"), ("reap", "_LAPSE_CONTACT_REENTRY_SQL")} <= names, names
    bad = []
    for site in found:
        code = re.search(r"last_error_code\s*=\s*'(\w+)'", site["sql"])
        for src, end in _sql_endings(site["set"], site["sql"]):
            if src in PRE_DISPATCH[site["rail"]] or (code and code.group(1) in POST_DISPATCH_CODES):
                continue
            needs = NO_EVIDENCE[site["rail"]].get(src)
            if needs is None:
                bad.append(f"{site['path']} {site['name']}: {src} -> {end} with no no-dispatch proof defined")
                continue
            copies = len(re.findall(r"\bstate\s+IN\s*\(", site["sql"], re.I))
            for predicate in needs:
                if site["sql"].count(predicate) < copies:
                    bad.append(f"{site['path']} {site['name']}: {src} -> {end} without {predicate!r} "
                               f"({site['sql'].count(predicate)} of {copies} copies)")
    assert bad == [], "\n".join(bad)


# ── why 'quoting' has no dispatch evidence ──────────────────────────────────────────────────


def _tree(path: str) -> ast.Module:
    return ast.parse((REPO / path).read_text(encoding="utf-8"))


def _dispatching(tree: ast.Module) -> Set[str]:
    """The functions of a module that dispatch a Reap checkout: call `begin_dispatch`, or call one that does."""
    calls = {f.name: {_call_name(c) for c in ast.walk(f) if isinstance(c, ast.Call)} for f in _functions(tree)}
    found = {name for name, called in calls.items() if "begin_dispatch" in called}
    while True:
        more = {name for name, called in calls.items() if called & found} - found
        if not more:
            return found
        found |= more


def test_every_reap_refusal_from_quoting_comes_before_any_dispatch_in_its_function():
    path = "services/reap_agentic_purchase.py"
    tree = _tree(path)
    dispatching = _dispatching(tree) | {"begin_dispatch"}
    assert {"_checkout_from_quote", "_step_quoting"} <= dispatching, dispatching
    checked = 0
    by_name = {f.name: f for f in _functions(tree)}
    for site in (s for s in _sites()[0] if s["path"] == path and s["sources"] and "quoting" in s["sources"]):
        fn = by_name[site["fn"]]
        # A dispatching call that is a `return`'s value ends its own path: nothing after it on that path.
        returned = {id(c) for r in ast.walk(fn) if isinstance(r, ast.Return) for c in ast.walk(r)}
        later = [c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call) and _call_name(c) in dispatching
                 and c.lineno <= site["line"] and id(c) not in returned]
        assert later == [], f"{path}:{site['line']} refuses from quoting after a dispatching call at {later}"
        checked += 1
    assert checked >= 8, checked


def test_advance_parks_a_quoting_row_with_dispatch_evidence_before_any_quoting_step():
    tree = _tree("services/reap_agentic_purchase.py")
    advance = next(f for f in _functions(tree) if f.name == "advance")
    dispatching = _dispatching(tree)
    # The steps run through `_STEPS[state]`; the quoting step is a dispatching one.
    steps_table = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "_STEPS" for t in n.targets))
    table = {k.value: v.id for k, v in zip(steps_table.keys, steps_table.values)}
    assert table["quoting"] in dispatching, table
    guard = [n.lineno for n in ast.walk(advance) if isinstance(n, ast.Compare)
             and isinstance(n.left, ast.Call) and _call_name(n.left) == "dispatch_state"
             and any(isinstance(c, ast.Constant) and c.value == "not_dispatched" for c in n.comparators)]
    steps = [c.lineno for c in ast.walk(advance) if isinstance(c, ast.Call)
             and (_call_name(c) in dispatching or _call_name(c) == "handler")]
    assert guard and steps and min(guard) < min(steps), (guard, steps)


def test_a2a_has_no_order_or_checkout_before_awaiting_authorization():
    import db.a2a_rail_ledger as a2a_ledger

    # The merchant checkout task is recorded only in 'paying'.
    assert "AND state = 'paying'" in a2a_ledger._RECORD_CHECKOUT_TASK_SQL
    # The rail's order is adopted only on the edge into 'awaiting_authorization'.
    adopted = []
    for path, tree in _modules():
        if "a2a" not in Path(path).name:
            continue
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) == "transition_as_holder"):
            if _kw(call, "order_id") is not None:
                target = _target(call)
                adopted.append((path, call.lineno, target.value if isinstance(target, ast.Constant) else None))
    assert adopted and all(t == "awaiting_authorization" for _, _, t in adopted), adopted
    # A refused / expired write from a pre-order state never resolves an unseen write: the ledger's fence
    # (a pending write blocks every transition that does not name it) stands between it and a live submit.
    assert "AND (pending_write IS NULL OR pending_write = :resolves_write)" in a2a_ledger._TRANSITION_SQL
    for path, tree in _modules():
        if "a2a" not in Path(path).name:
            continue
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) in WRITERS):
            target = _target(call)
            if isinstance(target, ast.Constant) and target.value in ENDINGS:
                assert _kw(call, "resolves_write") is None, f"{path}:{call.lineno}"


def test_the_reap_sweep_proves_no_dispatch_on_every_dialect():
    import db.reap_agentic_ledger as reap_ledger

    for name in ("_EXPIRE_OVERDUE_SQL", "_EXPIRE_OVERDUE_SQL_SQLITE"):
        sql = getattr(reap_ledger, name)
        assert sql.count("AND (state = 'needs_enrollment' OR dispatch_tracking_version = 1)") == 2, name


@pytest.mark.parametrize("bad, message", [
    ('await _move(row, worker_id, ["awaiting_approval"], "expired", last_error_code="stale")', "post_dispatch"),
    ('await _move(row, worker_id, [row["state"]], "refused", last_error_code=row["code"])', "unknown_states"),
    ('await ledger.transition_as_holder(pid, w, from_states=["quoting"], to_state=target)', "computed"),
])
def test_the_check_catches_what_it_is_for(tmp_path, monkeypatch, bad, message):
    """Positive control on synthetic source: a post-dispatch ending with an unlisted code, one from unknown
    states, and a computed target in a function that names refused are each caught."""
    module = tmp_path / "services"
    module.mkdir()
    (module / "reap_synthetic.py").write_text(
        "async def _step(row, worker_id, ledger, pid, w, target):\n"
        "    if target == 'refused':\n"
        f"        return {bad}\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    if message == "computed":
        with pytest.raises(AssertionError, match=message):
            _sites()
    else:
        sites, _ = _sites()
        assert sites and _violations(sites), sites
