"""Live engine: append a batch to the log, run agents, persist everything atomically.

One transaction per batch covers the new events, the decisions, the exceptions,
the recorded tool outputs, and the audit entries. Either the whole batch is in
the database or none of it is, and the upstream ack only happens after commit.
If a commit fails, in-memory state may be ahead of the database, so we throw it
away and rebuild from the log.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.engine import Engine as DbEngine

from . import db, eventlog, replay
from .audit import AuditChain
from .events import Event, InvalidEvent, canonical_json, sha256_hex
from .policy import DEFAULT_POLICY, Policy
from .processor import Processor, default_registry
from .state import State
from .tools.recorder import RecordingTools, ToolFn


@dataclass
class BatchResult:
    received: int = 0
    new: int = 0
    duplicate: int = 0
    conflict: int = 0
    invalid: list[dict[str, str]] = field(default_factory=list)
    decisions: int = 0
    auto_resolved: int = 0
    exceptions_opened: int = 0
    elapsed_s: float = 0.0
    decision_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["elapsed_s"] = round(self.elapsed_s, 4)
        return d


class Engine:
    def __init__(self, db_engine: DbEngine, policy: Policy = DEFAULT_POLICY, registry: dict[str, ToolFn] | None = None):
        self.db = db_engine
        self.policy = policy
        self.registry = registry or default_registry()
        self._lock = threading.Lock()
        self.state = State()
        self.tools = RecordingTools(self.registry, mode="live")
        self.proc = Processor(self.state, self.tools, lambda _e: self.policy)
        self.chain = AuditChain()
        self.last_seq = 0
        self.totals = {"batches": 0, "events": 0, "busy_s": 0.0}

    def boot(self) -> dict[str, Any]:
        """Rebuild in-memory state from the log."""
        with self._lock:
            return self._rebuild()

    def _rebuild(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            result = replay.fold(conn)
            self.chain = AuditChain.load(conn)
        self.state = result.state
        self.tools = RecordingTools(self.registry, mode="live")
        self.proc = Processor(self.state, self.tools, lambda _e: self.policy)
        self.last_seq = result.last_seq
        return {"events": result.events, "elapsed_s": round(result.elapsed_s, 3)}

    def process(self, raw: list[Event]) -> BatchResult:
        t0 = perf_counter()
        res = BatchResult(received=len(raw))
        valid: list[Event] = []
        for e in raw:
            try:
                e.validate()
                valid.append(e)
            except InvalidEvent as err:
                res.invalid.append({"event_id": e.event_id, "error": str(err)})

        with self._lock:
            try:
                with self.db.begin() as conn:
                    self._process_locked(conn, valid, res)
            except Exception:
                self._rebuild()
                raise
            res.elapsed_s = perf_counter() - t0
            self.totals["batches"] += 1
            self.totals["events"] += res.new
            self.totals["busy_s"] += res.elapsed_s
        return res

    def _process_locked(self, conn, valid: list[Event], res: BatchResult) -> None:
        appended = eventlog.append_batch(conn, valid)
        decision_rows: list[dict[str, Any]] = []
        exception_rows: list[dict[str, Any]] = []
        closes: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []

        for r in appended:
            if r.status == "duplicate":
                res.duplicate += 1
                continue
            if r.status == "conflict":
                res.conflict += 1
                continue
            res.new += 1
            ev = r.event
            self.last_seq = max(self.last_seq, ev.seq or 0)
            d = self.proc.handle(ev)
            if d is None:
                continue
            body = d.body()
            h = sha256_hex(canonical_json(body))
            decision_rows.append(
                {
                    "decision_id": d.decision_id,
                    "event_seq": ev.seq,
                    "event_id": ev.event_id,
                    "agent": d.agent,
                    "action": d.action,
                    "subject_id": d.subject_id,
                    "auto_resolved": d.auto_resolved,
                    "reasons": d.reasons,
                    "detail": d.detail,
                    "policy_version": d.policy_version,
                    "decision_hash": h,
                }
            )
            kind = "resolution" if d.agent == "human" else "decision"
            audit_rows.append(self.chain.next_row(ev.seq, kind, d.decision_id, {**body, "decision_hash": h}))
            res.decisions += 1
            res.decision_ids.append(d.decision_id)
            if d.agent != "human" and d.auto_resolved:
                res.auto_resolved += 1
            if d.exception_kind:
                res.exceptions_opened += 1
                exception_rows.append(
                    {
                        "exception_id": d.exception_id,
                        "decision_id": d.decision_id,
                        "agent": d.agent,
                        "kind": d.exception_kind,
                        "subject_id": d.subject_id,
                        "summary": d.exception_summary,
                        "status": "open",
                        "opened_seq": ev.seq,
                    }
                )
            for eff in d.effects:
                if eff.op == "exception.close":
                    closes.append({"exception_id": eff.data["exception_id"], "seq": ev.seq, "resolution": eff.data})

        tool_rows = self.tools.drain()
        if decision_rows:
            conn.execute(insert(db.decisions), decision_rows)
        if exception_rows:
            conn.execute(insert(db.exceptions), exception_rows)
        for c in closes:
            conn.execute(
                update(db.exceptions)
                .where(db.exceptions.c.exception_id == c["exception_id"])
                .values(status="resolved", resolved_seq=c["seq"], resolution=c["resolution"])
            )
        if tool_rows:
            conn.execute(insert(db.tool_calls), tool_rows)
        if audit_rows:
            conn.execute(insert(db.audit_log), audit_rows)
