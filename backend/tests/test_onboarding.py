from ledgerloop.agents.onboarding import OnboardingAgent
from ledgerloop.policy import DEFAULT_POLICY
from ledgerloop.state import State

from .helpers import NoTools, approved_customer, ev

agent = OnboardingAgent()


def apply(name="Brightline Foods LLC", **over):
    payload = {
        "customer_id": over.pop("customer_id", "c9"),
        "legal_name": name,
        "tax_id": "TX-555",
        "country": "US",
        "requested_limit_cents": 50_000_00,
        "annual_revenue_cents": 2_000_000_00,
    }
    payload.update(over)
    return ev("customer.applied", payload)


def decide(event, state=None):
    return agent.decide(event, state or State(), DEFAULT_POLICY, NoTools())


def test_clean_application_is_approved_with_requested_limit():
    d = decide(apply())
    assert d.action == "approved" and d.auto_resolved
    assert d.detail["granted_cents"] == 50_000_00


def test_limit_is_capped_by_revenue_policy():
    d = decide(apply(requested_limit_cents=250_000_00))  # policy allows 10% of $2M = $200k
    assert d.action == "approved"
    assert d.detail["granted_cents"] == 200_000_00
    assert "limit_capped_by_policy" in d.reasons


def test_request_far_above_policy_goes_to_review():
    d = decide(apply(requested_limit_cents=900_000_00))
    assert not d.auto_resolved and d.exception_kind == "limit_far_above_policy"


def test_missing_tax_id_goes_to_review():
    d = decide(apply(tax_id=""))
    assert d.exception_kind == "missing_tax_id"


def test_exact_sanctions_match_is_auto_rejected():
    d = decide(apply(name="Redmarsh Logistics Group Ltd."))
    assert d.action == "rejected" and d.auto_resolved
    assert d.reasons == ["sanctions_match"]


def test_sanctions_near_match_goes_to_review():
    d = decide(apply(name="Redmarsh Logistic Grp"))
    assert d.exception_kind == "sanctions_near_match"


def test_duplicate_tax_id_goes_to_review():
    s = State()
    approved_customer(s, cid="c1", name="Something Else Inc", tax_id="TX-555")
    d = decide(apply(), s)
    assert d.exception_kind == "duplicate_tax_id"
    assert d.detail["existing_customer_id"] == "c1"


def test_near_identical_name_in_same_country_goes_to_review():
    s = State()
    approved_customer(s, cid="c1", name="Brightline Foods Inc")
    d = decide(apply(name="Brightline Food LLC", tax_id="TX-999"), s)
    assert d.exception_kind == "possible_duplicate"


def test_similar_name_in_other_country_is_not_a_duplicate():
    s = State()
    approved_customer(s, cid="c1", name="Brightline Foods Inc", country="CA")
    d = decide(apply(tax_id="TX-999"), s)
    assert d.action == "approved"
