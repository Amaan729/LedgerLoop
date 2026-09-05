"""Append-only event log with idempotent appends.

Upstream delivery is at-least-once (Redis redelivers anything not acked, HTTP
clients retry). The log makes that safe: an event_id is only ever stored once,
so a retry is a no-op instead of a double-applied payment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from . import db
from .events import Event


@dataclass(frozen=True)
class AppendResult:
    event: Event
    status: str  # "new" | "duplicate" | "conflict"


def append_batch(conn: Connection, batch: list[Event]) -> list[AppendResult]:
    """Append events in order. Must run inside the caller's transaction.

    * duplicate: same event_id and same content as something already stored
    * conflict: same event_id but different content. Never applied; surfaced so a
      human can look at the producer.
    """
    if not batch:
        return []
    ids = list({e.event_id for e in batch})
    stored: dict[str, tuple[int, str]] = {}
    for chunk_start in range(0, len(ids), 1000):
        chunk = ids[chunk_start : chunk_start + 1000]
        rows = conn.execute(
            select(db.events.c.event_id, db.events.c.seq, db.events.c.fingerprint).where(
                db.events.c.event_id.in_(chunk)
            )
        )
        for event_id, seq, fp in rows:
            stored[event_id] = (seq, fp)

    results: list[AppendResult | None] = [None] * len(batch)
    to_insert: list[tuple[int, Event, str]] = []
    seen_in_batch: dict[str, str] = {}
    for i, ev in enumerate(batch):
        fp = ev.fingerprint()
        if ev.event_id in stored:
            seq, stored_fp = stored[ev.event_id]
            status = "duplicate" if stored_fp == fp else "conflict"
            results[i] = AppendResult(ev.with_seq(seq), status)
        elif ev.event_id in seen_in_batch:
            status = "duplicate" if seen_in_batch[ev.event_id] == fp else "conflict"
            results[i] = AppendResult(ev, status)
        else:
            seen_in_batch[ev.event_id] = fp
            to_insert.append((i, ev, fp))

    if to_insert:
        rows = [
            {
                "event_id": ev.event_id,
                "type": ev.type,
                "occurred_at": ev.occurred_at,
                "source": ev.source,
                "payload": ev.payload,
                "fingerprint": fp,
            }
            for _, ev, fp in to_insert
        ]
        returned = conn.execute(
            insert(db.events).returning(db.events.c.event_id, db.events.c.seq, sort_by_parameter_order=True),
            rows,
        ).all()
        seq_by_id = {event_id: seq for event_id, seq in returned}
        for i, ev, _ in to_insert:
            results[i] = AppendResult(ev.with_seq(seq_by_id[ev.event_id]), "new")

    return [r for r in results if r is not None]


def exists(conn: Connection, event_id: str) -> bool:
    return conn.execute(select(db.events.c.seq).where(db.events.c.event_id == event_id)).first() is not None


def read_events(conn: Connection, after_seq: int = 0, chunk: int = 5000) -> Iterator[Event]:
    """Stream the log in seq order."""
    last = after_seq
    while True:
        rows = conn.execute(
            select(
                db.events.c.seq,
                db.events.c.event_id,
                db.events.c.type,
                db.events.c.occurred_at,
                db.events.c.source,
                db.events.c.payload,
            )
            .where(db.events.c.seq > last)
            .order_by(db.events.c.seq)
            .limit(chunk)
        ).all()
        if not rows:
            return
        for seq, event_id, type_, occurred_at, source, payload in rows:
            yield Event(event_id, type_, occurred_at, payload, source, seq)
        last = rows[-1][0]
