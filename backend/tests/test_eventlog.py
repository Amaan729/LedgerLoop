import pytest

from ledgerloop import eventlog
from ledgerloop.events import Event, InvalidEvent


def pay(event_id, amount=1000, memo="INV-1"):
    return Event(
        event_id,
        "payment.received",
        "2026-09-01T10:00:00Z",
        {"payment_id": event_id, "payer_name": "Acme", "amount_cents": amount, "memo": memo},
    )


def test_append_assigns_increasing_seq(engine):
    with engine.begin() as conn:
        res = eventlog.append_batch(conn, [pay("p1"), pay("p2"), pay("p3")])
    assert [r.status for r in res] == ["new", "new", "new"]
    seqs = [r.event.seq for r in res]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3


def test_redelivery_is_a_noop(engine):
    with engine.begin() as conn:
        first = eventlog.append_batch(conn, [pay("p1")])
    with engine.begin() as conn:
        again = eventlog.append_batch(conn, [pay("p1"), pay("p2")])
    assert again[0].status == "duplicate"
    assert again[0].event.seq == first[0].event.seq
    assert again[1].status == "new"
    with engine.connect() as conn:
        assert len(list(eventlog.read_events(conn))) == 2


def test_duplicate_inside_one_batch(engine):
    with engine.begin() as conn:
        res = eventlog.append_batch(conn, [pay("p1"), pay("p1")])
    assert [r.status for r in res] == ["new", "duplicate"]


def test_same_id_different_content_is_a_conflict(engine):
    with engine.begin() as conn:
        eventlog.append_batch(conn, [pay("p1", amount=1000)])
    with engine.begin() as conn:
        res = eventlog.append_batch(conn, [pay("p1", amount=9999)])
    assert res[0].status == "conflict"


def test_read_events_in_order_across_chunks(engine):
    with engine.begin() as conn:
        eventlog.append_batch(conn, [pay(f"p{i}") for i in range(25)])
    with engine.connect() as conn:
        got = [e.event_id for e in eventlog.read_events(conn, chunk=7)]
    assert got == [f"p{i}" for i in range(25)]


def test_validation_rejects_float_money():
    ev = Event("x", "payment.received", "2026-09-01T00:00:00Z", {"payment_id": "x", "payer_name": "A", "amount_cents": 10.5})
    with pytest.raises(InvalidEvent):
        ev.validate()


def test_validation_rejects_missing_fields():
    ev = Event("x", "order.placed", "2026-09-01T00:00:00Z", {"order_id": "o1"})
    with pytest.raises(InvalidEvent):
        ev.validate()
