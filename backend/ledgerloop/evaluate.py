"""Score a run against the simulator's ground truth.

Auto-resolution rate alone rewards recklessness: an agent that auto-applies
every payment somewhere scores 100%. So alongside it we count auto-decisions
that were wrong: cash applied to the wrong invoices or applied twice, customers
approved who should have been caught, over-limit orders released.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import db
from .state import State

SHOULD_NOT_AUTO_APPROVE = {
    "missing_tax_id", "duplicate_reapply", "duplicate_tax_id", "sanctions_exact",
    "sanctions_near", "limit_far_above_policy", "high_risk_country",
}


def evaluate(conn: Connection, state: State, truth: dict[str, Any]) -> dict[str, Any]:
    rows = conn.execute(
        select(db.decisions.c.agent, db.decisions.c.subject_id, db.decisions.c.action,
               db.decisions.c.auto_resolved, db.decisions.c.reasons)
        .where(db.decisions.c.agent != "human")
    ).all()
    by_subject = {(a, s): (act, auto, reasons) for a, s, act, auto, reasons in rows}

    # ---- cash ----
    cash = Counter()
    cash_by_scenario: dict[str, Counter] = {}
    wrong_examples = []
    for pid, t in truth.get("payments", {}).items():
        d = by_subject.get(("cash", pid))
        if d is None:
            continue
        action, auto, reasons = d
        sc = cash_by_scenario.setdefault(t["scenario"], Counter())
        sc["total"] += 1
        if auto:
            cash["auto"] += 1
            sc["auto"] += 1
            applied = sorted({inv for inv, _ in state.payments[pid].applications})
            ok = t["should_apply"] and applied == sorted(t["invoices"])
            if ok:
                cash["auto_correct"] += 1
            else:
                cash["auto_wrong"] += 1
                sc["auto_wrong"] += 1
                if len(wrong_examples) < 10:
                    wrong_examples.append({"payment_id": pid, "scenario": t["scenario"], "expected": t["invoices"],
                                           "applied": applied, "reasons": reasons})
        else:
            cash["review"] += 1
            if not t["should_apply"]:
                cash["review_caught_bad_payment"] += 1

    # ---- onboarding ----
    onb = Counter()
    for cid, t in truth.get("customers", {}).items():
        d = by_subject.get(("onboarding", cid))
        if d is None:
            continue
        action, auto, _ = d
        onb["total"] += 1
        if auto:
            onb["auto"] += 1
            if action == "approved" and t["scenario"] in SHOULD_NOT_AUTO_APPROVE:
                onb["auto_wrong"] += 1
            if action == "rejected" and t["scenario"] != "sanctions_exact":
                onb["auto_wrong"] += 1

    # ---- risk ----
    risk = Counter()
    for oid, t in truth.get("orders", {}).items():
        d = by_subject.get(("risk", oid))
        if d is None:
            continue
        action, auto, reasons = d
        risk["total"] += 1
        if auto:
            risk["auto"] += 1
            if action == "released" and t["scenario"] == "over_limit":
                if "over_limit_within_tolerance" in reasons:
                    risk["released_within_tolerance"] += 1
                else:
                    risk["auto_wrong"] += 1

    def summarize(c: Counter) -> dict[str, Any]:
        out = dict(c)
        if c["auto"]:
            out["auto_precision"] = round(1 - c["auto_wrong"] / c["auto"], 5)
        return out

    total_auto = cash["auto"] + onb["auto"] + risk["auto"]
    total_wrong = cash["auto_wrong"] + onb["auto_wrong"] + risk["auto_wrong"]
    return {
        "cash": summarize(cash),
        "cash_by_scenario": {
            k: {**dict(v), "auto_rate": round(v["auto"] / v["total"], 3)} for k, v in sorted(cash_by_scenario.items())
        },
        "cash_wrong_examples": wrong_examples,
        "onboarding": summarize(onb),
        "risk": summarize(risk),
        "overall_auto_precision": round(1 - total_wrong / total_auto, 5) if total_auto else None,
        "wrong_auto_decisions": total_wrong,
    }
