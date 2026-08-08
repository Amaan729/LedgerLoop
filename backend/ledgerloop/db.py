"""Storage schema (SQLAlchemy Core).

Only `events` is the source of truth. Everything else is either derived
(decisions, exceptions) or evidence about how it was derived (audit_log,
tool_calls). Core instead of the ORM because the hot path is bulk inserts.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()

SeqType = BigInteger().with_variant(Integer, "sqlite")

events = Table(
    "events",
    metadata,
    Column("seq", SeqType, primary_key=True, autoincrement=True),
    Column("event_id", String(128), nullable=False, unique=True),
    Column("type", String(64), nullable=False, index=True),
    Column("occurred_at", String(40), nullable=False),
    Column("source", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("recorded_at", DateTime(timezone=True), server_default=func.now()),
)

decisions = Table(
    "decisions",
    metadata,
    Column("decision_id", String(64), primary_key=True),
    Column("event_seq", SeqType, nullable=False, index=True),
    Column("event_id", String(128), nullable=False),
    Column("agent", String(32), nullable=False, index=True),
    Column("action", String(48), nullable=False),
    Column("subject_id", String(128), nullable=False, index=True),
    Column("auto_resolved", Boolean, nullable=False),
    Column("reasons", JSON, nullable=False),
    Column("detail", JSON, nullable=False),
    Column("policy_version", String(32), nullable=False),
    Column("decision_hash", String(64), nullable=False),
)

exceptions = Table(
    "exceptions",
    metadata,
    Column("exception_id", String(64), primary_key=True),
    Column("decision_id", String(64), nullable=False),
    Column("agent", String(32), nullable=False),
    Column("kind", String(48), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("summary", Text, nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("opened_seq", SeqType, nullable=False),
    Column("resolved_seq", SeqType),
    Column("resolution", JSON),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("position", SeqType, primary_key=True, autoincrement=False),
    Column("event_seq", SeqType, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("ref_id", String(64), nullable=False),
    Column("body", JSON, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
)

tool_calls = Table(
    "tool_calls",
    metadata,
    Column("call_key", String(64), primary_key=True),
    Column("event_id", String(128), nullable=False, index=True),
    Column("tool", String(64), nullable=False),
    Column("input", JSON, nullable=False),
    Column("output", JSON, nullable=False),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite") and ":memory:" in url:
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)


def reset_db(engine: Engine) -> None:
    metadata.drop_all(engine)
    metadata.create_all(engine)
