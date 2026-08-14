"""Versioned policy knobs.

Every decision records the policy version it ran under. To see what a policy
change would do before shipping it, replay the log under the new version and
diff the decisions (see replay.what_if).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Policy:
    version: str = "v1"

    # onboarding
    limit_pct_of_revenue: int = 10
    default_limit_cents: int = 25_000_00
    max_auto_limit_cents: int = 250_000_00
    review_if_requested_over_policy_x: int = 3
    duplicate_name_threshold: float = 0.93
    sanctions_review_threshold: float = 0.85
    sanctions_block_threshold: float = 0.97

    # risk
    overdue_grace_days: int = 30
    over_limit_tolerance_pct: int = 5

    # cash application
    short_pay_tolerance_cents: int = 50_00
    payer_match_threshold: float = 0.90
    max_subset_invoices: int = 12

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_POLICY = Policy()

POLICIES: dict[str, Policy] = {
    "v1": DEFAULT_POLICY,
    # Stricter collections: hold orders sooner, no over-limit tolerance, smaller write-offs.
    "v2-strict": replace(
        DEFAULT_POLICY,
        version="v2-strict",
        overdue_grace_days=15,
        over_limit_tolerance_pct=0,
        short_pay_tolerance_cents=10_00,
    ),
}


def get_policy(version: str) -> Policy:
    try:
        return POLICIES[version]
    except KeyError:
        raise KeyError(f"unknown policy {version!r}; known: {', '.join(POLICIES)}") from None
