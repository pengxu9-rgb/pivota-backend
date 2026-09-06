"""The shipped PrestaShop module and the receiver are ONE contract.

PrestaShop has no outbound webhooks, so Pivota ships the sender. Nothing
executes that PHP in CI — there is no `php` binary here — which is exactly why
these text-level tests exist: they are the only thing standing between a
renamed payload key in `pivotatelemetry.php` and a receiver that silently maps
`None`. Same shape and the same reason as
`tests/test_sfcc_cartridge_contract.py`.

Five claims are pinned:

1. the hooks never touch the network — only the cron drain controller does;
2. the header names, the signed string and the batch bounds match the receiver;
3. every payload key the module emits is declared in the mapper's wire
   contract, and every key the mapper reads is one the module emits;
4. nothing personal is serialized, and the secret is a password field that is
   never rendered back;
5. what the module does with what it CANNOT deliver, or is asked to keep: an
   exhausted row is marked dead and logged rather than deleted, the endpoint
   must be https, and uninstall does not throw away queued events or the
   secret.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "integrations" / "prestashop-module" / "pivotatelemetry"
MODULE_PHP = MODULE_DIR / "pivotatelemetry.php"
DRAIN_PHP = MODULE_DIR / "controllers" / "front" / "drain.php"
ROUTE_PY = ROOT / "routes" / "prestashop_webhooks.py"
ADAPTER_PY = ROOT / "services" / "prestashop_event_adapter.py"


def _php_keys(block: str) -> set:
    """Top-level `'key' =>` array keys in one PHP block."""
    return set(re.findall(r"'([a-z_]+)' =>", block))


def _between(text: str, start: str, end: str) -> str:
    assert start in text, f"missing anchor: {start}"
    tail = text.split(start, 1)[1]
    assert end in tail, f"missing anchor: {end}"
    return tail.split(end, 1)[0]


@pytest.fixture(scope="module")
def module_php() -> str:
    return MODULE_PHP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def drain_php() -> str:
    return DRAIN_PHP.read_text(encoding="utf-8")


# ---- 1. the network stays out of the shopper's request -------------------------


def test_module_hooks_never_open_a_socket(module_php, drain_php):
    """A hook runs inside a checkout. A Pivota outage must not reach it.

    The whole module class is scanned, not just the three hook bodies: a
    helper that any hook could call is just as fatal, and a `curl_exec`
    smuggled into `hookActionValidateOrder` has to fail this test.
    """
    for forbidden in (
        "curl_init",
        "curl_exec",
        "curl_setopt",
        "file_get_contents",
        "fsockopen",
        "stream_context_create",
        "Tools::file_get_contents",
    ):
        assert forbidden not in module_php, f"{forbidden} must not appear in the module class"
    # ...and the drain controller is where it does live.
    assert "curl_exec" in drain_php
    # Each hook enqueues and returns.
    for hook in (
        "hookActionValidateOrder",
        "hookActionOrderStatusPostUpdate",
        "hookActionOrderSlipAdd",
    ):
        assert f"public function {hook}(" in module_php
        native = hook[4].lower() + hook[5:]
        assert f"$this->enqueue('{native}'" in module_php
    assert "Db::getInstance()->insert(" in module_php


def test_module_registers_post_update_not_the_pre_write_hook(module_php):
    """`actionOrderStatusUpdate` fires BEFORE the new state is written
    (classes/order/OrderHistory.php), so the order there still carries the old
    `current_state` and the old `total_paid_real`."""
    assert "$this->registerHook('actionValidateOrder')" in module_php
    assert "$this->registerHook('actionOrderStatusPostUpdate')" in module_php
    assert "$this->registerHook('actionOrderSlipAdd')" in module_php
    assert "registerHook('actionOrderStatusUpdate')" not in module_php

    from services.prestashop_event_adapter import SUPPORTED_PRESTASHOP_HOOKS

    registered = set(re.findall(r"registerHook\('(\w+)'\)", module_php))
    assert {hook.lower() for hook in registered} <= SUPPORTED_PRESTASHOP_HOOKS
    emitted = set(re.findall(r"\$this->enqueue\('(\w+)'", module_php))
    assert {hook.lower() for hook in emitted} <= SUPPORTED_PRESTASHOP_HOOKS
    assert emitted == registered


def test_module_never_asks_for_a_configuration_key_prestashop_does_not_have(module_php):
    """`PS_OS_PAYMENT_ERROR` does not exist in PrestaShop. Reading it returns
    false, which then compares equal to order state 0 — every unmatched state
    would resolve to a payment failure."""
    assert "PS_OS_PAYMENT_ERROR" not in module_php
    for existing in (
        "PS_OS_PAYMENT",
        "PS_OS_CANCELED",
        "PS_OS_REFUND",
        "PS_OS_ERROR",
        "PS_OS_SHIPPING",
        "PS_OS_DELIVERED",
    ):
        assert f"'{existing}'" in module_php
    # One L, as PrestaShop spells it.
    assert "PS_OS_CANCELLED" not in module_php


# ---- 2. the wire: headers, signed string, bounds --------------------------------


def test_drain_sends_exactly_the_headers_the_receiver_reads(drain_php):
    sent = set(re.findall(r"'(X-Pivota-PrestaShop-[\w-]+): ", drain_php))
    read = set(re.findall(r'alias="(X-Pivota-PrestaShop-[\w-]+)"', ROUTE_PY.read_text(encoding="utf-8")))
    assert sent == read == {
        "X-Pivota-PrestaShop-Signature",
        "X-Pivota-PrestaShop-Timestamp",
        "X-Pivota-PrestaShop-Delivery-Id",
        "X-Pivota-PrestaShop-Shop-Url",
    }


def test_drain_signs_timestamp_dot_body_with_sha256(drain_php):
    assert "hash_hmac('sha256', $timestamp . '.' . $body, $secret)" in drain_php
    assert "'X-Pivota-PrestaShop-Signature: sha256=' . $signature" in drain_php
    # The receiver rebuilds the same material.
    route = ROUTE_PY.read_text(encoding="utf-8")
    assert 'str(timestamp_int).encode("ascii") + b"." + raw' in route
    assert "hmac.compare_digest(" in route


def test_drain_is_token_guarded_bounded_and_gives_up(module_php, drain_php):
    from routes import prestashop_webhooks as route

    assert "hash_equals($expectedToken, $suppliedToken)" in drain_php
    assert "const MAX_EVENTS_PER_BATCH = 100;" in module_php
    assert re.search(r"const MAX_BATCHES_PER_RUN = \d+;", module_php)
    assert "const MAX_ATTEMPTS = 20;" in module_php
    # The batch the sender builds must fit the batch the receiver accepts.
    assert route.MAX_PRESTASHOP_EVENTS_PER_BATCH == 100
    # ...and all three bounds are actually applied in the drain loop.
    assert "$batch < PivotaTelemetry::MAX_BATCHES_PER_RUN" in drain_php
    assert "LIMIT ' . (int) PivotaTelemetry::MAX_EVENTS_PER_BATCH" in drain_php
    assert "$attempts >= PivotaTelemetry::MAX_ATTEMPTS" in drain_php
    # A non-2xx keeps the rows and backs off; a 2xx deletes them.
    assert "$this->markRetry($rows);" in drain_php
    assert "$this->removeRows($rows);" in drain_php
    assert "min(3600, pow(2, min($attempts, 10)) * 15)" in drain_php
    assert "$status >= 200 && $status < 300" in drain_php


def test_drain_envelope_is_the_envelope_the_receiver_parses(drain_php):
    from services.prestashop_event_adapter import MODULE_ENVELOPE_KEYS

    envelope = _between(drain_php, "$body = json_encode(array(", "));")
    assert _php_keys(envelope) == set(MODULE_ENVELOPE_KEYS)


# ---- 3. every key, both directions ---------------------------------------------


def test_module_emits_exactly_the_declared_wire_contract(module_php):
    from services.prestashop_event_adapter import (
        MODULE_EVENT_KEYS,
        MODULE_ORDER_KEYS,
        MODULE_SLIP_KEYS,
        MODULE_STATE_FLAG_KEYS,
    )

    payload = _between(module_php, "$payload = array(", "\n        );")
    head = payload.split("'order' => array(", 1)[0]
    order_block = _between(payload, "'order' => array(", "'order_slip' =>")
    slip_block = _between(payload, "'order_slip' => $slip ? array(", ") : null,")
    flags_block = _between(module_php, "private function stateFlags($state)", ");")

    assert _php_keys(head) | {"order", "order_slip"} == set(MODULE_EVENT_KEYS)
    assert _php_keys(order_block) == set(MODULE_ORDER_KEYS)
    assert _php_keys(slip_block) == set(MODULE_SLIP_KEYS)
    assert _php_keys(flags_block) == set(MODULE_STATE_FLAG_KEYS)


def test_the_mapper_reads_nothing_the_module_does_not_send():
    """The other direction. Every `.get("x")` in the adapter is a read of the
    module's payload — there is no other dict it reaches into — so an
    undeclared key here is a field that will always arrive as None."""
    from services.prestashop_event_adapter import (
        MODULE_ENVELOPE_KEYS,
        MODULE_EVENT_KEYS,
        MODULE_ORDER_KEYS,
        MODULE_SLIP_KEYS,
        MODULE_STATE_FLAG_KEYS,
    )

    declared = (
        set(MODULE_ENVELOPE_KEYS)
        | set(MODULE_EVENT_KEYS)
        | set(MODULE_ORDER_KEYS)
        | set(MODULE_SLIP_KEYS)
        | set(MODULE_STATE_FLAG_KEYS)
    )
    read = set(re.findall(r'\.get\("([a-z_]+)"\)', ADAPTER_PY.read_text(encoding="utf-8")))
    assert read, "the regex found nothing — it no longer matches the adapter"
    assert read <= declared, sorted(read - declared)
    # And the keys money actually depends on are genuinely read, so the
    # subset assertion above cannot pass by reading nothing that matters.
    assert {
        "hook",
        "occurred_at",
        "order",
        "order_slip",
        "id",
        "id_cart",
        "id_customer",
        "currency",
        "state_key",
        "state_flags",
        "paid",
        "total_paid_real",
        "total_paid_tax_incl",
        "total_products_tax_incl",
        "total_shipping_tax_incl",
        "amount",
        "shipping_cost_amount",
        "payment_module",
    } <= read


# ---- 4. no personal data, no leaked secret --------------------------------------


def test_module_serializes_nothing_personal(module_php, drain_php):
    for sensitive in (
        "email",
        "firstname",
        "lastname",
        "address",
        "phone",
        "card",
        "cookie",
    ):
        assert sensitive not in module_php.lower(), sensitive
        assert sensitive not in drain_php.lower(), sensitive
    # The customer is an integer id and nothing else.
    assert "'id_customer' => (int) $order->id_customer," in module_php


def test_the_secret_is_a_password_field_that_is_never_rendered_back(module_php, drain_php):
    form = _between(module_php, "private function renderForm()", "return $helper->generateForm")
    assert "'type' => 'password'," in form
    assert "'name' => 'PIVOTA_TELEMETRY_SECRET'," in form
    # The stored value is deliberately NOT put back into the field.
    assert "'PIVOTA_TELEMETRY_SECRET' => ''," in form
    assert "Configuration::get('PIVOTA_TELEMETRY_SECRET')" not in form
    # An empty submission keeps the current secret rather than clearing it.
    assert "if ($submittedSecret !== '')" in module_php
    # Nothing logs the secret or the payload.
    assert "[Pivota telemetry payload redacted]" in drain_php
    for line in drain_php.splitlines():
        if "addLog(" in line or "$secret" in line:
            assert "'. $secret" not in line and "$secret . " not in line


# ---- 5. exhaustion, transport, and what uninstall keeps --------------------------


def test_an_exhausted_row_is_marked_dead_and_logged_not_deleted(module_php, drain_php):
    """A DELETE here was silent data loss: the shop had no record that anything
    was dropped and Pivota's ledger was simply short. The row stays, the drain
    stops selecting it, and the shop's own log names it once."""
    exhausted = _between(
        drain_php,
        "if ($attempts >= PivotaTelemetry::MAX_ATTEMPTS) {",
        "continue;",
    )
    assert "DELETE" not in exhausted.upper(), exhausted
    assert "$this->markDead($row, $attempts);" in exhausted

    dead = _between(drain_php, "private function markDead(array $row, $attempts)", "\n    }")
    assert "DELETE" not in dead.upper(), dead
    assert "`status` = \"' . pSQL(PivotaTelemetry::STATUS_DEAD)" in dead
    # ...and it says so, at error severity, with the identifiers a human needs.
    assert "PrestaShopLogger::addLog(" in dead
    assert "$row['event_id']" in dead
    assert "$orderId" in dead
    assert "$attempts" in dead
    assert "\n            3,\n" in dead, "the give-up log must be severity 3 (error)"

    # The ONLY delete left in the drain is the one that removes DELIVERED rows.
    deletes = [
        line for line in drain_php.splitlines() if "DELETE FROM" in line.upper()
    ]
    assert len(deletes) == 1, deletes
    remove = _between(drain_php, "private function removeRows(array $rows)", "\n    }")
    assert "DELETE FROM" in remove.upper(), "the surviving DELETE is not the delivered-row one"


def test_the_drain_never_selects_a_dead_row(module_php, drain_php):
    """Without this filter a dead row is re-POSTed every two minutes forever."""
    due = _between(drain_php, "private function dueRows()", "\n    }")
    assert "`status` = \"' . pSQL(PivotaTelemetry::STATUS_PENDING)" in due
    # The column it filters on is one the install SQL actually creates, with the
    # value the enqueue actually writes.
    install = _between(module_php, "private function createOutboxTable()", "return Db::getInstance()")
    assert "`status` VARCHAR(16) NOT NULL DEFAULT" in install
    assert "pSQL(self::STATUS_PENDING)" in install
    assert "const STATUS_PENDING = 'pending';" in module_php
    assert "const STATUS_DEAD = 'dead';" in module_php
    enqueue = _between(module_php, "Db::getInstance()->insert(", "\n        );")
    assert "'status' => pSQL(self::STATUS_PENDING)," in enqueue


def test_the_merchant_can_see_how_many_events_were_lost(module_php):
    """A dead row nobody can count is the same silence as a deleted one."""
    assert "public function deadCount()" in module_php
    assert "self::STATUS_DEAD" in _between(module_php, "public function deadCount()", "\n    }")
    content = _between(module_php, "public function getContent()", "return $output . $this->renderForm();")
    assert "$this->deadCount()" in content
    assert "could not be delivered" in content


def test_the_endpoint_must_be_https(module_php):
    """The body is signed, not encrypted. Over http the payload and a valid
    signature for it travel in cleartext, replayable inside the receiver's
    300 s window."""
    content = _between(module_php, "public function getContent()", "return $output . $this->renderForm();")
    assert "stripos($submittedEndpoint, 'https://') !== 0" in content
    assert "$this->displayError(" in content
    # The rejected value is not written anyway.
    saved = content.split("} else {", 1)[1]
    assert "Configuration::updateValue('PIVOTA_TELEMETRY_ENDPOINT', $submittedEndpoint);" in saved


def test_uninstall_keeps_queued_events_and_the_secret(module_php):
    """Back-office **Reset** is uninstall + install, so uninstall runs on a path
    a merchant takes to FIX the module. Dropping the outbox there threw away
    undelivered events; deleting the secret forced a rotation and a re-paste."""
    uninstall = _between(module_php, "public function uninstall()", "return parent::uninstall();")
    assert "$this->pendingCount()" in uninstall
    assert "if ($pending > 0)" in uninstall
    assert "PrestaShopLogger::addLog(" in uninstall
    # The DROP is inside the else, i.e. only when nothing is queued.
    dropped = uninstall.split("} else {", 1)
    assert len(dropped) == 2, uninstall
    assert "DROP TABLE" not in dropped[0].upper()
    assert "DROP TABLE IF EXISTS" in dropped[1]
    # The two values a merchant cannot re-create are never deleted.
    assert "deleteByName('PIVOTA_TELEMETRY_SECRET')" not in module_php
    assert "deleteByName('PIVOTA_TELEMETRY_ENDPOINT')" not in module_php
    # ...and the one that install() mints again still is.
    assert "deleteByName('PIVOTA_TELEMETRY_CRON_TOKEN')" in uninstall


def test_the_dead_row_replay_gap_is_written_down(module_php):
    """A stated gap, not a silent one: nothing on the receiver side notices a
    shop that stopped delivering, and there is no self-service replay."""
    readme = (MODULE_DIR / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "PRESTASHOP_TELEMETRY.md").read_text(encoding="utf-8")
    for text in (readme, doc):
        assert "dead" in text.lower()
        assert "support" in text.lower()
    assert "staleness signal" in doc


def test_the_unread_store_id_setting_is_gone(module_php):
    """`PIVOTA_TELEMETRY_STORE_ID` was a REQUIRED configuration field nothing
    read: the store id is already the last segment of the endpoint URL, so the
    only thing it could do was be typed wrong."""
    assert "PIVOTA_TELEMETRY_STORE_ID" not in module_php
    readme = (MODULE_DIR / "README.md").read_text(encoding="utf-8")
    assert "PIVOTA_TELEMETRY_STORE_ID" not in readme
