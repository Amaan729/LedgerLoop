"""Turn a human's resolution of an exception into effects.

A resolution arrives as an `exception.resolved` event, so it lives in the log
next to everything else and replays like everything else. It is validated
against current state: a person can't apply more cash than a payment has or
close an exception that is already closed.
"""

from __future__ import annotations

from typing import Any

from .agents.base import Decision
from .events import Event
from .state import Effect, State

NAME = "human"


class InvalidResolution(ValueError):
    pass


def resolve(event: Event, state: State) -> Decision:
    p = event.payload
    exc_id = p["exception_id"]
    res: dict[str, Any] = p["resolution"] if isinstance(p["resolution"], dict) else {"action": p["resolution"]}
    action = res.get("action", "")
    exc = state.open_exceptions.get(exc_id)
    base_detail = {"exception_id": exc_id, "resolved_by": p["resolved_by"], "resolution": res, "note": p.get("note")}

    if exc is None:
        return Decision(NAME, "ignored", exc_id, False, ["exception_not_open"], base_detail)

    try:
        effects = _effects_for(exc, action, res, state)
    except InvalidResolution as e:
        return Decision(NAME, "resolution_rejected", exc_id, False, ["invalid_resolution"], {**base_detail, "error": str(e)})

    effects.append(Effect("exception.close", {"exception_id": exc_id, "resolution": res, "resolved_by": p["resolved_by"]}))
    return Decision(NAME, f"resolved:{action}", exc["subject_id"], False, [f"{exc['agent']}:{exc['kind']}"], base_detail, effects)


def _effects_for(exc: dict[str, Any], action: str, res: dict[str, Any], state: State) -> list[Effect]:
    agent, subject = exc["agent"], exc["subject_id"]
    if action == "dismiss":
        return []

    if agent == "onboarding":
        c = state.customers.get(subject)
        if c is None:
            raise InvalidResolution("customer not found")
        base = {"customer_id": c.customer_id, "legal_name": c.legal_name, "tax_id": c.tax_id, "country": c.country}
        if action == "approve":
            limit = res.get("credit_limit_cents")
            if not isinstance(limit, int) or limit < 0:
                raise InvalidResolution("approve needs integer credit_limit_cents")
            return [Effect("customer.upsert", {**base, "status": "approved", "credit_limit_cents": limit})]
        if action == "reject":
            return [Effect("customer.upsert", {**base, "status": "rejected", "credit_limit_cents": 0})]

    if agent == "risk":
        o = state.orders.get(subject)
        if o is None:
            raise InvalidResolution("order not found")
        base = {"order_id": o.order_id, "customer_id": o.customer_id, "amount_cents": o.amount_cents, "placed_at": o.placed_at}
        if action == "release":
            return [Effect("order.upsert", {**base, "status": "released"})]
        if action == "reject":
            return [Effect("order.upsert", {**base, "status": "rejected"})]

    if agent == "cash":
        pay = state.payments.get(subject)
        if pay is None:
            raise InvalidResolution("payment not found")
        base = {"payment_id": pay.payment_id, "payer_name": pay.payer_name, "amount_cents": pay.amount_cents,
                "received_at": pay.received_at, "memo": pay.memo, "bank_ref": pay.bank_ref}
        if action == "apply":
            allocations = res.get("allocations") or []
            if not allocations:
                raise InvalidResolution("apply needs allocations")
            total = 0
            owners = set()
            effects: list[Effect] = []
            for a in allocations:
                inv = state.invoices.get(a.get("invoice_id", ""))
                cents = a.get("cents")
                if inv is None:
                    raise InvalidResolution(f"unknown invoice {a.get('invoice_id')}")
                if not isinstance(cents, int) or cents <= 0 or cents > inv.open_cents:
                    raise InvalidResolution(f"bad amount for {inv.invoice_id} (open {inv.open_cents})")
                total += cents
                owners.add(inv.customer_id)
                effects.append(Effect("invoice.apply_cash", {"invoice_id": inv.invoice_id, "payment_id": pay.payment_id, "cents": cents}))
            if total > pay.unapplied_cents:
                raise InvalidResolution(f"allocations {total} exceed unapplied {pay.unapplied_cents}")
            if len(owners) != 1:
                raise InvalidResolution("allocations span customers")
            head = Effect("payment.upsert", {**base, "status": "applied", "customer_id": owners.pop(),
                                             "unapplied_cents": pay.unapplied_cents - total})
            return [head, *effects]
        if action in ("on_account", "refund"):
            return [Effect("payment.upsert", {**base, "status": "on_account" if action == "on_account" else "refunded"})]

    raise InvalidResolution(f"{action!r} is not a valid resolution for a {agent} exception")
