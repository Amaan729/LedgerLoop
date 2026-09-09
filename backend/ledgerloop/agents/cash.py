"""Cash application agent: match each incoming payment to open invoices.

Order of attack:
  1. Guard against the same bank transfer arriving twice under different ids.
  2. Parse invoice refs out of the memo (a recorded tool call).
  3. Work out who paid: from the referenced invoices, else from the payer name.
  4. If there are usable refs, allocate against them.
  5. Otherwise match on amount: one invoice, a unique combination of invoices,
     or a single invoice short-paid by less than the write-off tolerance.
Anything ambiguous goes to a person. A wrong auto-match is worse than a
manual one, so ties are never broken by guessing.
"""

from __future__ import annotations

from typing import Any

from ..events import PAYMENT_RECEIVED, Event
from ..policy import Policy
from ..state import Customer, Effect, Invoice, State
from ..text import normalize_name, similarity
from .base import Decision, Tools, needs_review

NAME = "cash"


_SUFFIX_WORDS = ("inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "gmbh")


def drop_cut_suffix(norm: str) -> str:
    """'acme foods group i' -> 'acme foods group' when the tail is a cut-off Inc/LLC/Corp."""
    head, _, tail = norm.rpartition(" ")
    if head and len(tail) <= 3 and any(w.startswith(tail) for w in _SUFFIX_WORDS):
        return head
    return norm


def name_score(payer_norm: str, customer_norm: str) -> float:
    if payer_norm == customer_norm:
        return 1.0
    # Bank feeds cut names at a fixed width ("DRIFTWOOD ENERGY S"). A long prefix of
    # exactly one customer's name is better evidence than raw edit similarity, which
    # would prefer the shorter "Driftwood Energy" over "Driftwood Energy Solutions".
    if len(payer_norm) >= 12 and customer_norm.startswith(payer_norm):
        return 0.98
    return similarity(payer_norm, customer_norm)


def identify_payer(state: State, payer_name: str, threshold: float) -> tuple[Customer | None, float]:
    norm = normalize_name(payer_name)
    variants = {norm, drop_cut_suffix(norm)}
    scored = []
    for c in state.customers_in_block(norm):
        if c.status == "rejected":
            continue
        scored.append((max(name_score(v, c.norm_name) for v in variants), c.customer_id, c))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda t: (-t[0], t[1]))
    best_score, _, best = scored[0]
    if best_score < threshold:
        return None, best_score
    if len(scored) > 1 and best_score - scored[1][0] < 0.03:
        return None, best_score  # two customers look equally plausible
    return best, best_score


def one_edit_apart(a: str, b: str) -> bool:
    """Same length and either one substituted char or one adjacent swap."""
    if len(a) != len(b) or a == b:
        return False
    diffs = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diffs) == 1:
        return True
    if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
        i, j = diffs
        return a[i] == b[j] and a[j] == b[i]
    return False


def resolve_refs(
    state: State, refs: list[str], payer: Customer | None
) -> tuple[list[Invoice], list[dict[str, str]], list[str]]:
    """Turn parsed refs into invoices, repairing single-digit typos against the payer's open invoices."""
    found: list[Invoice] = []
    repaired: list[dict[str, str]] = []
    unknown: list[str] = []
    payer_open = state.open_invoices(payer.customer_id) if payer else []
    for ref in refs:
        inv = state.invoices.get(ref)
        if inv is not None:
            # Real invoice. If it belongs to someone other than the payer we keep it
            # anyway and let the caller flag the mismatch rather than "fixing" it.
            found.append(inv)
            continue
        # Only refs that don't exist anywhere get repaired, and only to a unique candidate.
        candidates = [i for i in payer_open if one_edit_apart(ref, i.invoice_id)]
        if len(candidates) == 1:
            found.append(candidates[0])
            repaired.append({"parsed": ref, "matched": candidates[0].invoice_id})
        else:
            unknown.append(ref)
    unique: dict[str, Invoice] = {}
    for inv in found:
        unique.setdefault(inv.invoice_id, inv)
    return list(unique.values()), repaired, unknown


def find_subsets(items: list[tuple[str, int]], target: int, limit: int = 2) -> list[list[str]]:
    """Combinations of 2+ invoices whose open amounts sum exactly to target. Stops at `limit`."""
    results: list[list[str]] = []
    chosen: list[str] = []

    def dfs(i: int, remaining: int) -> None:
        if len(results) >= limit:
            return
        if remaining == 0:
            if len(chosen) >= 2:
                results.append(list(chosen))
            return
        if remaining < 0 or i == len(items):
            return
        chosen.append(items[i][0])
        dfs(i + 1, remaining - items[i][1])
        chosen.pop()
        dfs(i + 1, remaining)

    dfs(0, target)
    return results


class CashAgent:
    name = NAME
    handles = frozenset({PAYMENT_RECEIVED})

    def decide(self, event: Event, state: State, policy: Policy, tools: Tools) -> Decision:
        p = event.payload
        pid, amount = p["payment_id"], p["amount_cents"]
        payment = {
            "payment_id": pid,
            "payer_name": p["payer_name"],
            "amount_cents": amount,
            "received_at": event.occurred_at,
            "memo": p.get("memo") or "",
            "bank_ref": p.get("bank_ref"),
        }

        def hold(kind: str, summary: str, detail: dict[str, Any], customer_id: str | None = None) -> Decision:
            return needs_review(
                NAME, pid, kind, summary, [kind], detail,
                effects=[Effect("payment.upsert", {**payment, "status": "review", "customer_id": customer_id, "unapplied_cents": amount})],
            )

        if payment["bank_ref"]:
            prior = state.payment_by_bank_ref(payment["bank_ref"])
            if prior is not None and prior.payment_id != pid:
                return hold(
                    "duplicate_bank_ref",
                    f"bank ref {payment['bank_ref']} was already received as {prior.payment_id}",
                    {"prior_payment_id": prior.payment_id},
                )

        parsed = tools.call("remittance_parser", {"memo": payment["memo"]})
        payer, payer_score = identify_payer(state, p["payer_name"], policy.payer_match_threshold)
        invoices, repaired, unknown = resolve_refs(state, parsed["refs"], payer)
        detail: dict[str, Any] = {
            "parsed_refs": parsed["refs"],
            "parser": parsed.get("parser"),
            "repaired_refs": repaired,
            "unknown_refs": unknown,
            "payer_match": {"customer_id": payer.customer_id if payer else None, "score": round(payer_score, 3)},
        }

        if invoices:
            owners = sorted({inv.customer_id for inv in invoices})
            if len(owners) > 1:
                return hold("refs_span_customers", f"memo references invoices of {len(owners)} customers", {**detail, "owners": owners})
            customer_id = owners[0]
            if payer is not None and payer.customer_id != customer_id:
                return hold(
                    "payer_invoice_mismatch",
                    f"{p['payer_name']} paid invoices belonging to {customer_id}",
                    {**detail, "invoice_owner": customer_id},
                )
            open_refs = sorted((inv for inv in invoices if inv.is_open), key=lambda i: (i.due_date, i.invoice_id))
            if not open_refs:
                return hold(
                    "invoices_already_paid",
                    "every referenced invoice is already closed; possible duplicate payment",
                    detail, customer_id,
                )
            return self._allocate_to_refs(pid, amount, payment, customer_id, open_refs, detail, policy)

        if payer is None:
            return hold("unknown_payer", f"can't tell who {p['payer_name']!r} is and the memo has no usable refs", detail)
        return self._match_by_amount(state, pid, amount, payment, payer.customer_id, detail, policy, hold)

    # -- strategies ---------------------------------------------------------------

    def _applied(
        self, pid: str, payment: dict[str, Any], customer_id: str, reason: str,
        allocations: list[tuple[str, int]], write_offs: list[tuple[str, int]], detail: dict[str, Any],
        unapplied: int = 0,
    ) -> Decision:
        effects = [Effect("payment.upsert", {**payment, "status": "applied", "customer_id": customer_id, "unapplied_cents": unapplied})]
        effects += [Effect("invoice.apply_cash", {"invoice_id": i, "payment_id": pid, "cents": c}) for i, c in allocations if c > 0]
        effects += [Effect("invoice.write_off", {"invoice_id": i, "payment_id": pid, "cents": c, "reason": "short_pay"}) for i, c in write_offs]
        return Decision(
            NAME, "applied", pid, True, [reason],
            {**detail, "allocations": allocations, "write_offs": write_offs}, effects,
        )

    def _allocate_to_refs(self, pid, amount, payment, customer_id, open_refs, detail, policy) -> Decision:
        total_open = sum(inv.open_cents for inv in open_refs)
        if amount == total_open:
            return self._applied(pid, payment, customer_id, "reference_exact",
                                 [(i.invoice_id, i.open_cents) for i in open_refs], [], detail)
        if amount < total_open:
            short = total_open - amount
            if short <= policy.short_pay_tolerance_cents:
                allocs = [(i.invoice_id, i.open_cents) for i in open_refs]
                last_id, last_open = allocs[-1]
                allocs[-1] = (last_id, last_open - short)
                return self._applied(pid, payment, customer_id, "reference_short_pay_within_tolerance",
                                     allocs, [(last_id, short)], {**detail, "short_cents": short})
            if len(open_refs) == 1:
                return self._applied(pid, payment, customer_id, "reference_partial_payment",
                                     [(open_refs[0].invoice_id, amount)], [], {**detail, "remaining_cents": short})
            return needs_review(
                NAME, pid, "ambiguous_allocation",
                f"${amount / 100:,.2f} doesn't cover {len(open_refs)} referenced invoices (${total_open / 100:,.2f})",
                ["ambiguous_allocation"], {**detail, "total_open_cents": total_open},
                effects=[Effect("payment.upsert", {**payment, "status": "review", "customer_id": customer_id, "unapplied_cents": amount})],
            )
        over = amount - total_open
        d = self._applied(pid, payment, customer_id, "reference_exact",
                          [(i.invoice_id, i.open_cents) for i in open_refs], [], {**detail, "over_cents": over}, unapplied=over)
        d.auto_resolved = False
        d.action = "applied_with_overpayment"
        d.reasons = ["overpayment"]
        d.exception_kind = "overpayment"
        d.exception_summary = f"${over / 100:,.2f} left unapplied after closing the referenced invoices"
        return d

    def _match_by_amount(self, state, pid, amount, payment, customer_id, detail, policy, hold) -> Decision:
        opens = sorted(state.open_invoices(customer_id), key=lambda i: (i.due_date, i.invoice_id))
        if not opens:
            return hold("no_open_invoices", "payer has no open invoices", detail, customer_id)

        exact = [i for i in opens if i.open_cents == amount]
        if len(exact) == 1:
            return self._applied(pid, payment, customer_id, "amount_exact", [(exact[0].invoice_id, amount)], [], detail)
        if len(exact) > 1:
            return hold("ambiguous_amount_match", f"{len(exact)} open invoices for exactly this amount",
                        {**detail, "candidates": [i.invoice_id for i in exact]}, customer_id)

        window = [(i.invoice_id, i.open_cents) for i in opens[: policy.max_subset_invoices]]
        combos = find_subsets(window, amount)
        if len(combos) == 1:
            by_id = {i.invoice_id: i for i in opens}
            return self._applied(pid, payment, customer_id, "amount_multi_invoice",
                                 [(iid, by_id[iid].open_cents) for iid in combos[0]], [], detail)
        if len(combos) > 1:
            return hold("ambiguous_amount_match", "more than one set of invoices adds up to this amount",
                        {**detail, "candidates": combos}, customer_id)

        near = [i for i in opens if 0 < i.open_cents - amount <= policy.short_pay_tolerance_cents]
        if len(near) == 1:
            inv = near[0]
            short = inv.open_cents - amount
            return self._applied(pid, payment, customer_id, "amount_short_pay_within_tolerance",
                                 [(inv.invoice_id, amount)], [(inv.invoice_id, short)], {**detail, "short_cents": short})

        return hold("unmatched", f"no invoice or combination matches ${amount / 100:,.2f}",
                    {**detail, "open_invoice_count": len(opens)}, customer_id)
