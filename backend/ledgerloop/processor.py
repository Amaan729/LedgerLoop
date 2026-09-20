"""Apply one event to state. The same code runs live and in replay.

Keeping a single code path is the whole trick: if live processing and replay
went through different functions they would eventually disagree.
"""

from __future__ import annotations

from typing import Callable

from . import resolutions
from .agents.base import Agent, Decision
from .agents.cash import CashAgent
from .agents.onboarding import OnboardingAgent
from .agents.risk import RiskAgent
from .events import EXCEPTION_RESOLVED, INVOICE_ISSUED, Event
from .policy import Policy
from .state import Effect, State
from .tools.recorder import RecordingTools, ToolFn
from .tools.remittance import regex_remittance_parser

AGENTS: tuple[Agent, ...] = (OnboardingAgent(), RiskAgent(), CashAgent())


def default_registry() -> dict[str, ToolFn]:
    return {"remittance_parser": regex_remittance_parser}


def registry_from_env() -> dict[str, ToolFn]:
    """Regex parser unless LEDGERLOOP_LLM_PARSER=1 and an Anthropic key is set."""
    import os

    if os.getenv("LEDGERLOOP_LLM_PARSER") == "1" and os.getenv("ANTHROPIC_API_KEY"):
        from .tools.llm_remittance import LLMRemittanceParser

        return {"remittance_parser": LLMRemittanceParser(model=os.getenv("LEDGERLOOP_LLM_MODEL", "claude-haiku-4-5-20251001"))}
    return default_registry()


class Processor:
    def __init__(self, state: State, tools: RecordingTools, policy_for: Callable[[Event], Policy]) -> None:
        self.state = state
        self.tools = tools
        self.policy_for = policy_for
        self.by_type = {t: a for a in AGENTS for t in a.handles}
        self.ignored_invoices = 0

    def handle(self, event: Event) -> Decision | None:
        if event.type == INVOICE_ISSUED:
            if not self.state.add_invoice(event.payload):
                self.ignored_invoices += 1
            return None

        if event.type == EXCEPTION_RESOLVED:
            decision = resolutions.resolve(event, self.state)
            decision.policy_version = "-"
        else:
            agent = self.by_type[event.type]
            policy = self.policy_for(event)
            self.tools.begin(event.event_id)
            decision = agent.decide(event, self.state, policy, self.tools)
            decision.policy_version = policy.version

        decision.event_id = event.event_id
        decision.event_seq = event.seq or 0
        for effect in decision.effects:
            self.state.apply(effect)
        if decision.exception_kind:
            self.state.apply(
                Effect(
                    "exception.open",
                    {
                        "exception_id": decision.exception_id,
                        "agent": decision.agent,
                        "kind": decision.exception_kind,
                        "subject_id": decision.subject_id,
                        "summary": decision.exception_summary,
                    },
                )
            )
        return decision
