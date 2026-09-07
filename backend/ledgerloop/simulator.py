"""Synthetic order-to-cash workload with ground truth.

There is no real customer data in this project, so the benchmark runs on a
generated stream. The generator models a B2B business over N days: customers
apply, place orders against their credit, get invoiced, and pay. Payments are
where real AR data is messy, so most of the knobs are there: missing or
typo'd remittance memos, bank fees taken out of the wire, partial payments,
one wire covering several invoices, payer names truncated by the bank, and
the occasional duplicate or double payment.

The scenario rates below are rough guesses, not measurements from a real AR
team. Auto-resolution numbers depend heavily on them, so the benchmark prints
the rates it ran with. Ground truth (which invoices a payment was really for,
which applications were duplicates) is returned separately so we can measure
how often an auto-decision was *wrong*, not just how often one was made.

Deterministic for a given seed.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .events import Event
from .text import normalize_name

PREFIXES = """Acme Brightline Cobalt Summit Harbor Pioneer Evergreen Redwood Keystone Northwind Bluewater
Ironclad Silverline Granite Meridian Atlas Orion Sterling Cascade Liberty Frontier Beacon Crescent Pinnacle
Vertex Horizon Trident Falcon Juniper Sequoia Copperleaf Driftwood Ember Foxglove Glacier Hollowell Indigo
Kestrel Lakeshore Mosaic Nimbus Oakridge Prairie Quartz Riverstone Saffron Tamarack Upland Vantage Willow
Yellowstone Zephyr Alder Birchwood Canyon Delta Eastgate Fairview Goldcrest Highland""".split()
INDUSTRIES = """Industrial Foods Logistics Supply Manufacturing Health Energy Textiles Components Builders
Distribution Labs Plastics Metals Packaging Electronics Farms Motors Paper Chemicals Robotics Apparel
Hardware Marine Optics""".split()
MIDDLES = ["", "", "Group", "Partners", "Systems", "Works", "Solutions", "Trading"]
SUFFIXES = ["Inc", "LLC", "Corp", "Ltd", "Co", "Inc.", "L.L.C."]
COUNTRIES = ["US"] * 14 + ["CA", "CA", "MX", "GB", "DE"]


@dataclass
class ScenarioRates:
    # onboarding
    missing_tax_id: float = 0.03
    duplicate_reapply: float = 0.03
    duplicate_tax_id: float = 0.01
    sanctions_exact: float = 0.005
    sanctions_near: float = 0.005
    limit_far_above_policy: float = 0.03
    high_risk_country: float = 0.005
    # customer behaviour
    slow_payer: float = 0.06
    # orders
    push_over_limit: float = 0.30  # when an order would exceed the limit, how often sales submits it anyway
    round_amount: float = 0.10
    # payments (fractions of payment events; remaining mass is a clean single-invoice payment)
    multi_invoice_wire: float = 0.10
    ref_format_variant: float = 0.12
    ocr_letter_swap: float = 0.03
    digit_typo: float = 0.03
    no_memo: float = 0.12
    bank_fee_short_pay: float = 0.05
    partial_payment: float = 0.02
    overpayment: float = 0.01
    parent_company_payer: float = 0.02
    truncated_payer_name: float = 0.25
    duplicate_bank_resend: float = 0.005
    double_payment: float = 0.005
    redelivered_event: float = 0.01  # same event delivered twice by the transport


@dataclass
class SimConfig:
    seed: int = 42
    customers: int = 1000
    days: int = 120
    orders_per_customer_per_day: float = 0.28
    start: str = "2026-05-01T00:00:00+00:00"
    rates: ScenarioRates = field(default_factory=ScenarioRates)


@dataclass
class SimCustomer:
    cid: str
    name: str
    tax_id: str | None
    country: str
    limit: int
    slow: bool
    scenario: str
    active_from: int  # day index when orders may start
    open_ar: int = 0
    uninvoiced: int = 0


@dataclass
class SimInvoice:
    inv_id: str
    cid: str
    amount: int
    issued_day: int
    open: int
    paid_by: list[str] = field(default_factory=list)


class Simulator:
    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.r = random.Random(cfg.seed)
        self.rates = cfg.rates
        self.t0 = datetime.fromisoformat(cfg.start).astimezone(timezone.utc)
        self.events: list[tuple[datetime, int, str, Event]] = []
        self.truth: dict[str, dict[str, Any]] = {"customers": {}, "orders": {}, "payments": {}}
        self.customers: list[SimCustomer] = []
        self.invoices: dict[str, SimInvoice] = {}
        self.used_names: set[str] = set()
        self.n_order = 0
        self.n_inv = 0
        self.n_pay = 0
        self.n_bank = 0
        # payments scheduled for a future day: day -> list of (cid, [invoice ids])
        self.pay_schedule: dict[int, list[tuple[str, list[str]]]] = {}
        self._by_cid: dict[str, SimCustomer] = {}

    # ---- helpers ----------------------------------------------------------------

    def ts(self, day: int, hour_lo: int = 8, hour_hi: int = 18) -> datetime:
        seconds = self.r.randint(hour_lo * 3600, hour_hi * 3600 - 1)
        return self.t0 + timedelta(days=day, seconds=seconds)

    def emit(self, when: datetime, type_: str, payload: dict[str, Any], event_id: str) -> None:
        order = {"customer.applied": 0, "order.placed": 1, "invoice.issued": 2, "payment.received": 3}[type_]
        ev = Event(event_id, type_, when.isoformat().replace("+00:00", "Z"), payload, source="simulator")
        self.events.append((when, order, event_id, ev))
        if self.r.random() < self.rates.redelivered_event:
            self.events.append((when + timedelta(seconds=self.r.randint(1, 300)), order, event_id + "~", ev))

    def new_name(self) -> str:
        for _ in range(1000):
            parts = [self.r.choice(PREFIXES), self.r.choice(INDUSTRIES), self.r.choice(MIDDLES), self.r.choice(SUFFIXES)]
            name = " ".join(p for p in parts if p)
            norm = normalize_name(name)
            if norm not in self.used_names:
                self.used_names.add(norm)
                return name
        raise RuntimeError("ran out of names")

    def amount(self, lo: int, hi: int) -> int:
        if self.r.random() < self.rates.round_amount:
            return self.r.randint(max(1, lo // 100_00), max(1, hi // 100_00)) * 100_00
        return self.r.randint(lo, hi)

    # ---- phases -----------------------------------------------------------------

    def gen_customers(self) -> None:
        r, R = self.r, self.rates
        from .reference import SANCTIONED_ENTITIES

        for i in range(self.cfg.customers):
            cid = f"CUST-{i + 1:05d}"
            # front-load: a third apply in the first week, the rest trickle in
            day = r.randint(0, 6) if r.random() < 0.33 else r.randint(0, int(self.cfg.days * 0.7))
            name = self.new_name()
            tax_id: str | None = f"{r.randint(10, 99)}-{r.randint(1000000, 9999999)}"
            country = r.choice(COUNTRIES)
            revenue = r.randint(500_000_00, 50_000_000_00)
            policy_limit = revenue // 10
            requested = min(self.amount(20_000_00, 300_000_00), policy_limit)
            scenario = "clean"

            roll = r.random()
            cum = 0.0
            for s, p in [
                ("missing_tax_id", R.missing_tax_id),
                ("duplicate_reapply", R.duplicate_reapply),
                ("duplicate_tax_id", R.duplicate_tax_id),
                ("sanctions_exact", R.sanctions_exact),
                ("sanctions_near", R.sanctions_near),
                ("limit_far_above_policy", R.limit_far_above_policy),
                ("high_risk_country", R.high_risk_country),
            ]:
                cum += p
                if roll < cum:
                    scenario = s
                    break

            previous = [c for c in self.customers if c.scenario == "clean"]
            if scenario == "missing_tax_id":
                tax_id = None
            elif scenario in ("duplicate_reapply", "duplicate_tax_id") and previous:
                orig = r.choice(previous)
                country = orig.country
                # a re-application has to come after the original, or the original looks like the duplicate
                day = min(self.cfg.days - 1, max(day, orig.active_from + r.randint(1, 20)))
                if scenario == "duplicate_reapply":
                    # same company applying again under a slightly different legal name
                    stem = normalize_name(orig.name).title()
                    name = f"{stem} {r.choice(['Inc', 'LLC', 'Corp'])}"
                else:
                    tax_id = orig.tax_id
            elif scenario == "sanctions_exact":
                name = r.choice(SANCTIONED_ENTITIES)
            elif scenario == "sanctions_near":
                base = r.choice(SANCTIONED_ENTITIES).split()
                name = " ".join(base[:-1] + [base[-1][:-1]])  # drop a letter off the last word
            elif scenario == "limit_far_above_policy":
                requested = policy_limit * r.randint(4, 8)
            elif scenario == "high_risk_country":
                country = "XX"
            elif scenario in ("duplicate_reapply", "duplicate_tax_id"):
                scenario = "clean"

            payload = {
                "customer_id": cid,
                "legal_name": name,
                "tax_id": tax_id,
                "country": country,
                "requested_limit_cents": requested,
                "annual_revenue_cents": revenue,
                "email": f"ap@{normalize_name(name).replace(' ', '')[:20]}.example",
            }
            self.emit(self.ts(day), "customer.applied", payload, f"evt-cust-{cid}")
            granted = min(requested, policy_limit, 250_000_00)
            slow = r.random() < R.slow_payer
            c = SimCustomer(cid, name, tax_id, country, granted, slow, scenario, active_from=day + 1)
            self.customers.append(c)
            self._by_cid[cid] = c
            self.truth["customers"][cid] = {"scenario": scenario}

    def gen_orders_and_invoices(self) -> None:
        r, R = self.r, self.rates
        active = [c for c in self.customers if c.scenario == "clean"]
        for day in range(self.cfg.days):
            # payments that were scheduled for today go out first so AR is up to date
            for cid, inv_ids in self.pay_schedule.pop(day, []):
                self.gen_payment(day, cid, inv_ids)
            for c in active:
                if day < c.active_from or r.random() >= self.cfg.orders_per_customer_per_day:
                    continue
                self.n_order += 1
                oid = f"SO-{self.n_order:07d}"
                amt = self.amount(max(500_00, c.limit // 100), max(1_000_00, c.limit // 6))
                exposure = c.open_ar + c.uninvoiced + amt
                scenario = "within_limit"
                if exposure > c.limit:
                    if r.random() < R.push_over_limit:
                        scenario = "over_limit"  # sales pushes it through anyway; risk should hold it
                    else:
                        self.n_order -= 1
                        continue
                when = self.ts(day)
                self.emit(when, "order.placed", {"order_id": oid, "customer_id": c.cid, "amount_cents": amt}, f"evt-ord-{oid}")
                self.truth["orders"][oid] = {"scenario": scenario, "slow_payer": c.slow}
                if scenario == "over_limit":
                    continue  # not shipped, not invoiced
                c.uninvoiced += amt
                inv_day = day + r.randint(1, 3)
                if inv_day >= self.cfg.days:
                    continue
                self.n_inv += 1
                inv_id = f"INV-{100000 + self.n_inv}"
                due = (self.t0 + timedelta(days=inv_day + 30)).date().isoformat()
                self.emit(
                    self.ts(inv_day),
                    "invoice.issued",
                    {"invoice_id": inv_id, "order_id": oid, "customer_id": c.cid, "amount_cents": amt,
                     "due_date": due, "issued_at": (self.t0 + timedelta(days=inv_day)).date().isoformat()},
                    f"evt-inv-{inv_id}",
                )
                c.uninvoiced -= amt
                c.open_ar += amt
                self.invoices[inv_id] = SimInvoice(inv_id, c.cid, amt, inv_day, amt)
                delay = r.randint(50, 80) if c.slow else r.randint(18, 38)
                pay_day = inv_day + delay
                if r.random() < R.multi_invoice_wire:
                    # batch it with whatever else this customer is paying around then
                    pay_day = pay_day - pay_day % 7 + 6
                self.schedule(pay_day, c.cid, inv_id)
        # anything still scheduled past the horizon stays unpaid

    def schedule(self, day: int, cid: str, inv_id: str) -> None:
        todays = self.pay_schedule.setdefault(day, [])
        for entry in todays:
            if entry[0] == cid:
                entry[1].append(inv_id)
                return
        todays.append((cid, [inv_id]))

    # ---- payments -----------------------------------------------------------------

    def payer_name(self, c: SimCustomer) -> str:
        r = self.r
        if r.random() < self.rates.truncated_payer_name:
            return c.name.upper()[:18]  # bank feeds love fixed-width fields
        if r.random() < 0.3:
            return c.name.upper()
        return c.name

    def ref_text(self, inv_id: str, style: str) -> str:
        digits = inv_id.split("-")[1]
        if style == "variant":
            return self.r.choice([f"inv {digits}", f"Invoice #{digits}", f"INV{digits}", f"invoice no. {digits}", digits])
        if style == "ocr":
            i = digits.index("0") if "0" in digits else None
            if i is not None:
                digits = digits[:i] + "O" + digits[i + 1:]
            return f"INV-{digits}"
        if style == "typo":
            d = list(digits)
            if self.r.random() < 0.7:
                j = self.r.randint(1, len(d) - 2)
                d[j], d[j + 1] = d[j + 1], d[j]
            else:
                j = self.r.randint(1, len(d) - 1)
                d[j] = str((int(d[j]) + self.r.randint(1, 9)) % 10)
            return f"INV-{''.join(d)}"
        return inv_id

    def gen_payment(self, day: int, cid: str, inv_ids: list[str]) -> None:
        r, R = self.r, self.rates
        c = self._by_cid[cid]
        invs = [self.invoices[i] for i in inv_ids if self.invoices[i].open > 0]
        if not invs:
            return
        total = sum(i.open for i in invs)
        amount = total
        scenario = "clean" if len(invs) == 1 else "multi_invoice"
        memo_style = "plain"
        payer = self.payer_name(c)
        truth_invoices = [i.inv_id for i in invs]
        applies = True

        roll = r.random()
        cum = 0.0
        pick = None
        for s, p in [
            ("ref_format_variant", R.ref_format_variant),
            ("ocr_letter_swap", R.ocr_letter_swap),
            ("digit_typo", R.digit_typo),
            ("no_memo", R.no_memo),
            ("bank_fee_short_pay", R.bank_fee_short_pay),
            ("partial_payment", R.partial_payment),
            ("overpayment", R.overpayment),
            ("parent_company_payer", R.parent_company_payer),
        ]:
            cum += p
            if roll < cum:
                pick = s
                break
        if pick == "partial_payment" and len(invs) > 1:
            pick = None  # partials only make sense against a single invoice
        if pick == "ref_format_variant":
            memo_style = "variant"
        elif pick == "ocr_letter_swap":
            memo_style = "ocr"
        elif pick == "digit_typo":
            memo_style = "typo"
        elif pick == "no_memo":
            memo_style = "none"
        elif pick == "bank_fee_short_pay":
            amount = total - r.randint(10_00, 45_00)
        elif pick == "partial_payment":
            amount = total * r.randint(30, 70) // 100
        elif pick == "overpayment":
            amount = total + r.randint(100_00, 900_00)
        elif pick == "parent_company_payer":
            payer = f"{c.name.split()[0]} Holdings Group"
            memo_style = r.choice(["plain", "none"])
        if pick:
            scenario = pick

        if memo_style == "none":
            memo = r.choice(["", "PAYMENT", "ACH CREDIT", "thank you"])
        else:
            refs = [self.ref_text(i.inv_id, memo_style) for i in invs]
            memo = r.choice(["", "Payment for ", "PMT ", "Remit: "]) + ", ".join(refs)

        self.n_pay += 1
        self.n_bank += 1
        pid = f"PMT-{self.n_pay:07d}"
        bank_ref = f"FED{self.n_bank:09d}"
        when = self.ts(day)
        self.emit(when, "payment.received",
                  {"payment_id": pid, "payer_name": payer, "amount_cents": amount, "memo": memo, "bank_ref": bank_ref},
                  f"evt-pay-{pid}")
        self.truth["payments"][pid] = {"scenario": scenario, "invoices": truth_invoices, "should_apply": applies}

        # the sim's own books: assume the payment lands where it was meant to
        remaining = amount
        for inv in invs:
            take = min(inv.open, remaining)
            if scenario == "bank_fee_short_pay":
                take = inv.open
            inv.open -= take
            remaining -= take
            c.open_ar -= take
        if scenario == "partial_payment":
            self.schedule(day + r.randint(10, 25), cid, invs[0].inv_id)

        # a few copies of the same money arriving again
        if r.random() < R.duplicate_bank_resend:
            self.n_pay += 1
            pid2 = f"PMT-{self.n_pay:07d}"
            self.emit(when + timedelta(minutes=r.randint(5, 120)), "payment.received",
                      {"payment_id": pid2, "payer_name": payer, "amount_cents": amount, "memo": memo, "bank_ref": bank_ref},
                      f"evt-pay-{pid2}")
            self.truth["payments"][pid2] = {"scenario": "duplicate_bank_resend", "invoices": [], "should_apply": False}
        elif r.random() < R.double_payment and len(invs) == 1:
            self.n_pay += 1
            self.n_bank += 1
            pid2 = f"PMT-{self.n_pay:07d}"
            self.emit(self.ts(day + r.randint(2, 9)), "payment.received",
                      {"payment_id": pid2, "payer_name": payer, "amount_cents": total, "memo": invs[0].inv_id,
                       "bank_ref": f"FED{self.n_bank:09d}"},
                      f"evt-pay-{pid2}")
            self.truth["payments"][pid2] = {"scenario": "double_payment", "invoices": [], "should_apply": False}

    def run(self) -> tuple[list[Event], dict[str, Any]]:
        self.gen_customers()
        self.gen_orders_and_invoices()
        self.events.sort(key=lambda t: (t[0], t[1], t[2]))
        evs = [e for _, _, _, e in self.events]
        self.truth["config"] = {"seed": self.cfg.seed, "customers": self.cfg.customers, "days": self.cfg.days,
                                "rates": asdict(self.cfg.rates)}
        return evs, self.truth


def simulate(cfg: SimConfig | None = None) -> tuple[list[Event], dict[str, Any]]:
    return Simulator(cfg or SimConfig()).run()


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a synthetic order-to-cash event stream")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--customers", type=int, default=1000)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--out", default="events.jsonl")
    ap.add_argument("--truth", default="truth.json")
    a = ap.parse_args()
    events, truth = simulate(SimConfig(seed=a.seed, customers=a.customers, days=a.days))
    with open(a.out, "w") as f:
        for e in events:
            f.write(json.dumps(e.to_dict()) + "\n")
    with open(a.truth, "w") as f:
        json.dump(truth, f)
    print(f"wrote {len(events)} events to {a.out}")


if __name__ == "__main__":
    main()
