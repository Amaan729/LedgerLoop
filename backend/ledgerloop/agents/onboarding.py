"""Onboarding agent: decides whether a new customer gets credit, and how much."""

from __future__ import annotations

from ..events import CUSTOMER_APPLIED, Event
from ..policy import Policy
from ..reference import HIGH_RISK_COUNTRIES, SANCTIONED_ENTITIES
from ..state import Customer, Effect, State
from ..text import normalize_name, similarity
from .base import Decision, Tools, needs_review

NAME = "onboarding"

_SANCTIONED_NORM = tuple((name, normalize_name(name)) for name in SANCTIONED_ENTITIES)


def screen_sanctions(norm_name: str) -> tuple[str | None, float]:
    best, best_score = None, 0.0
    for original, norm in _SANCTIONED_NORM:
        s = similarity(norm_name, norm)
        if s > best_score:
            best, best_score = original, s
    return best, best_score


def find_similar_customer(
    state: State, cid: str, norm_name: str, country: str, threshold: float
) -> tuple[Customer, float] | None:
    best: tuple[Customer, float] | None = None
    for other in state.customers_in_block(norm_name):
        if other.customer_id == cid or other.country != country or other.status == "rejected":
            continue
        s = similarity(norm_name, other.norm_name)
        if s >= threshold and (best is None or s > best[1]):
            best = (other, s)
    return best


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

        norm = normalize_name(p["legal_name"])
        hit, score = screen_sanctions(norm)
        if score >= policy.sanctions_block_threshold:
            return Decision(
                agent=NAME,
                action="rejected",
                subject_id=cid,
                auto_resolved=True,
                reasons=["sanctions_match"],
                detail={"matched": hit, "score": round(score, 3)},
                effects=[Effect("customer.upsert", {**base, "status": "rejected"})],
            )
        if score >= policy.sanctions_review_threshold:
            return needs_review(
                NAME, cid, "sanctions_near_match", f"{p['legal_name']} resembles screened entity {hit}",
                ["sanctions_near_match"], detail={"matched": hit, "score": round(score, 3)},
                effects=[Effect("customer.upsert", {**base, "status": "review"})],
            )

        dup = state.customer_by_tax_id(base["tax_id"])
        if dup is not None and dup.customer_id != cid:
            return needs_review(
                NAME, cid, "duplicate_tax_id", f"tax id already belongs to {dup.legal_name} ({dup.customer_id})",
                ["duplicate_tax_id"], detail={"existing_customer_id": dup.customer_id},
                effects=[Effect("customer.upsert", {**base, "status": "review"})],
            )
        similar = find_similar_customer(state, cid, norm, p["country"], policy.duplicate_name_threshold)
        if similar is not None:
            other, sim = similar
            return needs_review(
                NAME, cid, "possible_duplicate", f"name is {sim:.0%} similar to {other.legal_name} ({other.customer_id})",
                ["possible_duplicate"], detail={"existing_customer_id": other.customer_id, "similarity": round(sim, 3)},
                effects=[Effect("customer.upsert", {**base, "status": "review"})],
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
