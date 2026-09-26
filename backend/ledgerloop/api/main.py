"""HTTP API for the dashboard and for posting events directly.

Run: uvicorn ledgerloop.api.main:app --reload
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine as DbEngine

from .. import audit, db, eventlog, replay
from ..config import load_settings
from ..engine import BatchResult, Engine
from ..events import EXCEPTION_RESOLVED, Event, canonical_json, derive_id
from ..policy import POLICIES
from ..processor import registry_from_env
from . import queries

log = logging.getLogger("ledgerloop")


class ResolveRequest(BaseModel):
    resolution: dict[str, Any] = Field(..., examples=[{"action": "release"}])
    resolved_by: str
    note: str | None = None
    idempotency_key: str | None = None


class WhatIfRequest(BaseModel):
    policy: str


def create_app(db_engine: DbEngine | None = None, start_consumer: bool | None = None) -> FastAPI:
    settings = load_settings()
    if start_consumer is None:
        start_consumer = os.getenv("LEDGERLOOP_CONSUME", "0") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        dbe = db_engine or db.make_engine(settings.database_url)
        db.init_db(dbe)
        engine = Engine(dbe, registry=registry_from_env())
        boot = engine.boot()
        log.info("booted from log: %s", boot)
        app.state.db = dbe
        app.state.engine = engine
        app.state.consumer = None
        if start_consumer:
            import redis

            from ..bus import StreamConsumer

            r = redis.Redis.from_url(settings.redis_url)
            consumer = StreamConsumer(r, engine, settings.stream_key, settings.consumer_group, batch_size=settings.batch_size)
            consumer.start()
            app.state.consumer = consumer
        yield
        if app.state.consumer:
            app.state.consumer.stop()

    app = FastAPI(title="LedgerLoop", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("LEDGERLOOP_CORS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def eng() -> Engine:
        return app.state.engine

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "last_seq": eng().last_seq}

    @app.post("/events")
    def post_events(events: list[dict[str, Any]] = Body(...)) -> dict[str, Any]:
        try:
            parsed = [Event.from_dict(e) for e in events]
        except (KeyError, TypeError) as e:
            raise HTTPException(422, f"malformed event: {e}") from None
        return eng().process(parsed).to_dict()

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        with app.state.db.connect() as conn:
            m = queries.metrics(conn)
        t = eng().totals
        m["engine"] = {
            "batches": t["batches"],
            "events_since_boot": t["events"],
            "busy_s": round(t["busy_s"], 3),
            "events_per_busy_s": round(t["events"] / t["busy_s"]) if t["busy_s"] else None,
            "policy": eng().policy.version,
        }
        m["stream"] = app.state.consumer.lag() if app.state.consumer else None
        return m

    @app.get("/exceptions")
    def list_exceptions(
        status: str | None = "open",
        agent: str | None = None,
        limit: int = Query(50, le=500),
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with app.state.db.connect() as conn:
            return queries.list_exceptions(conn, status, agent, limit, offset)

    @app.get("/exceptions/{exception_id}")
    def get_exception(exception_id: str) -> dict[str, Any]:
        with app.state.db.connect() as conn:
            row = queries.get_exception(conn, exception_id)
        if row is None:
            raise HTTPException(404, "no such exception")
        row["subject"] = _subject_snapshot(eng(), row["agent"], row["subject_id"])
        return row

    @app.post("/exceptions/{exception_id}/resolve")
    def resolve(exception_id: str, req: ResolveRequest) -> dict[str, Any]:
        key = req.idempotency_key or canonical_json([exception_id, req.resolution, req.resolved_by])
        event_id = derive_id("res", key)
        with app.state.db.connect() as conn:
            if eventlog.exists(conn, event_id):
                # A retry of a resolution we already have. The timestamp would differ, so
                # appending it again would look like a conflict rather than a duplicate.
                return {"batch": BatchResult(received=1, duplicate=1).to_dict(), "exception": queries.get_exception(conn, exception_id)}
        ev = Event(
            event_id=event_id,
            type=EXCEPTION_RESOLVED,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            payload={"exception_id": exception_id, "resolution": req.resolution, "resolved_by": req.resolved_by, "note": req.note},
            source="dashboard",
        )
        result = eng().process([ev])
        with app.state.db.connect() as conn:
            row = queries.get_exception(conn, exception_id)
        return {"batch": result.to_dict(), "exception": row}

    @app.get("/audit")
    def audit_log(limit: int = Query(50, le=500), before: int | None = None) -> list[dict[str, Any]]:
        with app.state.db.connect() as conn:
            return queries.audit_page(conn, limit, before)

    @app.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        with app.state.db.connect() as conn:
            return audit.verify(conn).to_dict()

    @app.post("/replay/verify")
    def replay_verify() -> dict[str, Any]:
        with app.state.db.connect() as conn:
            out = replay.verify(conn)
        out["live_state_hash"] = eng().state.state_hash()
        out["matches_live_state"] = out["state_hash"] == out["live_state_hash"]
        return out

    @app.post("/replay/what-if")
    def replay_what_if(req: WhatIfRequest) -> dict[str, Any]:
        if req.policy not in POLICIES:
            raise HTTPException(404, f"unknown policy; known: {list(POLICIES)}")
        with app.state.db.connect() as conn:
            return replay.what_if(conn, req.policy)

    @app.get("/policies")
    def policies() -> dict[str, Any]:
        return {"active": eng().policy.version, "available": {k: v.to_dict() for k, v in POLICIES.items()}}

    @app.get("/customers/{customer_id}")
    def customer(customer_id: str) -> dict[str, Any]:
        snap = _subject_snapshot(eng(), "onboarding", customer_id)
        if snap is None:
            raise HTTPException(404, "no such customer")
        return snap

    return app


def _subject_snapshot(engine: Engine, agent: str, subject_id: str) -> dict[str, Any] | None:
    s = engine.state
    if agent == "onboarding":
        c = s.customers.get(subject_id)
        if not c:
            return None
        return {
            **asdict(c),
            "open_ar_cents": s.open_ar_cents(c.customer_id),
            "open_invoices": [asdict(i) for i in s.open_invoices(c.customer_id)][:50],
        }
    if agent == "risk":
        o = s.orders.get(subject_id)
        if not o:
            return None
        c = s.customers.get(o.customer_id)
        return {
            **asdict(o),
            "customer": asdict(c) if c else None,
            "open_ar_cents": s.open_ar_cents(o.customer_id),
            "uninvoiced_cents": s.uninvoiced_exposure_cents(o.customer_id),
        }
    if agent == "cash":
        p = s.payments.get(subject_id)
        if not p:
            return None
        cid = p.customer_id
        return {
            **asdict(p),
            "candidate_invoices": [asdict(i) for i in s.open_invoices(cid)][:50] if cid else [],
        }
    return None


app = create_app()
