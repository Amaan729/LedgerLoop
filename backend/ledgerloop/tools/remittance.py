"""Pull invoice references out of free-text remittance memos.

Bank memos are messy: "INV-104233", "inv 104233", "Invoice #104233",
"PMT INV1O4233 & INV104240" (letter O instead of zero). This parser only
extracts candidates. Whether a candidate is a real, open invoice for this payer
is the cash agent's job.
"""

from __future__ import annotations

import re
from typing import Any

PARSER_VERSION = "regex-2"

_PREFIXED = re.compile(r"(?i)\binv(?:oice)?[\s#:.\-]*(?:no\.?\s*)?([0-9OoIl]{6})(?![0-9A-Za-z])")
_BARE = re.compile(r"(?<![\w\-])(\d{6})(?![\w])")
_OCR = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})


def extract_invoice_refs(memo: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for m in _PREFIXED.finditer(memo):
        digits = m.group(1).translate(_OCR)
        if digits.isdigit():
            found.append((m.start(1), digits))
    for m in _BARE.finditer(memo):
        found.append((m.start(1), m.group(1)))
    refs: list[str] = []
    for _, digits in sorted(found):
        ref = f"INV-{digits}"
        if ref not in refs:
            refs.append(ref)
    return refs


def regex_remittance_parser(payload: dict[str, Any]) -> dict[str, Any]:
    return {"refs": extract_invoice_refs(payload.get("memo") or ""), "parser": PARSER_VERSION}
