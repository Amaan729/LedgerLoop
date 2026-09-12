from ledgerloop.engine import Engine
from ledgerloop.evaluate import evaluate
from ledgerloop.simulator import SimConfig, simulate


def small(**kw):
    return SimConfig(**{"customers": 60, "days": 60, **kw})


def test_same_seed_same_stream():
    a, _ = simulate(small(seed=7))
    b, _ = simulate(small(seed=7))
    assert [e.to_dict() for e in a] == [e.to_dict() for e in b]


def test_different_seed_different_stream():
    a, _ = simulate(small(seed=7))
    b, _ = simulate(small(seed=8))
    assert [e.event_id for e in a] != [e.event_id for e in b]


def test_stream_is_time_ordered_and_valid():
    events, _ = simulate(small())
    seen, stamps = set(), []
    for e in events:
        if e.event_id not in seen:  # a redelivery arrives later but keeps its original timestamp
            seen.add(e.event_id)
            stamps.append(e.occurred_at)
    assert stamps == sorted(stamps)
    assert len(seen) < len(events), "expected some redelivered events"
    for e in events:
        e.validate()


def test_closed_loop_never_invoices_a_held_order(engine):
    events, _ = simulate(small())
    eng = Engine(engine)
    eng.process(events)
    held_but_invoiced = [o for o in eng.state.orders.values() if o.status != "released" and o.invoiced]
    assert held_but_invoiced == []


def test_clerk_resolutions_are_all_accepted(engine):
    events, truth = simulate(small(customers=120, days=90))
    eng = Engine(engine)
    eng.process(events)
    with engine.connect() as conn:
        from sqlalchemy import select

        from ledgerloop import db

        actions = [a for (a,) in conn.execute(select(db.decisions.c.action).where(db.decisions.c.agent == "human"))]
        report = evaluate(conn, eng.state, truth)
    assert actions, "expected the clerk to resolve something"
    assert "resolution_rejected" not in actions
    assert report["wrong_auto_decisions"] <= 2
