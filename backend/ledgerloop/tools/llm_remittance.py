"""Optional LLM fallback for remittance memos the regex can't read.

The regex handles the common formats. Some memos reference invoices in ways
no pattern will catch ("second half of the march invoice, 104233 short 40").
For those, and only those, we ask a model. Two things keep this safe:

  * The model only proposes candidate refs. The cash agent still checks that
    each ref is a real invoice for this payer and that the amount fits.
  * The call goes through RecordingTools, so its output is stored with the
    event. Replay reuses the stored answer instead of asking the model again,
    which is what keeps decisions reproducible even though the model isn't.

Off by default. Set LEDGERLOOP_LLM_PARSER=1 and ANTHROPIC_API_KEY to enable.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .remittance import PARSER_VERSION, regex_remittance_parser

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_REF = re.compile(r"^INV-\d{6}$")

PROMPT = """You extract invoice numbers from bank remittance memos.
Invoice numbers have the form INV-123456 (six digits). In memos they may be written
as "inv 123456", "Invoice #123456", just "123456", or with the letters O or I typed
instead of the digits 0 or 1. Only return numbers the memo actually references.

Reply with JSON only, like {{"refs": ["INV-123456"]}}. Use {{"refs": []}} if there are none.

<memo>{memo}</memo>"""


class LLMRemittanceParser:
    def __init__(self, client: Any = None, model: str = DEFAULT_MODEL,
                 fallback: Callable[[dict[str, Any]], dict[str, Any]] = regex_remittance_parser) -> None:
        if client is None:
            import anthropic  # optional dependency: pip install .[llm]

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.fallback = fallback

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        base = self.fallback(payload)
        memo = (payload.get("memo") or "").strip()
        if base["refs"] or not memo:
            return base  # regex was enough, or there's nothing to read
        try:
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=200,
                messages=[{"role": "user", "content": PROMPT.format(memo=memo[:500])}],
            )
            text = "".join(getattr(block, "text", "") for block in msg.content)
            data = json.loads(text[text.index("{") : text.rindex("}") + 1])
            refs = []
            for r in data.get("refs", []):
                r = str(r).strip().upper()
                if _REF.match(r) and r not in refs:
                    refs.append(r)
            return {"refs": refs, "parser": f"llm:{self.model}"}
        except Exception as e:  # noqa: BLE001 - the model is a best-effort fallback
            return {**base, "parser": PARSER_VERSION, "llm_error": type(e).__name__}
