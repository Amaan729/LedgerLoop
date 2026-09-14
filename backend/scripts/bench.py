"""End-to-end benchmark.

  1. Generate a synthetic stream (see ledgerloop/simulator.py).
  2. Publish it all to a Redis stream up front (not timed).
  3. Start the consumer and time how long it takes to drain the stream into
     Postgres: parse -> idempotent append -> agents -> decisions/audit -> commit -> ack.
  4. Verify the audit chain and replay the whole log, checking every decision hash.
  5. Score the agents against the simulator's ground truth.

Numbers depend on the machine and on the scenario mix. The JSON written to
bench_results/ records both so a run can be reproduced.

  python scripts/bench.py --customers 1000 --days 120
  python scripts/bench.py --mode inproc   # skip Redis, call the engine directly
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledgerloop import audit, db, replay  # noqa: E402
from ledgerloop.api import queries  # noqa: E402
from ledgerloop.engine import Engine  # noqa: E402
from ledgerloop.evaluate import evaluate  # noqa: E402
from ledgerloop.simulator import SimConfig, simulate  # noqa: E402


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))
    return values[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("LEDGERLOOP_DATABASE_URL", "postgresql+psycopg://postgres@localhost/ledgerloop"))
    ap.add_argument("--redis", default=os.getenv("LEDGERLOOP_REDIS_URL", "redis://localhost:6379/0"))
    ap.add_argument("--mode", choices=["pipeline", "inproc"], default="pipeline")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--customers", type=int, default=1000)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "bench_results"))
    a = ap.parse_args()

    t = time.perf_counter()
    events, truth = simulate(SimConfig(seed=a.seed, customers=a.customers, days=a.days))
    gen_s = time.perf_counter() - t
    print(f"generated {len(events):,} events in {gen_s:.1f}s")

    dbe = db.make_engine(a.db)
    db.reset_db(dbe)
    engine = Engine(dbe)
    engine.boot()

    batch_times: list[float] = []
    if a.mode == "pipeline":
        import redis

        from ledgerloop import bus

        r = redis.Redis.from_url(a.redis)
        stream = "ledgerloop:bench"
        r.delete(stream, f"{stream}:dead")
        bus.publish(r, stream, events)
        consumer = bus.StreamConsumer(r, engine, stream, "bench", batch_size=a.batch, block_ms=100)
        consumer.ensure_group()
        consumer._recovering = False
        t = time.perf_counter()
        consumed = 0
        while consumed < len(events):
            bt = time.perf_counter()
            res = consumer.poll_once()
            if res is None:
                break
            batch_times.append(time.perf_counter() - bt)
            consumed += res.received
        elapsed = time.perf_counter() - t
        lag = consumer.lag()
        dead = r.xlen(f"{stream}:dead")
    else:
        t = time.perf_counter()
        for i in range(0, len(events), a.batch):
            bt = time.perf_counter()
            engine.process(events[i : i + a.batch])
            batch_times.append(time.perf_counter() - bt)
        elapsed = time.perf_counter() - t
        lag, dead = None, 0

    throughput = len(events) / elapsed
    print(f"processed {len(events):,} events in {elapsed:.2f}s -> {throughput:,.0f} events/s ({a.mode})")

    with dbe.connect() as conn:
        m = queries.metrics(conn)
        t = time.perf_counter()
        chain = audit.verify(conn)
        audit_s = time.perf_counter() - t
        rv = replay.verify(conn)
        report = evaluate(conn, engine.state, truth)

    result = {
        "when": datetime.now(timezone.utc).isoformat(),
        "machine": {"python": platform.python_version(), "platform": platform.platform(), "cpus": os.cpu_count()},
        "mode": a.mode,
        "batch_size": a.batch,
        "sim": truth["config"],
        "events": len(events),
        "events_by_type": m["events_by_type"],
        "throughput": {
            "elapsed_s": round(elapsed, 3),
            "events_per_s": round(throughput),
            "batch_p50_ms": round(pct(batch_times, 50) * 1000, 1),
            "batch_p99_ms": round(pct(batch_times, 99) * 1000, 1),
        },
        "stream": {"lag": lag, "dead_letters": dead},
        "auto_resolution": {
            "overall": m["auto_resolution_rate"],
            "by_agent": {k: v["auto_resolution_rate"] for k, v in m["agents"].items()},
            "decisions": m["decisions_total"],
            "auto_resolved": m["auto_resolved_total"],
        },
        "exceptions": {"open_by_kind": m["exceptions_open_by_kind"], "resolved_by_clerk": m["exceptions_resolved"]},
        "correctness": {
            "overall_auto_precision": report["overall_auto_precision"],
            "wrong_auto_decisions": report["wrong_auto_decisions"],
            "cash": report["cash"],
            "onboarding": report["onboarding"],
            "risk": report["risk"],
            "cash_by_scenario": report["cash_by_scenario"],
        },
        "audit_chain": {**chain.to_dict(), "verify_s": round(audit_s, 3)},
        "replay": {k: rv[k] for k in ("ok", "events_replayed", "decisions_checked", "mismatch_count", "elapsed_s", "events_per_s")},
        "replay_matches_live_state": rv["state_hash"] == engine.state.state_hash(),
    }

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"bench-{a.mode}-seed{a.seed}-{a.customers}c-{a.days}d.json"
    path.write_text(json.dumps(result, indent=2))

    print(f"auto-resolution {m['auto_resolution_rate']:.1%}  (by agent: "
          + ", ".join(f"{k} {v:.1%}" for k, v in result["auto_resolution"]["by_agent"].items()) + ")")
    print(f"wrong auto-decisions: {report['wrong_auto_decisions']}  precision {report['overall_auto_precision']:.4%}")
    print(f"audit chain ok={chain.ok} ({chain.entries:,} entries)  replay ok={rv['ok']} "
          f"({rv['decisions_checked']:,} decisions re-derived in {rv['elapsed_s']}s)  state match={result['replay_matches_live_state']}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
