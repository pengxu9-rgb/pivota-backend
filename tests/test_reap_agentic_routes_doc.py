"""docs/reap_agentic_routes.md documents the re-entry contract the routes actually implement.

The gateway codes against that page. These checks pin the parts that drifted once already:
`POST /purchases/{purchase_id}/resume`, `contact_reentry_required`, `checkout_dispatch_state`,
and the `503 checkout_outcome_unknown` instruction. They read the code's own vocabularies, so a
new refusal or dispatch state the page does not name fails here instead of in a client.
"""
import ast
import json
import re
from pathlib import Path

import db.reap_continuation as continuation
import routes.agent_commerce_reap as routes_reap

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "reap_agentic_routes.md").read_text(encoding="utf-8")
ROUTE_SRC = (ROOT / "routes" / "agent_commerce_reap.py").read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """From `heading` to the next heading of the same or a higher level."""
    start = DOC.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    nxt = re.search(rf"^#{{1,{level}}} ", DOC[start + len(heading):], flags=re.M)
    return DOC[start: start + len(heading) + (nxt.start() if nxt else len(DOC))]


def _function_source(name: str) -> str:
    tree = ast.parse(ROUTE_SRC)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.get_source_segment(ROUTE_SRC, node)


def _raised_reasons(name: str) -> set:
    return set(re.findall(r'PurchaseRefused\(\s*"([a-z_]+)"', _function_source(name)))


RESUME = _section("### Resume a contact-paused purchase")


def test_the_resume_route_is_documented_with_its_path():
    assert "@router.post(\"/purchases/{purchase_id}/resume\")" in ROUTE_SRC
    assert "`POST /agent/v2/commerce/reap/purchases/{purchase_id}/resume`" in RESUME


def test_every_refusal_the_resume_handler_raises_itself_is_in_its_table():
    raised = _raised_reasons("resume_reap_purchase") | _raised_reasons("_validate_resume_selection")
    # The create gate's private reason is answered as the public 404 (`_refused`).
    raised.discard("create_disabled")
    assert raised, "the parser found no refusals; the test is broken"
    missing = sorted(r for r in raised if f"`{r}`" not in RESUME)
    assert not missing, f"resume refusals missing from the contract page: {missing}"
    for reason in raised:
        status = routes_reap._REFUSAL_STATUS.get(reason, routes_reap._DEFAULT_REFUSAL_STATUS)
        assert re.search(rf"^\| {status} \| [^\n]*`{reason}`", RESUME, flags=re.M), (reason, status)


def test_resume_documents_its_binding_and_the_503():
    # The window and its terminal outcome are pinned to the code below
    # (test_the_resume_section_states_the_window_the_code_runs).
    for phrase in ("idempotency_key", "same agent and the same buyer", "`purchase_not_found`",
                   "`price_changed`", "`consent_required`", "| 503 | `checkout_outcome_unknown` |"):
        assert phrase in RESUME, phrase


def test_every_dispatch_state_value_is_documented_and_no_other():
    values = set(re.findall(r"return '([a-z_]+)'", ast.get_source_segment(
        Path(continuation.__file__).read_text(encoding="utf-8"),
        next(n for n in ast.walk(ast.parse(Path(continuation.__file__).read_text(encoding="utf-8")))
             if isinstance(n, ast.FunctionDef) and n.name == "dispatch_state"))))
    assert values == {"dispatched", "dispatch_started", "not_dispatched", "unknown"}
    rules = _section("### Field rules the door must not guess at")
    documented = set(re.findall(r"^  \| `([a-z_]+)` \|", rules, flags=re.M))
    assert documented == values
    assert "**`contact_reentry_required`**" in rules


def test_the_create_refusal_table_says_recover_never_repost_on_503():
    create = _section("## `POST /agent/v2/commerce/reap/purchases`")
    row = next(line for line in create.splitlines() if line.startswith("| 503 | `checkout_outcome_unknown`"))
    assert "POST /purchases/recover" in row and "same body and key" in row and "never re-POST" in row


def test_the_documented_202_bodies_carry_exactly_the_keys_the_route_returns():
    create = _section("### Response — `202 Accepted`")
    bodies = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", create, flags=re.S)]
    variant, cart = bodies[0], bodies[1]
    # The same key sets tests/test_reap_contact_resume.py pins against the live route.
    assert list(variant) == ["purchase_id", "status", "poll_after_seconds",
                             "checkout_dispatch_state", "contact_reentry_required"]
    assert list(cart) == list(variant) + ["variant_title"]
    accepted = _function_source("start_reap_purchase")
    for key in variant:
        assert f'"{key}"' in accepted, key


def test_the_documented_owner_views_carry_the_two_continuation_fields():
    get = _section("## `GET /agent/v2/commerce/reap/purchases/{purchase_id}`")
    views = [json.loads(block) for block in re.findall(r"```json\n(\{\n  \"id\".*?)\n```", get, flags=re.S)]
    assert len(views) == 3
    for view in views:
        assert list(view)[-2:] == ["checkout_dispatch_state", "contact_reentry_required"]
        assert view["contact_reentry_required"] is False
    assert [v["checkout_dispatch_state"] for v in views] == ["not_dispatched", "dispatched", "dispatched"]


# ── the re-entry window, pinned to the CODE that runs it ──────────────────────────────────────
#
# The page once stated the window and its terminal code before the sweep existed. Every value
# below is read from the ledger and the poller, so the page cannot state a dial, a default, a
# bound, an anchor, a terminal code or a transition the code does not implement.

def lapse_sql_facts(statements) -> dict:
    """Read the lapse UPDATE's terminal code, transitions and clock out of its SQL text.

    `anchor` is the column the window is measured from; `legacy_anchor` is the fallback a
    COALESCE gives rows with no `anchor` (None when there is no fallback), and `legacy_states`
    the states that fallback admits."""
    codes, cases, anchors, legacy_states = set(), set(), set(), set()
    for sql in statements:
        head = sql.split("WHERE", 1)[0]  # the SET clause: the code the row is GIVEN
        codes |= set(re.findall(r"last_error_code = '([a-z_]+)'", head))
        cases.add(re.search(r"SET state = CASE WHEN state = '([a-z_]+)' THEN '([a-z_]+)' ELSE '([a-z_]+)' END",
                            head).groups())
        anchors |= set(re.findall(
            r"AND (?:COALESCE\()?(?:p\.)?([a-z_]+)(?:, (?:p\.)?([a-z_]+)\))? < (?:clock_timestamp\(\) -|datetime\('now')",
            sql))
        for states in re.findall(r"OR \((?:p\.)?state IN \(([^)]*)\)", sql):
            legacy_states.add(tuple(re.findall(r"'([a-z_]+)'", states)))
    [code], [(special, special_to, other_to)], [(anchor, legacy_anchor)] = codes, cases, anchors
    assert len(legacy_states) <= 1, legacy_states
    return {"code": code, "special": (special, special_to), "other_to": other_to, "anchor": anchor,
            "legacy_anchor": legacy_anchor or None,
            "legacy_states": next(iter(legacy_states), ())}


def lapse_facts():
    """What `ledger.lapse_contact_reentry` actually does, read out of its own SQL and the dial."""
    from db import reap_agentic_ledger as ledger
    from jobs import reap_agentic_purchase_poll as poll

    dial = poll.DIALS["contact_reentry_window_seconds"]
    assert (dial.default, dial.minimum, dial.maximum) == (
        ledger.CONTACT_REENTRY_WINDOW_SECONDS_DEFAULT, ledger.CONTACT_REENTRY_WINDOW_SECONDS_MIN,
        ledger.CONTACT_REENTRY_WINDOW_SECONDS_MAX)
    facts = lapse_sql_facts((ledger._LAPSE_CONTACT_REENTRY_SQL, ledger._LAPSE_CONTACT_REENTRY_SQL_SQLITE))
    (special, special_to), other_to = facts["special"], facts["other_to"]
    transitions = {state: (special_to if state == special else other_to)
                   for state in ledger._LAPSE_CONTACT_REENTRY_SOURCE_STATES}
    assert transitions == {"needs_enrollment": "expired", "resolving": "failed", "quoting": "failed"}
    assert facts["code"] in poll.PollReport.__dataclass_fields__
    return dial, facts["code"], facts["anchor"], transitions


def lapse_legacy():
    """(fallback anchor, states it admits) for rows paused before `contact_purged_at`, or (None, ())."""
    from db import reap_agentic_ledger as ledger

    facts = lapse_sql_facts((ledger._LAPSE_CONTACT_REENTRY_SQL, ledger._LAPSE_CONTACT_REENTRY_SQL_SQLITE))
    return facts["legacy_anchor"], facts["legacy_states"]


def assert_states_the_window(text: str, *, where: str) -> None:
    dial, code, anchor, transitions = lapse_facts()
    text = " ".join(text.split())
    for phrase in (f"`{dial.env}`", f"default {dial.default}", f"{dial.minimum}–{dial.maximum}",
                   f"measured from `{anchor}`", "dispatch evidence"):
        assert phrase in text, (where, phrase)
    assert f'last_error_code: "{code}"' in text, (where, code)
    for source, target in transitions.items():
        assert f"`{source}` → `{target}`" in text, (where, source, target)


def test_the_field_rule_states_the_window_the_code_runs():
    rules = _section("### Field rules the door must not guess at")
    bullet = rules.split("* **`contact_reentry_required`**", 1)[1].split("\n* ", 1)[0]
    assert_states_the_window(bullet, where="field rules")


def test_the_resume_section_states_the_window_the_code_runs():
    lapse = RESUME.split("**If nobody resumes.**", 1)[1]
    assert_states_the_window(lapse, where="resume")
    assert "terminal_purchase_not_resumable" in lapse


# ── /resume: the table is in the order the handler checks ─────────────────────────────────────

def _resume_check_sequence() -> list:
    """The refusals `resume_reap_purchase` reaches, in source order, with the fresh admission
    (`_validate_resume_selection`) inlined where it is called. Helpers that answer a fixed public
    code are mapped to it: `_require_rail`/the create gate and the pilot scope to the 404,
    `_not_found()` to `purchase_not_found`, `_PurchasePersistenceUnavailable` to the 503."""
    def tokens(name):
        pattern = (r'PurchaseRefused\(\s*"([a-z_]+)"|(_require_rail\(\))|(_not_found\(\))'
                   r'|(enforce_pilot_scope\()|(await _validate_resume_selection\()|(except _PurchasePersistenceUnavailable)')
        out = []
        for m in re.finditer(pattern, _function_source(name)):
            reason, rail, missing, pilot, admission, unavailable = m.groups()
            if admission:
                out += tokens("_validate_resume_selection")
            elif reason:
                out.append("not_available_on_this_rail" if reason == "create_disabled" else reason)
            elif rail or pilot:
                out.append("not_available_on_this_rail")
            elif missing:
                out.append("purchase_not_found")
            elif unavailable:
                pass  # the handler's catch, not a check; the 503 row is placed by the key lookup
        return out
    return tokens("resume_reap_purchase")


def test_the_resume_table_is_in_the_order_the_handler_checks():
    assert "in the order the handler checks them" in RESUME
    rows = [line for line in RESUME.splitlines() if re.match(r"^\| \d{3} \| `", line)]
    sequence = _resume_check_sequence()
    assert sequence[0] == "not_available_on_this_rail" and sequence[-1] == "resume_raced", sequence
    assert sequence.index("purchase_not_found") < sequence.index("terminal_purchase_not_resumable") \
        < sequence.index("checkout_dispatch_unresolved") < sequence.index("contact_reentry_not_required") \
        < sequence.index("consent_required") < sequence.index("buyer_unlinked") \
        < sequence.index("merchant_not_purchasable") < sequence.index("price_changed")
    # Every check maps to a table row at or after the previous check's row (a reason checked twice,
    # like the 404 or `consent_required`, has a row at each position).
    at = 0
    for reason in sequence:
        later = [i for i, row in enumerate(rows) if i >= at and f"`{reason}`" in row.split("|")[2]]
        assert later, (reason, "no table row at or after", rows[at] if at < len(rows) else None)
        at = later[0]
    # The late 404s are said to be late.
    gates = RESUME.split("**Gates and authentication.**", 1)[1].split("\n\n", 1)[0]
    assert "fresh admission" in gates


# ── /recover: what the 200 can be, and which failures are 503 ────────────────────────────────

RECOVER = _section("### Recover a lost create response")


def test_recover_documents_the_retired_receipt_and_its_keys():
    import services.reap_unopened_attempt as retirement

    src = Path(retirement.__file__).read_text(encoding="utf-8")
    receipt = re.search(r'return \{"recovery_status": "([a-z_]+)", "reconciliation_id": [^}]+\}', src)
    assert receipt, "the retired receipt shape moved; re-pin this test"
    for key in ("recovery_status", "reconciliation_id", receipt.group(1)):
        assert f"`{key}`" in RECOVER or f'"{key}"' in RECOVER, key
    assert "no `checkout_dispatch_state`" in " ".join(RECOVER.split())
    rules = " ".join(_section("### Field rules the door must not guess at").split())
    assert "except a retired attempt's `/recover` receipt" in rules


def test_recover_names_which_failures_are_503_and_which_are_500():
    flat = " ".join(RECOVER.split())
    line = next(l for l in RECOVER.splitlines() if l.startswith("* `503 checkout_outcome_unknown`"))
    assert "retirement receipt" in line and "refusal marker" in line
    assert "key mapping or retirement receipt could not be read" not in flat
    assert "`500`" in flat
    # The /recover handler: only the receipt read is wrapped; the key lookup is not.
    src = _function_source("recover_reap_purchase")
    assert re.search(r"retired_receipt\(.*?\)\s*except Exception:\s*raise _PurchasePersistenceUnavailable", src, re.S)
    lookup = src.split("purchase_id = await _replayed_purchase_id", 1)[1]
    assert "except Exception" not in lookup.split("except _PurchasePersistenceUnavailable", 1)[0]
