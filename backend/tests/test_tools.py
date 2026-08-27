import pytest

from ledgerloop.tools.recorder import MissingRecording, RecordingTools
from ledgerloop.tools.remittance import extract_invoice_refs


@pytest.mark.parametrize(
    "memo,expected",
    [
        ("INV-104233", ["INV-104233"]),
        ("Payment for INV-104233, INV-104240", ["INV-104233", "INV-104240"]),
        ("inv 104233", ["INV-104233"]),
        ("Invoice #104233 thanks", ["INV-104233"]),
        ("invoice no. 104233", ["INV-104233"]),
        ("PMT INV1O4233 & INV104240", ["INV-104233", "INV-104240"]),
        ("ACH CREDIT 104233", ["INV-104233"]),
        ("wire ref 20260910 amt 1,042.33", []),
        ("", []),
        ("INV-104233 INV-104233", ["INV-104233"]),
    ],
)
def test_extract_invoice_refs(memo, expected):
    assert extract_invoice_refs(memo) == expected


def test_live_mode_records_and_replay_serves_recording():
    calls = []

    def tool(payload):
        calls.append(payload)
        return {"n": len(calls)}

    live = RecordingTools({"t": tool})
    live.begin("e1")
    assert live.call("t", {"x": 1}) == {"n": 1}
    rows = live.drain()
    assert len(rows) == 1

    replay = RecordingTools({"t": tool}, mode="replay", recorded={rows[0]["call_key"]: rows[0]["output"]})
    replay.begin("e1")
    assert replay.call("t", {"x": 1}) == {"n": 1}
    assert len(calls) == 1  # the real tool was not called again


def test_replay_without_recording_fails_loudly():
    replay = RecordingTools({"t": lambda p: {}}, mode="replay")
    replay.begin("e1")
    with pytest.raises(MissingRecording):
        replay.call("t", {"x": 1})
