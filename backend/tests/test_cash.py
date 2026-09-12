from ledgerloop.agents.cash import CashAgent, find_subsets, one_edit_apart
from ledgerloop.policy import DEFAULT_POLICY
from ledgerloop.state import State
from ledgerloop.tools.recorder import RecordingTools
from ledgerloop.tools.remittance import regex_remittance_parser

from .helpers import approved_customer, ev, invoice

agent = CashAgent()


def pay(amount, memo="", payer="Acme Industrial Inc", pid="p1", bank_ref=None):
    return ev(
        "payment.received",
        {"payment_id": pid, "payer_name": payer, "amount_cents": amount, "memo": memo, "bank_ref": bank_ref},
    )


def decide(event, state):
    tools = RecordingTools({"remittance_parser": regex_remittance_parser})
    tools.begin(event.event_id)
    d = agent.decide(event, state, DEFAULT_POLICY, tools)
    for e in d.effects:  # apply like the engine would, so tests catch bad effects
        state.apply(e)
    return d


def world():
    s = State()
    approved_customer(s, "c1", "Acme Industrial")
    approved_customer(s, "c2", "Borealis Freight")
    invoice(s, "INV-100001", "c1", 1_000_00, due="2026-09-01")
    invoice(s, "INV-100002", "c1", 2_500_00, due="2026-09-05")
    invoice(s, "INV-100003", "c1", 4_000_00, due="2026-09-10")
    invoice(s, "INV-200001", "c2", 1_000_00, due="2026-09-01")
    return s


def test_exact_reference_match_closes_invoice():
    s = world()
    d = decide(pay(2_500_00, "INV-100002"), s)
    assert d.action == "applied" and d.reasons == ["reference_exact"]
    assert s.invoices["INV-100002"].open_cents == 0


def test_multiple_refs_exact():
    s = world()
    d = decide(pay(3_500_00, "INV-100001 INV-100002"), s)
    assert d.reasons == ["reference_exact"]
    assert s.open_ar_cents("c1") == 4_000_00


def test_short_pay_within_tolerance_writes_off_the_difference():
    s = world()
    d = decide(pay(2_500_00 - 25_00, "INV-100002"), s)  # $25 wire fee
    assert d.reasons == ["reference_short_pay_within_tolerance"]
    inv = s.invoices["INV-100002"]
    assert inv.open_cents == 0 and inv.written_off_cents == 25_00


def test_partial_payment_on_single_ref_leaves_invoice_open():
    s = world()
    d = decide(pay(1_700_00, "INV-100003"), s)
    assert d.reasons == ["reference_partial_payment"]
    assert s.invoices["INV-100003"].open_cents == 2_300_00


def test_underpaying_several_refs_goes_to_review():
    s = world()
    d = decide(pay(1_000_00, "INV-100002 INV-100003"), s)
    assert d.exception_kind == "ambiguous_allocation"
    assert s.open_ar_cents("c1") == 7_500_00  # nothing applied


def test_overpayment_closes_invoice_and_flags_remainder():
    s = world()
    d = decide(pay(3_000_00, "INV-100002"), s)
    assert d.exception_kind == "overpayment" and not d.auto_resolved
    assert s.invoices["INV-100002"].open_cents == 0
    assert s.payments["p1"].unapplied_cents == 500_00


def test_typo_in_ref_is_repaired_against_payer_open_invoices():
    s = world()
    d = decide(pay(2_500_00, "INV-100020"), s)  # transposed digits of 100002
    assert d.action == "applied"
    assert d.detail["repaired_refs"] == [{"parsed": "INV-100020", "matched": "INV-100002"}]


def test_ref_to_another_customers_invoice_is_flagged():
    s = world()
    d = decide(pay(700_00, "INV-200001"), s)  # Acme paying part of Borealis's invoice
    assert d.exception_kind == "payer_invoice_mismatch"


def test_foreign_ref_is_repaired_when_amount_matches_payers_own_invoice():
    s = world()
    # INV-200001 is Borealis's, but Acme's INV-100001 is one digit away and exactly $1,000
    d = decide(pay(1_000_00, "INV-200001"), s)
    assert d.action == "applied"
    assert s.invoices["INV-100001"].open_cents == 0
    assert s.invoices["INV-200001"].open_cents == 1_000_00


def test_partial_that_equals_a_sibling_invoice_is_ambiguous():
    s = world()
    # $1,000 against INV-100003 ($4,000) could be a partial, or a typo for INV-100001 ($1,000)
    d = decide(pay(1_000_00, "INV-100003"), s)
    assert d.exception_kind == "ambiguous_reference"


def test_ref_to_paid_invoice_is_repaired_to_sibling_that_fits():
    s = world()
    decide(pay(1_000_00, "INV-100001", pid="p0"), s)
    d = decide(pay(2_500_00, "INV-100001", pid="p1"), s)  # meant INV-100002
    assert d.action == "applied"
    assert s.invoices["INV-100002"].open_cents == 0


def test_partial_from_unidentified_payer_goes_to_review():
    s = world()
    d = decide(pay(1_500_00, "INV-100003", payer="Some Holding Company"), s)
    assert d.exception_kind == "unverified_partial"


def test_no_memo_single_amount_match():
    s = world()
    d = decide(pay(4_000_00, ""), s)
    assert d.reasons == ["amount_exact"]
    assert s.invoices["INV-100003"].open_cents == 0


def test_no_memo_unique_combination():
    s = world()
    d = decide(pay(6_500_00, "thanks"), s)  # 2,500 + 4,000
    assert d.reasons == ["amount_multi_invoice"]
    assert s.open_ar_cents("c1") == 1_000_00


def test_no_memo_ambiguous_combination_goes_to_review():
    s = world()
    invoice(s, "INV-100004", "c1", 3_500_00, due="2026-09-12")
    d = decide(pay(3_500_00 + 4_000_00, ""), s)
    # 3,500 + 4,000 and 1,000 + 2,500 + 4,000 both sum to 7,500
    assert d.exception_kind == "ambiguous_amount_match"


def test_no_memo_short_pay_within_tolerance():
    s = world()
    d = decide(pay(4_000_00 - 15_00, ""), s)
    assert d.reasons == ["amount_short_pay_within_tolerance"]
    assert s.invoices["INV-100003"].written_off_cents == 15_00


def test_unknown_payer_without_refs():
    s = world()
    d = decide(pay(1_234_00, "", payer="Zzyzx Holdings"), s)
    assert d.exception_kind == "unknown_payer"


def test_payer_name_from_bank_feed_is_matched():
    s = world()
    d = decide(pay(4_000_00, "", payer="ACME INDUSTRIAL"), s)
    assert d.action == "applied"


def test_same_bank_ref_under_new_payment_id_is_flagged():
    s = world()
    decide(pay(1_000_00, "INV-100001", pid="p1", bank_ref="FED123"), s)
    d = decide(pay(1_000_00, "INV-100001", pid="p2", bank_ref="FED123"), s)
    assert d.exception_kind == "duplicate_bank_ref"


def test_paying_closed_invoice_is_flagged():
    s = world()
    decide(pay(1_000_00, "INV-100001", pid="p1"), s)
    d = decide(pay(1_000_00, "INV-100001", pid="p2"), s)
    assert d.exception_kind == "invoices_already_paid"


def test_one_edit_apart():
    assert one_edit_apart("INV-100002", "INV-100020")
    assert one_edit_apart("INV-100002", "INV-100003")
    assert not one_edit_apart("INV-100002", "INV-130302")
    assert not one_edit_apart("INV-100002", "INV-100002")


def test_find_subsets_stops_at_limit():
    items = [("a", 1), ("b", 1), ("c", 1), ("d", 1)]
    assert len(find_subsets(items, 2, limit=2)) == 2
    assert find_subsets([("a", 5), ("b", 7)], 12) == [["a", "b"]]
    assert find_subsets([("a", 5)], 5) == []  # single-invoice matches are handled elsewhere


def test_decision_effects_never_over_apply():
    s = world()
    d = decide(pay(10_000_00, "INV-100001"), s)
    assert s.invoices["INV-100001"].open_cents == 0
    assert d.exception_kind == "overpayment"


def test_truncated_bank_name_prefers_the_longer_customer_it_was_cut_from():
    s = State()
    approved_customer(s, "c1", "Driftwood Energy Inc")
    approved_customer(s, "c2", "Driftwood Energy Solutions LLC")
    invoice(s, "INV-300001", "c2", 1_234_56)
    d = decide(pay(1_234_56, "", payer="DRIFTWOOD ENERGY S"), s)
    assert d.action == "applied"
    assert d.detail["payer_match"]["customer_id"] == "c2"


def test_truncated_suffix_is_ignored():
    s = State()
    approved_customer(s, "c1", "Acme Foods Group Inc")
    invoice(s, "INV-300001", "c1", 1_234_56)
    d = decide(pay(1_234_56, "", payer="ACME FOODS GROUP I"), s)
    assert d.detail["payer_match"] == {"customer_id": "c1", "score": 1.0}
