import os
import uuid

import pytest

from ledgerloop import bus
from ledgerloop.engine import Engine
from ledgerloop.events import Event

redis = pytest.importorskip("redis")


@pytest.fixture
def r():
    client = redis.Redis.from_url(os.getenv("LEDGERLOOP_TEST_REDIS_URL", "redis://localhost:6379/15"))
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("redis not running")
    yield client
    client.flushdb()


def events(n, start=0):
    return [
        Event(f"e{i}", "customer.applied", "2026-09-10T00:00:00Z",
              {"customer_id": f"c{i}", "legal_name": f"Company {i} Holdings", "tax_id": f"T{i}",
               "country": "US", "requested_limit_cents": 1_000_00})
        for i in range(start, start + n)
    ]


def consumer(r, engine, stream):
    c = bus.StreamConsumer(r, engine, stream, "g", batch_size=50, block_ms=50)
    c.ensure_group()
    return c


def drain(c):
    """Poll until a live (non-recovery) read comes back empty."""
    total = 0
    while True:
        was_recovering = c._recovering
        res = c.poll_once()
        if res is None:
            if was_recovering:
                continue
            return total
        total += res.new


def test_consume_and_ack(r, engine):
    stream = f"s-{uuid.uuid4().hex}"
    bus.publish(r, stream, events(120))
    eng = Engine(engine)
    c = consumer(r, eng, stream)
    assert drain(c) == 120
    assert len(eng.state.customers) == 120
    assert c.lag()["pending"] == 0


def test_crash_between_commit_and_ack_is_a_noop_on_redelivery(r, engine, monkeypatch):
    stream = f"s-{uuid.uuid4().hex}"
    bus.publish(r, stream, events(30))
    eng = Engine(engine)
    c = consumer(r, eng, stream)
    c._recovering = False

    monkeypatch.setattr(r, "xack", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("died")))
    with pytest.raises(ConnectionError):
        c.poll_once()  # committed to the db, never acked
    monkeypatch.undo()

    # restart: new engine boots from the db, new consumer drains its pending list first
    eng2 = Engine(engine)
    eng2.boot()
    c2 = consumer(r, eng2, stream)
    res = c2.poll_once()
    assert res.duplicate == 30 and res.new == 0
    assert len(eng2.state.customers) == 30


def test_garbage_goes_to_dead_letter(r, engine):
    stream = f"s-{uuid.uuid4().hex}"
    r.xadd(stream, {"event": "{not json"})
    bus.publish(r, stream, events(2))
    eng = Engine(engine)
    c = consumer(r, eng, stream)
    drain(c)
    assert r.xlen(f"{stream}:dead") == 1
    assert len(eng.state.customers) == 2
