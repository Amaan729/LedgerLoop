"""Push a simulated stream into a running LedgerLoop for the dashboard demo.

  python scripts/seed.py --to redis             # XADD to the ingest stream (API started with LEDGERLOOP_CONSUME=1)
  python scripts/seed.py --to http --rate 500   # POST batches to the API, ~500 events/s
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledgerloop.config import load_settings  # noqa: E402
from ledgerloop.simulator import SimConfig, simulate  # noqa: E402


def main() -> None:
    s = load_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", choices=["redis", "http"], default="redis")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--redis", default=s.redis_url)
    ap.add_argument("--stream", default=s.stream_key)
    ap.add_argument("--customers", type=int, default=300)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rate", type=int, default=0, help="events/s, 0 = as fast as possible")
    ap.add_argument("--chunk", type=int, default=250)
    a = ap.parse_args()

    events, _ = simulate(SimConfig(seed=a.seed, customers=a.customers, days=a.days))
    print(f"sending {len(events):,} events to {a.to}")

    if a.to == "redis":
        import redis

        from ledgerloop import bus

        r = redis.Redis.from_url(a.redis)
        send = lambda chunk: bus.publish(r, a.stream, chunk)  # noqa: E731
    else:
        import httpx

        client = httpx.Client(base_url=a.api, timeout=60)

        def send(chunk):
            client.post("/events", json=[e.to_dict() for e in chunk]).raise_for_status()

    t0 = time.perf_counter()
    for i in range(0, len(events), a.chunk):
        send(events[i : i + a.chunk])
        if a.rate:
            target = (i + a.chunk) / a.rate
            ahead = target - (time.perf_counter() - t0)
            if ahead > 0:
                time.sleep(ahead)
    print(f"done in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
