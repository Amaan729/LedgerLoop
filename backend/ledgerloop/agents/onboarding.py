"""Onboarding agent: decides whether a new customer gets credit, and how much."""

from __future__ import annotations

from ..events import CUSTOMER_APPLIED, Event
from ..policy import Policy
from ..reference import HIGH_RISK_COUNTRIES
from ..state import Effect, State
from .base import Decision, Tools, needs_review

NAME = "onboarding"


class OnboardingAgent:
    name = NAME
    handles = frozenset({CUSTOMER_APPLIED})

    def decide(self, event: Event, state: State, policy: Policy, tools: Tools) -> Decision:
        p = event.payload
        cid = p["customer_id"]
        base = {
            "customer_id": cid,
            "legal_name": p["legal_name"],
            "tax_id": (p.get("tax_id") or "").strip() or None,
            "country": p["country"],
        }

        if base["tax_id"] is None:
            return needs_review(
                NAME, cid, "missing_tax_id", f"{p['legal_name']} applied without a tax id",
                ["missing_tax_id"], effects=[Effect("customer.upsert", {**base, "status": "review"})],
            )
        if p["country"] in HIGH_RISK_COUNTRIES:
            return needs_review(
                NAME, cid, "high_risk_country", f"{p['legal_name']} is registered in {p['country']}",
                ["high_risk_country"], effects=[Effect("customer.upsert", {**base, "status": "review"})],
            )

        requested = p["requested_limit_cents"]
        revenue = p.get("annual_revenue_cents")
        policy_limit = revenue * policy.limit_pct_of_revenue // 100 if revenue else policy.default_limit_cents
        granted = min(requested, policy_limit, policy.max_auto_limit_cents)

        if requested > policy_limit * policy.review_if_requested_over_policy_x:
            return needs_review(
                NAME, cid, "limit_far_above_policy",
                f"requested ${requested / 100:,.0f}, policy allows ${policy_limit / 100:,.0f}",
                ["limit_far_above_policy"],
                detail={"requested_cents": requested, "policy_limit_cents": policy_limit},
                effects=[Effect("customer.upsert", {**base, "status": "review"})],
            )

        reasons = ["checks_passed"]
        if granted < requested:
            reasons.append("limit_capped_by_policy")
        return Decision(
            agent=NAME,
            action="approved",
            subject_id=cid,
            auto_resolved=True,
            reasons=reasons,
            detail={"requested_cents": requested, "granted_cents": granted},
            effects=[Effect("customer.upsert", {**base, "status": "approved", "credit_limit_cents": granted})],
        )
