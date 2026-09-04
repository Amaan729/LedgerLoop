"""Redis Streams ingestion.

Producers XADD events to one stream. The engine reads with a consumer group,
processes a batch, and XACKs only after the batch has committed. If the process
dies between commit and ack, Redis redelivers the batch on restart and the
idempotent log turns it into a no-op. At-least-once delivery plus idempotent
appends gives effectively-once processing.

Entries that aren't valid JSON events are moved to a dead-letter stream so one
bad producer can't wedge the consumer.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterable

import redis

from .engine import BatchResult, Engine
from .events import Event

log = logging.getLogger(__name__)


def publish(r: redis.Redis, stream: str, events: Iterable[Event], chunk: int = 1000) -> int:
    n = 0
    pipe = r.pipeline(transaction=False)
    for ev in events:
        pipe.xadd(stream, {"event": json.dumps(ev.to_dict(), separators=(",", ":"))})
        n += 1
        if n % chunk == 0:
            pipe.execute()
    pipe.execute()
    return n


class StreamConsumer:
    def __init__(
        self,
        r: redis.Redis,
        engine: Engine,
        stream: str,
        group: str,
        consumer: str = "engine-1",
        batch_size: int = 500,
        block_ms: int = 1000,
    ) -> None:
        self.r = r
        self.engine = engine
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.dead_letter = f"{stream}:dead"
        self._stop = threading.Event()
        self._recovering = True  # first drain anything we were handed before a crash

    def ensure_group(self) -> None:
        try:
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def poll_once(self) -> BatchResult | None:
        start_id = "0" if self._recovering else ">"
        resp = self.r.xreadgroup(
            self.group, self.consumer, {self.stream: start_id}, count=self.batch_size,
            block=None if self._recovering else self.block_ms,
        )
        entries: list[tuple[Any, dict[Any, Any]]] = resp[0][1] if resp else []
        if not entries:
            if self._recovering:
                self._recovering = False
            return None

        ids: list[Any] = []
        events: list[Event] = []
        for entry_id, fields in entries:
            ids.append(entry_id)
            raw = fields.get(b"event") or fields.get("event")
            try:
                events.append(Event.from_dict(json.loads(raw)))
            except Exception as e:  # noqa: BLE001 - anything unparseable is dead-lettered
                self.r.xadd(self.dead_letter, {"entry_id": entry_id, "raw": raw or b"", "error": str(e)})

        result = self.engine.process(events)
        for bad in result.invalid:
            self.r.xadd(self.dead_letter, {"event_id": bad["event_id"], "error": bad["error"]})
        self.r.xack(self.stream, self.group, *ids)
        return result

    def run(self) -> None:
        self.ensure_group()
        while not self._stop.is_set():
            try:
                self.poll_once()
            except redis.ConnectionError:
                log.warning("redis unavailable, retrying")
                self._stop.wait(1.0)
            except Exception:
                # Batch failed (e.g. database down). Not acked, so it will be redelivered.
                log.exception("batch failed; will retry from pending")
                self._recovering = True
                self._stop.wait(1.0)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="stream-consumer", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def lag(self) -> dict[str, Any]:
        try:
            groups = self.r.xinfo_groups(self.stream)
        except redis.ResponseError:
            return {}
        for g in groups:
            name = g.get("name") or g.get(b"name")
            if name in (self.group, self.group.encode()):
                return {"pending": g.get("pending"), "lag": g.get("lag")}
        return {}
