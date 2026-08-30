"""Deterministic replay.

`fold` rebuilds state from nothing but the event log, the recorded tool outputs,
and the policy version each decision ran under. Three things use it:

  * boot: the engine's in-memory state is just `fold(...)`.
  * verify: re-derive every decision and compare hashes with what was stored.
    Any mismatch means something nondeterministic leaked into an agent (a clock
    read, dict ordering, an unrecorded call) or agent code changed behavior.
  * what_if: re-run history under a different policy and diff the outcomes
    before shipping the policy change.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import db, eventlog
from .agents.base import Decision
from .events import Event, canonical_json, sha256_hex
from .policy import DEFAULT_POLICY, Policy, get_policy
from .processor import Processor
from .state import State
from .tools.recorder import RecordingTools


@dataclass
class FoldResult:
    state: State
    events: int
    last_seq: int
    decisions: list[Decision] = field(default_factory=list)
    elapsed_s: float = 0.0


def load_recordings(conn: Connection) -> dict[str, Any]:
    return {k: out for k, out in conn.execute(select(db.tool_calls.c.call_key, db.tool_calls.c.output))}


def load_policy_versions(conn: Connection) -> dict[str, str]:
    return {eid: v for eid, v in conn.execute(select(db.decisions.c.event_id, db.decisions.c.policy_version))}


def fold(conn: Connection, policy_override: Policy | None = None, collect: bool = False) -> FoldResult:
    t0 = perf_counter()
    tools = RecordingTools({}, mode="replay", recorded=load_recordings(conn))
    versions = {} if policy_override else load_policy_versions(conn)

    def policy_for(event: Event) -> Policy:
        if policy_override is not None:
            return policy_override
        return get_policy(versions.get(event.event_id, DEFAULT_POLICY.version))

    state = State()
    proc = Processor(state, tools, policy_for)
    n = 0
    last_seq = 0
    decisions: list[Decision] = []
    for ev in eventlog.read_events(conn):
        d = proc.handle(ev)
        if collect and d is not None:
            decisions.append(d)
        n += 1
        last_seq = ev.seq or last_seq
    return FoldResult(state, n, last_seq, decisions, perf_counter() - t0)


def verify(conn: Connection) -> dict[str, Any]:
    """Recompute every decision from the log and compare with what was stored."""
    stored = {
        did: h for did, h in conn.execute(select(db.decisions.c.decision_id, db.decisions.c.decision_hash))
    }
    result = fold(conn, collect=True)
    mismatches = []
    seen = set()
    for d in result.decisions:
        seen.add(d.decision_id)
        if stored.get(d.decision_id) != d.decision_hash():
            mismatches.append({"decision_id": d.decision_id, "event_id": d.event_id, "agent": d.agent})
    missing = sorted(set(stored) - seen)
    return {
        "ok": not mismatches and not missing,
        "events_replayed": result.events,
        "decisions_checked": len(result.decisions),
        "mismatches": mismatches[:50],
        "mismatch_count": len(mismatches),
        "missing_count": len(missing),
        "state_hash": result.state.state_hash(),
        "elapsed_s": round(result.elapsed_s, 3),
        "events_per_s": round(result.events / result.elapsed_s) if result.elapsed_s else None,
    }


def _outcome(d: Decision) -> str:
    return f"{d.action}:{d.exception_kind or '-'}"


def what_if(conn: Connection, policy_version: str, sample: int = 20) -> dict[str, Any]:
    """Replay history under another policy and report which decisions would change."""
    candidate = get_policy(policy_version)
    baseline = fold(conn, collect=True)
    alt = fold(conn, policy_override=candidate, collect=True)
    base_by_id = {d.decision_id: d for d in baseline.decisions if d.agent != "human"}
    changes: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    changed = 0
    for d in alt.decisions:
        if d.agent == "human":
            continue
        b = base_by_id.get(d.decision_id)
        if b is None or _outcome(b) == _outcome(d):
            continue
        changed += 1
        key = f"{d.agent}: {_outcome(b)} -> {_outcome(d)}"
        changes[key] += 1
        if len(examples) < sample:
            examples.append({"decision_id": d.decision_id, "subject_id": d.subject_id, "change": key})

    def auto_rate(decisions: list[Decision]) -> float:
        agent_ds = [x for x in decisions if x.agent != "human"]
        return round(sum(x.auto_resolved for x in agent_ds) / len(agent_ds), 4) if agent_ds else 0.0

    return {
        "policy": policy_version,
        "decisions_compared": len(base_by_id),
        "changed": changed,
        "changes_by_type": dict(changes.most_common()),
        "examples": examples,
        "auto_resolution_rate": {"current": auto_rate(baseline.decisions), "candidate": auto_rate(alt.decisions)},
        "state_hash": {"current": baseline.state.state_hash(), "candidate": alt.state.state_hash()},
    }


def decision_hash_digest(decisions: list[Decision]) -> str:
    """One hash over all decisions, handy for comparing two full runs."""
    return sha256_hex(canonical_json([d.decision_hash() for d in decisions]))
