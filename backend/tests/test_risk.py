from dataclasses import replace

from ledgerloop.agents.risk import RiskAgent
from ledgerloop.policy import DEFAULT_POLICY
from ledgerloop.state import Effect, State

from .helpers import NoTools, approved_customer, ev, invoice

agent = RiskAgent()


def order(amount, oid="o1", cid="c1", at="2026-09-10T12:00:00Z"):
    return ev("order.placed", {"order_id": oid, "customer_id": cid, "amount_cents": amount}, at=at)


def decide(event, state, policy=DEFAULT_POLICY):
    return agent.decide(event, state, policy, NoTools())


def test_order_within_limit_is_released():
    s = State()
    approved_customer(s, limit=100_000_00)
    invoice(s, "INV-1", amount=40_000_00)
    d = decide(order(50_000_00), s)
    assert d.action == "released" and d.auto_resolved
    assert d.detail["exposure_after_cents"] == 90_000_00


def test_released_but_uninvoiced_orders_count_toward_exposure():
    s = State()
    approved_customer(s, limit=100_000_00)
    s.apply(Effect("order.upsert", {"order_id": "o0", "customer_id": "c1", "amount_cents": 80_000_00, "placed_at": "x", "status": "released"}))
    d = decide(order(30_000_00), s)
    assert d.exception_kind == "over_limit"


def test_invoicing_an_order_moves_exposure_from_orders_to_ar():
    s = State()
    approved_customer(s, limit=100_000_00)
    s.apply(Effect("order.upsert", {"order_id": "o0", "customer_id": "c1", "amount_cents": 80_000_00, "placed_at": "x", "status": "released"}))
    invoice(s, "INV-1", amount=80_000_00, order_id="o0")
    assert s.uninvoiced_exposure_cents("c1") == 0
    assert s.open_ar_cents("c1") == 80_000_00


def test_small_overage_is_released_within_tolerance():
    s = State()
    approved_customer(s, limit=100_000_00)
    d = decide(order(104_000_00), s)
    assert d.action == "released" and d.reasons == ["over_limit_within_tolerance"]


def test_strict_policy_has_no_tolerance():
    s = State()
    approved_customer(s, limit=100_000_00)
    d = decide(order(104_000_00), s, replace(DEFAULT_POLICY, over_limit_tolerance_pct=0))
    assert d.exception_kind == "over_limit"


def test_overdue_invoice_holds_even_small_orders():
    s = State()
    approved_customer(s, limit=100_000_00)
    invoice(s, "INV-1", amount=5_000_00, due="2026-07-01")
    d = decide(order(1_000_00, at="2026-09-10T00:00:00Z"), s)
    assert d.action == "held" and d.exception_kind == "overdue_balance"
    assert d.detail["oldest_overdue"] == "INV-1"


def test_recently_due_invoice_is_within_grace():
    s = State()
    approved_customer(s, limit=100_000_00)
    invoice(s, "INV-1", amount=5_000_00, due="2026-09-01")
    assert decide(order(1_000_00), s).action == "released"


def test_unapproved_customer_is_held():
    s = State()
    s.apply(Effect("customer.upsert", {"customer_id": "c1", "legal_name": "X", "country": "US", "status": "review"}))
    assert decide(order(100), s).exception_kind == "customer_not_approved"


def test_rejected_customer_order_is_auto_rejected():
    s = State()
    s.apply(Effect("customer.upsert", {"customer_id": "c1", "legal_name": "X", "country": "US", "status": "rejected"}))
    d = decide(order(100), s)
    assert d.action == "rejected" and d.auto_resolved


def test_unknown_customer_is_held():
    assert decide(order(100, cid="nope"), State()).exception_kind == "unknown_customer"
