from ledgerloop.events import Event
from ledgerloop.state import Effect, State

_counter = {"n": 0}


def ev(type_, payload, at="2026-09-10T12:00:00Z", event_id=None):
    _counter["n"] += 1
    return Event(event_id or f"t{_counter['n']}", type_, at, payload)


class NoTools:
    def call(self, tool, payload):
        raise AssertionError(f"unexpected tool call {tool}")


def approved_customer(state: State, cid="c1", name="Acme Industrial", limit=100_000_00, tax_id=None, country="US"):
    state.apply(
        Effect(
            "customer.upsert",
            {
                "customer_id": cid,
                "legal_name": name,
                "tax_id": tax_id or f"TAX-{cid}",
                "country": country,
                "status": "approved",
                "credit_limit_cents": limit,
            },
        )
    )


def invoice(state: State, inv_id, cid="c1", amount=10_000_00, due="2026-09-30", order_id=None):
    state.add_invoice(
        {
            "invoice_id": inv_id,
            "order_id": order_id or f"o-{inv_id}",
            "customer_id": cid,
            "amount_cents": amount,
            "due_date": due,
            "issued_at": "2026-09-01",
        }
    )
