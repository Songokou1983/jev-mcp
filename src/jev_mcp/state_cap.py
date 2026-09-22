"""State size cap: refuse if serialized state exceeds N characters.

CJK caveat: English ~4 chars/token, Chinese ~1 char/token. 32k Chinese chars ≈ 30k tokens,
which may push against Jev's state+questions input limit. Phase 2 to measure real tickets.
For now, the cap is in characters; server-side pre-check before hitting API.
"""
from __future__ import annotations

import json
from typing import Any


def serialize_state(state: Any) -> str:
    """String passes through; dict → canonical JSON (sorted keys, no ASCII escape)."""
    if isinstance(state, str):
        return state
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


class StateCapExceeded(Exception):
    """Raised when serialized state exceeds the configured cap."""

    def __init__(self, size: int, cap: int):
        self.size = size
        self.cap = cap
        super().__init__(f"state size {size} chars exceeds cap {cap} chars")


def check_state_cap(state: Any, cap: int) -> None:
    """Raise StateCapExceeded if serialized state > cap characters."""
    size = len(serialize_state(state))
    if size > cap:
        raise StateCapExceeded(size=size, cap=cap)