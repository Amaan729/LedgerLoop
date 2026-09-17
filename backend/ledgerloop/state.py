"""In-memory projections of the order-to-cash world.

State is disposable. It is rebuilt by folding the event log, which is why agents
are only allowed to change it through `Effect`s that the engine applies. Nothing
else mutates these objects.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .events import canonical_json, parse_date, sha256_hex
from .text import block_key, normalize_name


@dataclass
class Customer:
    customer_id: str
    legal_name: str
    norm_name: str
    tax_id: str | None
    country: str
    status: str = "pending"  # pending | approved | rejected | review
    credit_limit_cents: int = 0


@dataclass
class Order:
    order_id: str
    customer_id: str
    amount_cents: int
    placed_at: str
    status: str = "pending"  # released | held | review
    invoiced: bool = False


@dataclass
class Invoice:
    invoice_id: str
    order_id: str
    customer_id: str
    amount_cents: int
    open_cents: int
    due_date: str
    issued_at: str
    written_off_cents: int = 0

    @property
    def is_open(self) -> bool:
        return self.open_cents > 0


@dataclass
class Payment:
    payment_id: str
    payer_name: str
    amount_cents: int
    received_at: str
    memo: str
    bank_ref: str | None
    status: str = "pending"  # applied | partially_applied | review
    customer_id: str | None = None
    applications: list[list[Any]] = field(default_factory=list)  # [[invoice_id, cents], ...]
    unapplied_cents: int = 0


@dataclass(frozen=True)
class Effect:
    op: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "data": self.data}


class State:
    def __init__(self) -> None:
        self.customers: dict[str, Customer] = {}
        self.orders: dict[str, Order] = {}
        self.invoices: dict[str, Invoice] = {}
        self.payments: dict[str, Payment] = {}
        self.open_exceptions: dict[str, dict[str, Any]] = {}
        # indexes
        self._by_tax_id: dict[str, str] = {}
        self._by_block: dict[str, set[str]] = defaultdict(set)
        # open invoice ids per customer, insertion-ordered so iteration is deterministic
        self._open_invoices: dict[str, dict[str, None]] = defaultdict(dict)
        self._uninvoiced: dict[str, int] = defaultdict(int)
        self._payments_by_bank_ref: dict[str, str] = {}
        # Bumped whenever a name block's membership or a member's status changes, so
        # memoized name lookups know when they're stale. Not part of the snapshot.
        self.block_version: dict[str, int] = defaultdict(int)
        self.memo: dict[tuple[str, ...], tuple[int, object]] = {}

    # ---- reads used by agents -------------------------------------------------

    def customer_by_tax_id(self, tax_id: str) -> Customer | None:
        cid = self._by_tax_id.get(tax_id)
        return self.customers.get(cid) if cid else None

    def customers_in_block(self, norm_name: str) -> list[Customer]:
        return [self.customers[c] for c in sorted(self._by_block.get(block_key(norm_name), ()))]

    def open_invoices(self, customer_id: str) -> list[Invoice]:
        return [self.invoices[i] for i in self._open_invoices.get(customer_id, ())]

    def open_ar_cents(self, customer_id: str) -> int:
        return sum(inv.open_cents for inv in self.open_invoices(customer_id))

    def uninvoiced_exposure_cents(self, customer_id: str) -> int:
        """Released orders that haven't been invoiced yet still count against the limit."""
        return self._uninvoiced.get(customer_id, 0)

    def overdue_invoices(self, customer_id: str, as_of: date, grace_days: int) -> list[Invoice]:
        out = []
        for inv in self.open_invoices(customer_id):
            if (as_of - parse_date(inv.due_date)).days > grace_days:
                out.append(inv)
        return out

    def payment_by_bank_ref(self, bank_ref: str) -> Payment | None:
        pid = self._payments_by_bank_ref.get(bank_ref)
        return self.payments.get(pid) if pid else None

    # ---- writes ---------------------------------------------------------------

    def add_invoice(self, p: dict[str, Any]) -> bool:
        """Record an issued invoice. Returns False if the invoice id was already issued."""
        if p["invoice_id"] in self.invoices:
            return False
        inv = Invoice(
            invoice_id=p["invoice_id"],
            order_id=p["order_id"],
            customer_id=p["customer_id"],
            amount_cents=p["amount_cents"],
            open_cents=p["amount_cents"],
            due_date=p["due_date"],
            issued_at=p.get("issued_at", ""),
        )
        self.invoices[inv.invoice_id] = inv
        if inv.is_open:
            self._open_invoices[inv.customer_id][inv.invoice_id] = None
        order = self.orders.get(inv.order_id)
        if order and not order.invoiced:
            if order.status == "released":
                self._uninvoiced[order.customer_id] -= order.amount_cents
            order.invoiced = True
        return True

    def _reduce_invoice(self, inv: Invoice, cents: int) -> None:
        inv.open_cents -= cents
        if not inv.is_open:
            self._open_invoices[inv.customer_id].pop(inv.invoice_id, None)

    def apply(self, effect: Effect) -> None:
        d = effect.data
        op = effect.op
        if op == "customer.upsert":
            existing = self.customers.get(d["customer_id"])
            if existing is None:
                c = Customer(
                    customer_id=d["customer_id"],
                    legal_name=d["legal_name"],
                    norm_name=normalize_name(d["legal_name"]),
                    tax_id=d.get("tax_id"),
                    country=d["country"],
                )
                self.customers[c.customer_id] = c
                if c.tax_id:
                    self._by_tax_id.setdefault(c.tax_id, c.customer_id)
                self._by_block[block_key(c.norm_name)].add(c.customer_id)
                self.block_version[block_key(c.norm_name)] += 1
                existing = c
            if d.get("status", existing.status) != existing.status:
                self.block_version[block_key(existing.norm_name)] += 1
            existing.status = d.get("status", existing.status)
            existing.credit_limit_cents = d.get("credit_limit_cents", existing.credit_limit_cents)
        elif op == "order.upsert":
            o = self.orders.get(d["order_id"])
            if o is None:
                o = Order(d["order_id"], d["customer_id"], d["amount_cents"], d["placed_at"])
                self.orders[o.order_id] = o
            new_status = d.get("status", o.status)
            if not o.invoiced and new_status != o.status:
                if o.status == "released":
                    self._uninvoiced[o.customer_id] -= o.amount_cents
                if new_status == "released":
                    self._uninvoiced[o.customer_id] += o.amount_cents
            o.status = new_status
        elif op == "payment.upsert":
            p = self.payments.get(d["payment_id"])
            if p is None:
                p = Payment(
                    payment_id=d["payment_id"],
                    payer_name=d["payer_name"],
                    amount_cents=d["amount_cents"],
                    received_at=d["received_at"],
                    memo=d.get("memo", ""),
                    bank_ref=d.get("bank_ref"),
                )
                self.payments[p.payment_id] = p
                if p.bank_ref:
                    self._payments_by_bank_ref.setdefault(p.bank_ref, p.payment_id)
            for k in ("status", "customer_id", "unapplied_cents"):
                if k in d:
                    setattr(p, k, d[k])
        elif op == "invoice.apply_cash":
            inv = self.invoices[d["invoice_id"]]
            if d["cents"] > inv.open_cents:
                raise ValueError(f"over-application on {inv.invoice_id}")
            self._reduce_invoice(inv, d["cents"])
            self.payments[d["payment_id"]].applications.append([inv.invoice_id, d["cents"]])
        elif op == "invoice.write_off":
            inv = self.invoices[d["invoice_id"]]
            if d["cents"] > inv.open_cents:
                raise ValueError(f"over-write-off on {inv.invoice_id}")
            self._reduce_invoice(inv, d["cents"])
            inv.written_off_cents += d["cents"]
        elif op == "exception.open":
            self.open_exceptions[d["exception_id"]] = d
        elif op == "exception.close":
            self.open_exceptions.pop(d["exception_id"], None)
        else:
            raise ValueError(f"unknown effect {op}")

    # ---- replay support ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "customers": {k: asdict(v) for k, v in sorted(self.customers.items())},
            "orders": {k: asdict(v) for k, v in sorted(self.orders.items())},
            "invoices": {k: asdict(v) for k, v in sorted(self.invoices.items())},
            "payments": {k: asdict(v) for k, v in sorted(self.payments.items())},
            "open_exceptions": sorted(self.open_exceptions),
        }

    def state_hash(self) -> str:
        return sha256_hex(canonical_json(self.snapshot()))
