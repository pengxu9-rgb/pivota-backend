"""Code-enforced guardrails for every merchant-facing write an assistant lane proposes.

Ported from Anthropic's open-source commerce-agents blueprint (Apache-2.0,
`merchant-agent/core/merchant_agent/changes.py` + `gates.py`, read 2026-09-02). The
blueprint's shape is kept deliberately: a pure `check_guardrails(kind, items, config)`
returning one operator-readable message per broken rule, run when a change is STAGED
and again at APPLY against the config in force at apply time.

What is adapted, and why
------------------------
Pivota's assistant lanes are content lanes. Two of them reach merchant-visible state:

  1. A PDP governance module payload -> `merchant_product_overlay` (the public PDP
     merge hook serves it).  Stage: `create_module_draft`.  Apply:
     `publish_module_version`.
  2. The product-enrichment overlay -> the served Pivota PDP and, on explicit merchant
     action, the merchant's live Shopify store as the app-owned `pivota/ai_pdp`
     metafield.  Stage: `CanonicalPdpEnrichmentAgent`.  Apply: `publish_content_to_store`.

So the blueprint's promotion-depth and campaign-budget limits are dropped (Pivota has
no promotion or campaign lane to bound), and the price/restock limits are kept but are
DORMANT: no lane wired here moves a price or a stock level. They exist so the first
lane that does cannot ship without a ceiling. The rules that actually bite today are
the items cap, the one-line-per-(target, field) rule, the protected-field set, the
fields a content update may not carry, and the field-size ceiling.

Nothing in this module does I/O, imports a route, or touches the database.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------
# Kinds and items
# --------------------------------------------------------------------------------

#: A PDP governance module payload on its way to `merchant_product_overlay`.
KIND_PDP_MODULE_CONTENT = "pdp_module_content"
#: Copy on its way to the merchant's own live store (Shopify app-owned metafield).
KIND_STORE_CONTENT_WRITEBACK = "store_content_writeback"
#: The product-enrichment overlay that feeds the served Pivota PDP.
KIND_PRODUCT_ENRICHMENT = "product_enrichment"
#: Dormant today -- no wired lane emits these. See the module docstring.
KIND_PRICE_UPDATE = "price_update"
KIND_INVENTORY_ACTION = "inventory_action"

WRITE_KINDS: Tuple[str, ...] = (
    KIND_PDP_MODULE_CONTENT,
    KIND_STORE_CONTENT_WRITEBACK,
    KIND_PRODUCT_ENRICHMENT,
    KIND_PRICE_UPDATE,
    KIND_INVENTORY_ACTION,
)

#: Kinds that are content edits. A price or stock move must never ride inside one.
CONTENT_KINDS: frozenset = frozenset(
    {KIND_PDP_MODULE_CONTENT, KIND_STORE_CONTENT_WRITEBACK, KIND_PRODUCT_ENRICHMENT}
)

# Who is asking for the write. A model NEVER satisfies a host-approval requirement:
# an approval emitted by the model, or typed into a payload it produced, sets nothing.
ACTOR_HUMAN = "human"
ACTOR_MODEL = "model"
ACTOR_SYSTEM = "system"


@dataclass(frozen=True)
class WriteItem:
    """One (target, field) line of a proposed change -- the unit an operator approves."""

    target: str
    field: str
    before: Any = None
    after: Any = None


class GuardrailViolation(ValueError):
    """The write breaks the guardrails; ``violations`` holds one message per rule.

    Routes map this to a 4xx with the messages attached -- it must never reach a
    caller as an unhandled 500, and it must never be swallowed into a silent no-op.
    """

    def __init__(self, violations: Sequence[str]):
        self.violations: List[str] = list(violations)
        super().__init__("; ".join(self.violations))


# --------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------

# Every default below is a PRODUCTION value chosen for Pivota's lanes, not the
# blueprint's demonstration number. The blueprint says as much in docs/safety.md
# ("The defaults in the two config.py modules are demonstration values.").

#: Identity, routing and merchant-identity keys. An assistant payload that could set
#: one of these would move the write onto a different product, a different merchant,
#: or a different storefront field than the one the lane resolved. `body_html` is here
#: because `shopify_content_writeback` promises never to write the merchant's theme
#: body, and a guardrail is cheaper than re-reading that promise.
_PROTECTED_FIELDS: Tuple[str, ...] = (
    "product_key",
    "content_key",
    "platform",
    "platform_product_id",
    "source_product_id",
    "variant_id",
    "variant_ids",
    "sku",
    "sku_key",
    "barcode",
    "gtin",
    "handle",
    "url",
    "canonical_url",
    "destination_url",
    "merchant_id",
    "store_id",
    "seller_of_record",
    "currency",
    "body_html",
)

#: Names Pivota's catalog and offer writers actually use for money and stock
#: (catalog_offers.list_price / merchant_effective_price / inventory_quantity /
#: availability, catalog_products.price_tier, external_product_seeds.price_amount),
#: plus the platform-standard names. A content edit carrying one of these is refused
#: and told to stage as its own kind, so the price/restock ceilings apply to it.
_CONTENT_BLOCKED_FIELDS: Tuple[str, ...] = (
    "price",
    "prices",
    "compare_at_price",
    "sale_price",
    "list_price",
    "merchant_effective_price",
    "estimated_best_price",
    "price_amount",
    "price_tier",
    "inventory_quantity",
    "inventory",
    "stock",
    "quantity",
    "availability",
)

#: The subset of the above that carries an amount the movement cap can be checked on.
_PRICE_BEARING_FIELDS: Tuple[str, ...] = (
    "price",
    "compare_at_price",
    "sale_price",
    "list_price",
    "merchant_effective_price",
    "estimated_best_price",
    "price_amount",
)


@dataclass(frozen=True)
class MerchantWriteGuardrailConfig:
    """Limits, checked at staging and again at apply against the config in force then."""

    #: The largest real change today is the Shopify metafield write -- five copy fields
    #: on one product -- and the overlay write, which is one field. 20 leaves headroom
    #: for `_OVERLAY_FIELD_MAP` and `_build_metafield_value` to grow while still
    #: refusing a bulk write that escaped its batcher. (Blueprint demo value: 25.)
    max_items_per_change: int = 20

    #: A runaway-output ceiling, not an editorial one. Real PDP descriptions are long,
    #: so a tight cap would refuse honest copy; the blueprint's 2,000 would. This sits
    #: below the size a Shopify JSON metafield value accepts, so a value past it is one
    #: the store would reject after we had already sent it.
    max_field_chars: int = 50_000

    #: DORMANT -- no wired lane moves a price (see the module docstring). Set below the
    #: blueprint's 20.0 because Pivota's assistant lanes run unattended: the first lane
    #: that does move a price inherits the tighter ceiling, not a demo number.
    max_price_delta_pct: float = 10.0

    #: DORMANT -- no wired lane moves stock. Blueprint demo value was 500.
    max_restock_quantity: int = 100

    protected_fields: Tuple[str, ...] = _PROTECTED_FIELDS
    content_update_blocked_fields: Tuple[str, ...] = _CONTENT_BLOCKED_FIELDS
    price_bearing_fields: Tuple[str, ...] = _PRICE_BEARING_FIELDS

    #: Host approval, per lane. TRUE means a model actor can never make the write land;
    #: only a human (or a deployment-owned system actor) can.
    #:
    #: `store_content_writeback` defaults TRUE: it reaches the merchant's live store,
    #: and the only production caller is already an explicit merchant action
    #: (routes/merchant_products.py `publish_store_pdp`, role=="merchant"), so TRUE
    #: matches production behaviour exactly while closing the path to a future
    #: auto-dispatcher.
    require_host_approval_store_writeback: bool = True

    #: `pdp_module_publish` defaults FALSE, which is TODAY'S BEHAVIOUR and is deliberate
    #: -- see the PR body. Publishing a low-risk module on an LLM rubric alone is the
    #: designed machine-publish lane (`MACHINE_PUBLISH_MODULES`, the
    #: `machine_publish_allowed_module` rubric check, and
    #: tests/test_pdp_governance_routes.py::test_gpt55_gate_can_publish_low_risk_llm_candidate_after_review
    #: which asserts published is True from a model rubric). Flipping this default would
    #: change production silently. The switch exists so an operator can close the lane
    #: without a deploy, and the guardrails above bound it either way.
    require_host_approval_pdp_module_publish: bool = False


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_CONFIG = MerchantWriteGuardrailConfig()


def current_config() -> MerchantWriteGuardrailConfig:
    """The config in force RIGHT NOW.

    Read fresh on every call rather than cached at import, because the blueprint's
    contract is that apply re-checks against the config in force AT APPLY TIME: an
    operator who tightens a limit while a change sits staged must have the tighter
    limit applied to it.
    """
    return replace(
        DEFAULT_CONFIG,
        max_items_per_change=_env_int(
            "MERCHANT_WRITE_MAX_ITEMS", DEFAULT_CONFIG.max_items_per_change
        ),
        max_field_chars=_env_int(
            "MERCHANT_WRITE_MAX_FIELD_CHARS", DEFAULT_CONFIG.max_field_chars
        ),
        max_price_delta_pct=_env_float(
            "MERCHANT_WRITE_MAX_PRICE_DELTA_PCT", DEFAULT_CONFIG.max_price_delta_pct
        ),
        max_restock_quantity=_env_int(
            "MERCHANT_WRITE_MAX_RESTOCK", DEFAULT_CONFIG.max_restock_quantity
        ),
        require_host_approval_store_writeback=_env_flag(
            "MERCHANT_WRITE_REQUIRE_HOST_APPROVAL_STORE_WRITEBACK",
            DEFAULT_CONFIG.require_host_approval_store_writeback,
        ),
        require_host_approval_pdp_module_publish=_env_flag(
            "MERCHANT_WRITE_REQUIRE_HOST_APPROVAL_PDP_PUBLISH",
            DEFAULT_CONFIG.require_host_approval_pdp_module_publish,
        ),
    )


# --------------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------------


def _field_chars(value: Any) -> int:
    """Characters a field's new value costs. Strings measure directly; anything else
    measures as the JSON we would persist, which is what the size ceiling is about."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _as_price(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _as_quantity(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def check_guardrails(
    kind: str,
    items: Sequence[WriteItem],
    config: Optional[MerchantWriteGuardrailConfig] = None,
) -> List[str]:
    """One operator-readable message per guardrail the items break; empty to proceed.

    Pure: no I/O, no exceptions for ordinary refusals. Callers turn a non-empty result
    into their lane's own refusal shape (a `GuardrailViolation` the route maps to 4xx,
    or a `blocked` status envelope).
    """
    config = config if config is not None else current_config()
    items = list(items or [])
    violations: List[str] = []

    if len(items) > config.max_items_per_change:
        violations.append(
            f"change touches {len(items)} fields and the limit is "
            f"{config.max_items_per_change} per change; split it into separate changes "
            "within the limit, each approved on its own"
        )

    protected = {name.casefold() for name in config.protected_fields}
    content_blocked = {name.casefold() for name in config.content_update_blocked_fields}
    price_bearing = {name.casefold() for name in config.price_bearing_fields}

    seen: set = set()
    for item in items:
        name = str(item.field)
        folded = name.casefold()
        target = str(item.target)

        # Every cap below is per item, so a target repeated inside one change would
        # pass each cap once per repeat and then apply the SUM of the repeats.
        if (target, folded) in seen:
            violations.append(
                f"'{name}' on {target} appears more than once in this change — "
                "stage one line per field"
            )
        seen.add((target, folded))

        if folded in protected:
            violations.append(
                f"field '{name}' on {target} is protected and cannot be changed by an "
                "assistant lane"
            )

        if kind in CONTENT_KINDS and folded in content_blocked:
            violations.append(
                f"'{name}' cannot be changed through a content update on {target} — "
                "stage it as a price update or an inventory action so its own limits apply"
            )

        chars = _field_chars(item.after)
        if chars > config.max_field_chars:
            violations.append(
                f"'{name}' on {target} is {chars} characters and the limit is "
                f"{config.max_field_chars}; shorten it before staging"
            )

        if kind == KIND_PRICE_UPDATE and folded in price_bearing:
            before = _as_price(item.before)
            after = _as_price(item.after)
            if after is None:
                violations.append(f"price for {target} must be a positive amount")
            elif before is None:
                violations.append(
                    f"price for {target} has no grounded current price — the movement "
                    "cap cannot be checked"
                )
            else:
                delta_pct = abs(after - before) / before * 100
                if delta_pct > config.max_price_delta_pct:
                    violations.append(
                        f"price move of {delta_pct:.0f}% on {target} exceeds the "
                        f"{config.max_price_delta_pct:.0f}% per-change limit"
                    )

        if kind == KIND_INVENTORY_ACTION:
            added = _as_quantity(item.after) - _as_quantity(item.before)
            if added > config.max_restock_quantity:
                violations.append(
                    f"restock of {added} units on {target} exceeds the "
                    f"{config.max_restock_quantity}-unit per-change limit"
                )

    return violations


def requires_host_approval(
    kind: str, config: Optional[MerchantWriteGuardrailConfig] = None
) -> bool:
    """Whether this lane's apply needs a mark only a host can set."""
    config = config if config is not None else current_config()
    if kind == KIND_STORE_CONTENT_WRITEBACK:
        return config.require_host_approval_store_writeback
    if kind == KIND_PDP_MODULE_CONTENT:
        return config.require_host_approval_pdp_module_publish
    return False


def check_host_approval(
    kind: str,
    *,
    actor_kind: str,
    config: Optional[MerchantWriteGuardrailConfig] = None,
) -> List[str]:
    """Empty when the write may be applied; one message when it may not.

    `actor_kind` is the HOST's statement about who is asking, taken from the
    authenticated caller. It is never read out of a payload, a rubric, or any other
    model output, so model output cannot set the approval mark: a rubric that says
    "approved" is data, and this function never looks at it.
    """
    config = config if config is not None else current_config()
    if not requires_host_approval(kind, config):
        return []
    if actor_kind == ACTOR_HUMAN or actor_kind == ACTOR_SYSTEM:
        return []
    return [
        f"this {kind} write requires host approval and the caller is '{actor_kind}'; "
        "a model verdict does not approve a merchant write — a person has to ask for "
        "it through the approval surface"
    ]


def guardrail_block_message(violations: Sequence[str]) -> str:
    """One line an operator can act on, for a log or an API detail field."""
    return "Refused by the merchant write guardrails: " + "; ".join(violations)


# --------------------------------------------------------------------------------
# Turning a lane's payload into items
# --------------------------------------------------------------------------------


def items_from_payload(
    target: str,
    payload: Optional[Mapping[str, Any]],
    *,
    before: Optional[Mapping[str, Any]] = None,
    skip_fields: Iterable[str] = (),
) -> List[WriteItem]:
    """One item per top-level key of a proposed payload.

    `before` is the currently-live payload when the lane has it, so a movement cap can
    be checked; a lane with no grounded prior passes nothing and the price rule says so
    rather than assuming a baseline.
    """
    if not isinstance(payload, Mapping):
        return []
    skip = {str(name).casefold() for name in skip_fields}
    prior: Mapping[str, Any] = before if isinstance(before, Mapping) else {}
    out: List[WriteItem] = []
    for key, value in payload.items():
        name = str(key)
        if name.casefold() in skip:
            continue
        out.append(
            WriteItem(target=target, field=name, before=prior.get(key), after=value)
        )
    return out


#: The copy keys `services/shopify_content_writeback._build_metafield_value` actually
#: produces, minus the `_provenance` block it stamps on afterwards. Kept here so the
#: guardrail lane knows the shape it is bounding; a completeness test asserts this set
#: still equals what that function returns, so widening the metafield without widening
#: the guardrail is a red test rather than an unbounded write.
STORE_WRITEBACK_COPY_FIELDS: Tuple[str, ...] = (
    "title",
    "summary",
    "description",
    "bullets",
    "usage",
)
