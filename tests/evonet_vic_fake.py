"""An in-repo fake of the rail provider's VIC order API, at the WIRE (a `vic_order.VicOrderTransport`), so
the production client's classification and credential stripping run on everything it answers.

It answers in the shape we are asking the provider for (reports/, local; the contract summary is in
services/payment_orchestration/vic_order.py). Not a test file (no `test_` prefix): the VIC worker tests
import it. NEVER wired into a process: `vic_order.from_env()` takes no transport from the environment.

The script a test drives:
    fake.buyer("ap_...", "approved")              # the buyer's choice on the hosted page
    fake.checkout("ap_...", "completed", order_number="1001", total_minor=4650)
    fake.fail_next("submit", TimeoutError())      # or an HTTP status, for one call
    fake.leak = {...}                             # extra fields merged into EVERY answer (credentials)
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Tuple

HOSTED_HOST = "pay.vic-enabler.test"


class FakeEvonetVic:
    def __init__(self) -> None:
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, Any]] = []
        self._faults: Dict[str, List[Any]] = {"submit": [], "status": []}
        #: Merged into every answer body (top level) and its `authorization` / `checkout` objects.
        self.leak: Dict[str, Any] = {}
        #: When True a submit is RECORDED and then answered with `fail_next`'s fault (an unseen create).
        self.record_before_fault = True
        self.page_seconds = 900

    # ── the script ──────────────────────────────────────────────────────────────────────────

    def fail_next(self, op: str, fault: Any) -> "FakeEvonetVic":
        self._faults[op].append(fault)
        return self

    def buyer(self, ref: str, status: str) -> None:
        self.orders[ref]["authorization"]["status"] = status
        if status != "pending":
            self.orders[ref]["authorization"].pop("hosted_url", None)

    def checkout(self, ref: str, status: str, **fields: Any) -> None:
        self.orders[ref]["checkout"] = {"status": status, **fields}

    def submits(self) -> List[Mapping[str, Any]]:
        return [body for op, body in self.calls if op == "submit"]

    # ── the wire ────────────────────────────────────────────────────────────────────────────

    async def submit_order(self, body: Mapping[str, Any]) -> Tuple[int, Any]:
        self.calls.append(("submit", copy.deepcopy(dict(body))))
        ref = str(body["client_reference"])
        fault = self._faults["submit"].pop(0) if self._faults["submit"] else None
        if fault is not None and not (isinstance(fault, Exception) and self.record_before_fault):
            return await self._fault(fault)
        if ref not in self.orders:
            self.orders[ref] = {
                "order_id": f"evo_ord_{len(self.orders) + 1:04d}",
                "client_reference": ref,
                "amount_minor": body["amount_minor"],
                "currency": body["currency"],
                "authorization": {
                    "status": "pending",
                    "hosted_url": f"https://{HOSTED_HOST}/vic/approve?o={len(self.orders) + 1:04d}",
                    "expires_in": self.page_seconds,
                },
            }
        if fault is not None:
            return await self._fault(fault)
        return 201, self._answer(ref)

    async def get_order(self, client_reference: str) -> Tuple[int, Any]:
        self.calls.append(("status", client_reference))
        fault = self._faults["status"].pop(0) if self._faults["status"] else None
        if fault is not None:
            return await self._fault(fault)
        if client_reference not in self.orders:
            return 404, {"error": {"code": "order_not_found", "retryable": False}}
        return 200, self._answer(client_reference)

    async def _fault(self, fault: Any) -> Tuple[int, Any]:
        if isinstance(fault, Exception):
            raise fault
        if isinstance(fault, tuple):
            return fault
        return int(fault), None

    def _answer(self, ref: str) -> Dict[str, Any]:
        order = copy.deepcopy(self.orders[ref])
        body: Dict[str, Any] = {
            "order_id": order["order_id"],
            "client_reference": ref,
            "authorization": order["authorization"],
        }
        if "checkout" in order:
            body["checkout"] = order["checkout"]
        if self.leak:
            body.update(copy.deepcopy(self.leak))
            body["authorization"].update(copy.deepcopy(self.leak))
            if "checkout" in body:
                body["checkout"].update(copy.deepcopy(self.leak))
        return body

