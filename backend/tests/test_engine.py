import pytest
from sqlalchemy import func, select

from ledgerloop import audit, db, replay
from ledgerloop.engine import Engine
from ledgerloop.events import Event


def E(eid, type_, payload, at="2026-09-10T12:00:00Z"):
    return Event(eid, type_, at, payload)


def story():
    return [
        E("e1", "customer.applied", {"customer_id": "c1", "legal_name": "Acme Industrial Inc", "tax_id": "T1",
                                      "country": "US", "requested_limit_cents": 50_000_00, "annual_revenue_cents": 5_000_000_00}),
        E("e2", "order.placed", {"order_id": "o1", "customer_id": "c1", "amount_cents": 10_000_00}),
        E("e3", "invoice.issued", {"invoice_id": "INV-100001", "order_id": "o1", "customer_id": "c1",
                                    "amount_cents": 10_000_00, "due_date": "2026-10-10"}),
        E("e4", "order.placed", {"order_id": "o2", "customer_id": "c1", "amount_cents": 60_000_00}),  # over limit
        E("e5", "payment.received", {"payment_id": "p1", "payer_name": "ACME INDUSTRIAL", "amount_cents": 10_000_00,
                                      "memo": "INV-100001", "bank_ref": "B1"}),
    ]


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_end_to_end_batch(engine):
    eng = Engine(engine)
    res = eng.process(story())
    assert res.new == 5 and res.decisions == 4 and res.auto_resolved == 3 and res.exceptions_opened == 1
    assert eng.state.invoices["INV-100001"].open_cents == 0
    assert eng.state.orders["o2"].status == "held"
    assert count(engine, db.audit_log) == 4
    assert count(engine, db.tool_calls) == 1
    with engine.connect() as conn:
        assert audit.verify(conn).ok


def test_redelivered_batch_changes_nothing(engine):
    eng = Engine(engine)
    eng.process(story())
    before = eng.state.state_hash()
    res = eng.process(story())
    assert res.new == 0 and res.duplicate == 5 and res.decisions == 0
    assert eng.state.state_hash() == before
    assert count(engine, db.decisions) == 4


def test_fresh_engine_boots_to_same_state(engine):
    eng = Engine(engine)
    for ev in story():  # one event per batch this time
        eng.process([ev])
    other = Engine(engine)
    other.boot()
    assert other.state.state_hash() == eng.state.state_hash()
    assert other.chain.head_hash == eng.chain.head_hash


def test_replay_verify_matches_live_decisions(engine):
    eng = Engine(engine)
    eng.process(story())
    with engine.connect() as conn:
        v = replay.verify(conn)
    assert v["ok"] and v["decisions_checked"] == 4
    assert v["state_hash"] == eng.state.state_hash()


def test_failed_commit_rolls_back_state(engine, monkeypatch):
    eng = Engine(engine)
    eng.process(story()[:3])
    good = eng.state.state_hash()

    real_next_row = eng.chain.next_row

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(eng.chain, "next_row", boom)
    with pytest.raises(RuntimeError):
        eng.process(story()[3:])
    # state was rebuilt from the database, so the half-processed batch left no trace
    assert eng.state.state_hash() == good
    assert "o2" not in eng.state.orders
    res = eng.process(story()[3:])
    assert res.new == 2
    with engine.connect() as conn:
        assert audit.verify(conn).ok and replay.verify(conn)["ok"]
    assert real_next_row  # silence unused warning


def test_human_resolution_releases_held_order(engine):
    eng = Engine(engine)
    eng.process(story())
    exc_id = next(iter(eng.state.open_exceptions))
    res = eng.process([E("r1", "exception.resolved", {"exception_id": exc_id, "resolution": {"action": "release"},
                                                       "resolved_by": "amaan"})])
    assert res.decisions == 1
    assert eng.state.orders["o2"].status == "released"
    assert not eng.state.open_exceptions
    with engine.connect() as conn:
        row = conn.execute(select(db.exceptions.c.status).where(db.exceptions.c.exception_id == exc_id)).scalar_one()
        assert row == "resolved"
        assert replay.verify(conn)["ok"]


def test_invalid_resolution_is_recorded_but_changes_nothing(engine):
    eng = Engine(engine)
    eng.process(story())
    exc_id = next(iter(eng.state.open_exceptions))
    eng.process([E("r1", "exception.resolved", {"exception_id": exc_id, "resolution": {"action": "apply"}, "resolved_by": "x"})])
    assert exc_id in eng.state.open_exceptions


def test_invalid_events_are_rejected_not_logged(engine):
    eng = Engine(engine)
    res = eng.process([E("bad", "order.placed", {"order_id": "o9"})])
    assert res.new == 0 and len(res.invalid) == 1
    assert count(engine, db.events) == 0


def test_what_if_reports_policy_changes(engine):
    eng = Engine(engine)
    eng.process(story())
    # a $52k order is 4% over the $50k limit: released under v1 tolerance, held under v2-strict
    eng.process([E("e6", "order.placed", {"order_id": "o3", "customer_id": "c1", "amount_cents": 52_000_00})])
    with engine.connect() as conn:
        diff = replay.what_if(conn, "v2-strict")
    assert diff["changed"] == 1
    assert "risk: released:- -> held:over_limit" in diff["changes_by_type"]
