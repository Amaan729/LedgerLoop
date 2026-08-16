"""Risk agent: release or hold each order against the customer's credit."""

from __future__ import annotations

from ..events import ORDER_PLACED, Event, parse_ts
from ..policy import Policy
from ..state import Effect, State
from .base import Decision, Tools, needs_review

NAME = "risk"


class RiskAgent:
    name = NAME
    handles = frozenset({ORDER_PLACED})

    def decide(self, event: Event, state: State, policy: Policy, tools: Tools) -> Decision:
        p = event.payload
        oid, cid, amount = p["order_id"], p["customer_id"], p["amount_cents"]
        order = {"order_id": oid, "customer_id": cid, "amount_cents": amount, "placed_at": event.occurred_at}

        customer = state.customers.get(cid)
        if customer is None:
            return needs_review(
                NAME, oid, "unknown_customer", f"order for unknown customer {cid}", ["unknown_customer"],
                effects=[Effect("order.upsert", {**order, "status": "held"})], action="held",
            )
        if customer.status == "rejected":
            return Decision(
                NAME, "rejected", oid, True, ["customer_rejected"],
                effects=[Effect("order.upsert", {**order, "status": "rejected"})],
            )
        if customer.status != "approved":
            return needs_review(
                NAME, oid, "customer_not_approved", f"{customer.legal_name} is still {customer.status}",
                ["customer_not_approved"], effects=[Effect("order.upsert", {**order, "status": "held"})], action="held",
            )

        open_ar = state.open_ar_cents(cid)
        uninvoiced = state.uninvoiced_exposure_cents(cid)
        exposure_after = open_ar + uninvoiced + amount
        limit = customer.credit_limit_cents
        detail = {
            "open_ar_cents": open_ar,
            "uninvoiced_cents": uninvoiced,
            "exposure_after_cents": exposure_after,
            "limit_cents": limit,
        }

        as_of = parse_ts(event.occurred_at).date()
        overdue = state.overdue_invoices(cid, as_of, policy.overdue_grace_days)
        if overdue:
            oldest = min(overdue, key=lambda inv: inv.due_date)
            overdue_cents = sum(inv.open_cents for inv in overdue)
            return needs_review(
                NAME, oid, "overdue_balance",
                f"{len(overdue)} invoice(s) over {policy.overdue_grace_days} days past due "
                f"(${overdue_cents / 100:,.0f}, oldest {oldest.invoice_id})",
                ["overdue_balance"], {**detail, "overdue_cents": overdue_cents, "oldest_overdue": oldest.invoice_id},
                effects=[Effect("order.upsert", {**order, "status": "held"})], action="held",
            )

        if exposure_after <= limit:
            return Decision(
                NAME, "released", oid, True, ["within_limit"], detail,
                effects=[Effect("order.upsert", {**order, "status": "released"})],
            )
        # A small overage on a customer with nothing overdue isn't worth a human's time.
        if exposure_after * 100 <= limit * (100 + policy.over_limit_tolerance_pct):
            return Decision(
                NAME, "released", oid, True, ["over_limit_within_tolerance"], detail,
                effects=[Effect("order.upsert", {**order, "status": "released"})],
            )
        return needs_review(
            NAME, oid, "over_limit",
            f"{customer.legal_name} would be at ${exposure_after / 100:,.0f} of a ${limit / 100:,.0f} limit",
            ["over_limit"], detail, effects=[Effect("order.upsert", {**order, "status": "held"})], action="held",
        )
