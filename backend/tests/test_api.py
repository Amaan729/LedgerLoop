import pytest
from fastapi.testclient import TestClient

from ledgerloop.api.main import create_app

from .test_engine import story


@pytest.fixture
def client(engine):
    app = create_app(db_engine=engine, start_consumer=False)
    with TestClient(app) as c:
        yield c


def post_story(client):
    r = client.post("/events", json=[e.to_dict() for e in story()])
    assert r.status_code == 200, r.text
    return r.json()


def test_post_events_and_metrics(client):
    body = post_story(client)
    assert body["new"] == 5
    m = client.get("/metrics").json()
    assert m["events_total"] == 5
    assert m["decisions_total"] == 4
    assert m["auto_resolution_rate"] == 0.75
    assert m["exceptions_open"] == 1


def test_resolve_exception_flow(client):
    post_story(client)
    [exc] = client.get("/exceptions").json()
    assert exc["kind"] == "over_limit"
    detail = client.get(f"/exceptions/{exc['exception_id']}").json()
    assert detail["subject"]["order_id"] == "o2"

    r = client.post(f"/exceptions/{exc['exception_id']}/resolve", json={"resolution": {"action": "release"}, "resolved_by": "amaan"})
    assert r.json()["exception"]["status"] == "resolved"
    assert client.get("/exceptions").json() == []

    # posting the same resolution again is idempotent
    again = client.post(f"/exceptions/{exc['exception_id']}/resolve", json={"resolution": {"action": "release"}, "resolved_by": "amaan"})
    assert again.json()["batch"]["duplicate"] == 1


def test_audit_and_replay_endpoints(client):
    post_story(client)
    assert client.get("/audit/verify").json()["ok"]
    assert len(client.get("/audit?limit=2").json()) == 2
    v = client.post("/replay/verify").json()
    assert v["ok"] and v["matches_live_state"]
    w = client.post("/replay/what-if", json={"policy": "v2-strict"}).json()
    assert w["policy"] == "v2-strict"
    assert client.post("/replay/what-if", json={"policy": "nope"}).status_code == 404


def test_malformed_events_are_rejected(client):
    assert client.post("/events", json=[{"type": "order.placed"}]).status_code == 422
