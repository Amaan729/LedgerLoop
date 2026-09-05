"""Read-side queries for the dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.engine import Connection

from .. import db


def metrics(conn: Connection) -> dict[str, Any]:
    events_by_type = dict(conn.execute(select(db.events.c.type, func.count()).group_by(db.events.c.type)).all())

    rows = conn.execute(
        select(
            db.decisions.c.agent,
            func.count(),
            func.sum(case((db.decisions.c.auto_resolved, 1), else_=0)),
        )
        .where(db.decisions.c.agent != "human")
        .group_by(db.decisions.c.agent)
    ).all()
    agents = {a: {"decisions": int(n), "auto_resolved": int(auto or 0)} for a, n, auto in rows}
    for v in agents.values():
        v["auto_resolution_rate"] = round(v["auto_resolved"] / v["decisions"], 4) if v["decisions"] else None
    total = sum(v["decisions"] for v in agents.values())
    auto = sum(v["auto_resolved"] for v in agents.values())

    open_by_kind = dict(
        conn.execute(
            select(db.exceptions.c.kind, func.count())
            .where(db.exceptions.c.status == "open")
            .group_by(db.exceptions.c.kind)
        ).all()
    )
    resolved = conn.execute(
        select(func.count()).select_from(db.exceptions).where(db.exceptions.c.status == "resolved")
    ).scalar_one()

    reasons = conn.execute(
        select(db.decisions.c.agent, db.decisions.c.action, func.count())
        .group_by(db.decisions.c.agent, db.decisions.c.action)
    ).all()

    return {
        "events_by_type": events_by_type,
        "events_total": sum(events_by_type.values()),
        "agents": agents,
        "decisions_total": total,
        "auto_resolved_total": auto,
        "auto_resolution_rate": round(auto / total, 4) if total else None,
        "exceptions_open_by_kind": open_by_kind,
        "exceptions_open": sum(open_by_kind.values()),
        "exceptions_resolved": resolved,
        "actions": [{"agent": a, "action": act, "count": n} for a, act, n in reasons],
    }


def list_exceptions(conn: Connection, status: str | None, agent: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    q = select(db.exceptions).order_by(db.exceptions.c.opened_seq.desc()).limit(limit).offset(offset)
    if status:
        q = q.where(db.exceptions.c.status == status)
    if agent:
        q = q.where(db.exceptions.c.agent == agent)
    return [dict(r) for r in conn.execute(q).mappings()]


def get_exception(conn: Connection, exception_id: str) -> dict[str, Any] | None:
    row = conn.execute(select(db.exceptions).where(db.exceptions.c.exception_id == exception_id)).mappings().first()
    if row is None:
        return None
    out = dict(row)
    decision = conn.execute(select(db.decisions).where(db.decisions.c.decision_id == row["decision_id"])).mappings().first()
    out["decision"] = dict(decision) if decision else None
    return out


def audit_page(conn: Connection, limit: int, before: int | None) -> list[dict[str, Any]]:
    q = select(db.audit_log).order_by(db.audit_log.c.position.desc()).limit(limit)
    if before:
        q = q.where(db.audit_log.c.position < before)
    return [dict(r) for r in conn.execute(q).mappings()]
