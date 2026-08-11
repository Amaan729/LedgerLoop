"""Hash-chained audit log.

Every agent decision and every human resolution gets an entry whose hash covers
the previous entry's hash. Editing or deleting any row breaks every hash after
it, so `verify()` can say exactly where the history was touched.

This is tamper-evident, not tamper-proof: someone with write access to the table
could rewrite the whole chain. Anchoring the head hash somewhere external (for
example, printing it into a daily report) is the usual fix and is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import db
from .events import canonical_json, sha256_hex

GENESIS_HASH = "0" * 64


def entry_hash(prev_hash: str, position: int, event_seq: int, kind: str, ref_id: str, body: Any) -> str:
    return sha256_hex(prev_hash + canonical_json([position, event_seq, kind, ref_id, body]))


class AuditChain:
    def __init__(self, position: int = 0, head_hash: str = GENESIS_HASH) -> None:
        self.position = position
        self.head_hash = head_hash

    @classmethod
    def load(cls, conn: Connection) -> "AuditChain":
        row = conn.execute(
            select(db.audit_log.c.position, db.audit_log.c.hash)
            .order_by(db.audit_log.c.position.desc())
            .limit(1)
        ).first()
        return cls(row[0], row[1]) if row else cls()

    def next_row(self, event_seq: int, kind: str, ref_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Build the next row and advance the head. Caller persists it."""
        position = self.position + 1
        h = entry_hash(self.head_hash, position, event_seq, kind, ref_id, body)
        row = {
            "position": position,
            "event_seq": event_seq,
            "kind": kind,
            "ref_id": ref_id,
            "body": body,
            "prev_hash": self.head_hash,
            "hash": h,
        }
        self.position = position
        self.head_hash = h
        return row


@dataclass
class VerifyResult:
    ok: bool
    entries: int
    head_hash: str
    first_bad_position: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def verify(conn: Connection, chunk: int = 5000) -> VerifyResult:
    prev = GENESIS_HASH
    expected_position = 1
    count = 0
    last = 0
    while True:
        rows = conn.execute(
            select(db.audit_log).where(db.audit_log.c.position > last).order_by(db.audit_log.c.position).limit(chunk)
        ).mappings().all()
        if not rows:
            break
        for r in rows:
            if r["position"] != expected_position:
                return VerifyResult(False, count, prev, expected_position, "missing entry (gap in positions)")
            if r["prev_hash"] != prev:
                return VerifyResult(False, count, prev, r["position"], "prev_hash does not match previous entry")
            recomputed = entry_hash(prev, r["position"], r["event_seq"], r["kind"], r["ref_id"], r["body"])
            if recomputed != r["hash"]:
                return VerifyResult(False, count, prev, r["position"], "entry content does not match its hash")
            prev = r["hash"]
            expected_position += 1
            count += 1
        last = rows[-1]["position"]
    return VerifyResult(True, count, prev)
