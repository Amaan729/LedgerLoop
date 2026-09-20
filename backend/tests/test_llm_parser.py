from types import SimpleNamespace

from ledgerloop.tools.llm_remittance import LLMRemittanceParser
from ledgerloop.tools.recorder import RecordingTools


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=[SimpleNamespace(text=self.reply)])


def test_regex_hits_never_call_the_model():
    client = FakeClient('{"refs": ["INV-999999"]}')
    parser = LLMRemittanceParser(client=client)
    assert parser({"memo": "INV-104233"})["refs"] == ["INV-104233"]
    assert client.calls == 0


def test_model_is_asked_only_when_regex_finds_nothing():
    client = FakeClient('Sure! {"refs": ["inv-104233", "garbage", "INV-104233"]}')
    parser = LLMRemittanceParser(client=client)
    out = parser({"memo": "second half of the march invoice one-oh-four-two-three-three"})
    assert out["refs"] == ["INV-104233"]
    assert out["parser"].startswith("llm:")
    assert client.calls == 1


def test_model_errors_fall_back_to_regex_result():
    parser = LLMRemittanceParser(client=FakeClient(TimeoutError()))
    out = parser({"memo": "no idea"})
    assert out["refs"] == [] and out["llm_error"] == "TimeoutError"


def test_replay_does_not_call_the_model_again():
    client = FakeClient('{"refs": ["INV-104233"]}')
    live = RecordingTools({"remittance_parser": LLMRemittanceParser(client=client)})
    live.begin("evt-1")
    first = live.call("remittance_parser", {"memo": "the march one"})
    rows = live.drain()

    client.reply = '{"refs": ["INV-555555"]}'  # the model would answer differently today
    replay = RecordingTools({"remittance_parser": LLMRemittanceParser(client=client)}, mode="replay",
                            recorded={r["call_key"]: r["output"] for r in rows})
    replay.begin("evt-1")
    assert replay.call("remittance_parser", {"memo": "the march one"}) == first
    assert client.calls == 1
