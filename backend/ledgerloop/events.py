"""Event model.

Everything LedgerLoop knows comes from events. Inbound events describe facts from
the outside world (a customer applied, a payment landed). A human resolving an
exception is also an event. Agent decisions are *derived* from events and are
never the source of truth, which is what makes replay possible.

Conventions:
  * Money is integer cents. No floats anywhere near a ledger.
  * `occurred_at` is set by the source and is the only clock agents may read.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

CUSTOMER_APPLIED = "customer.applied"
ORDER_PLACED = "order.placed"
INVOICE_ISSUED = "invoice.issued"
PAYMENT_RECEIVED = "payment.received"
EXCEPTION_RESOLVED = "exception.resolved"

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    CUSTOMER_APPLIED: ("customer_id", "legal_name", "country", "requested_limit_cents"),
    ORDER_PLACED: ("order_id", "customer_id", "amount_cents"),
    INVOICE_ISSUED: ("invoice_id", "order_id", "customer_id", "amount_cents", "due_date"),
    PAYMENT_RECEIVED: ("payment_id", "payer_name", "amount_cents"),
    EXCEPTION_RESOLVED: ("exception_id", "resolution", "resolved_by"),
}

INBOUND_TYPES = frozenset(REQUIRED_FIELDS)


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no whitespace. Hashes are computed over this."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_id(prefix: str, *parts: Any) -> str:
    """Deterministic id from its inputs, so replays produce identical ids."""
    return f"{prefix}_{sha256_hex(canonical_json(parts))[:16]}"


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


class InvalidEvent(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    occurred_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "external"
    seq: int | None = None

    def validate(self) -> None:
        if self.type not in INBOUND_TYPES:
            raise InvalidEvent(f"unknown event type {self.type!r}")
        missing = [k for k in REQUIRED_FIELDS[self.type] if self.payload.get(k) in (None, "")]
        if missing:
            raise InvalidEvent(f"{self.type} missing {', '.join(missing)}")
        for key, value in self.payload.items():
            if key.endswith("_cents") and not isinstance(value, int):
                raise InvalidEvent(f"{key} must be integer cents, got {type(value).__name__}")
        parse_ts(self.occurred_at)

    def fingerprint(self) -> str:
        """Hash of the content (not the seq). Same event_id + different fingerprint = conflict."""
        return sha256_hex(canonical_json([self.type, self.occurred_at, self.payload]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
            "source": self.source,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            event_id=d["event_id"],
            type=d["type"],
            occurred_at=d["occurred_at"],
            payload=dict(d.get("payload") or {}),
            source=d.get("source", "external"),
            seq=d.get("seq"),
        )

    def with_seq(self, seq: int) -> "Event":
        return Event(self.event_id, self.type, self.occurred_at, self.payload, self.source, seq)
