"""What every agent looks like.

An agent is a function of (event, current state, policy, tools) -> Decision.
It must not read the wall clock, randomness, or the network directly. Anything
nondeterministic goes through `tools`, which records outputs so replay can serve
the same answers later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..events import Event, canonical_json, derive_id, sha256_hex
from ..policy import Policy
from ..state import Effect, State


class Tools(Protocol):
    def call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class Decision:
    agent: str
    action: str
    subject_id: str
    auto_resolved: bool
    reasons: list[str]
    detail: dict[str, Any] = field(default_factory=dict)
    effects: list[Effect] = field(default_factory=list)
    exception_kind: str | None = None
    exception_summary: str | None = None
    # filled in by the engine
    event_id: str = ""
    event_seq: int = 0
    policy_version: str = ""

    @property
    def decision_id(self) -> str:
        return derive_id("dec", self.event_id, self.agent)

    @property
    def exception_id(self) -> str | None:
        return derive_id("exc", self.event_id, self.agent) if self.exception_kind else None

    def body(self) -> dict[str, Any]:
        """Everything that defines the decision. The hash and the audit entry cover this."""
        return {
            "decision_id": self.decision_id,
            "event_id": self.event_id,
            "agent": self.agent,
            "action": self.action,
            "subject_id": self.subject_id,
            "auto_resolved": self.auto_resolved,
            "reasons": self.reasons,
            "detail": self.detail,
            "effects": [e.to_dict() for e in self.effects],
            "exception": (
                {"id": self.exception_id, "kind": self.exception_kind, "summary": self.exception_summary}
                if self.exception_kind
                else None
            ),
            "policy_version": self.policy_version,
        }

    def decision_hash(self) -> str:
        return sha256_hex(canonical_json(self.body()))


class Agent(Protocol):
    name: str
    handles: frozenset[str]

    def decide(self, event: Event, state: State, policy: Policy, tools: Tools) -> Decision: ...


def needs_review(
    agent: str,
    subject_id: str,
    kind: str,
    summary: str,
    reasons: list[str],
    detail: dict[str, Any] | None = None,
    effects: list[Effect] | None = None,
    action: str = "needs_review",
) -> Decision:
    return Decision(
        agent=agent,
        action=action,
        subject_id=subject_id,
        auto_resolved=False,
        reasons=reasons,
        detail=detail or {},
        effects=effects or [],
        exception_kind=kind,
        exception_summary=summary,
    )
