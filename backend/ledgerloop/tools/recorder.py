"""Record/replay for anything an agent can't compute deterministically itself.

In live mode a tool call runs for real and its output is saved next to the
event that caused it. In replay mode the saved output is returned instead, and
a call with no recording is an error rather than a silent live call. That is
what lets a decision made with an LLM in the loop be reproduced bit-for-bit
months later, after the model or the parser code has changed.
"""

from __future__ import annotations

from typing import Any, Callable

from ..events import derive_id

ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


class MissingRecording(RuntimeError):
    pass


class RecordingTools:
    def __init__(
        self,
        registry: dict[str, ToolFn],
        mode: str = "live",
        recorded: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if mode not in ("live", "replay"):
            raise ValueError(mode)
        self.registry = registry
        self.mode = mode
        self.recorded = recorded or {}
        self.pending: list[dict[str, Any]] = []
        self._event_id: str | None = None

    def begin(self, event_id: str) -> None:
        self._event_id = event_id

    def call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._event_id is None:
            raise RuntimeError("tool call outside of an event")
        key = derive_id("call", self._event_id, tool, payload)
        if self.mode == "replay":
            try:
                return self.recorded[key]
            except KeyError:
                raise MissingRecording(f"no recording for {tool} on {self._event_id}") from None
        output = self.registry[tool](payload)
        self.pending.append(
            {"call_key": key, "event_id": self._event_id, "tool": tool, "input": payload, "output": output}
        )
        return output

    def drain(self) -> list[dict[str, Any]]:
        rows, self.pending = self.pending, []
        return rows
