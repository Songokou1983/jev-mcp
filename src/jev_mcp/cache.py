"""Cache: sha256 key derivation + LRU with hit/miss tracking.

Pure logic — no I/O.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any


def compute_cache_key(
    state: Any,
    questions: dict[str, Any],
    model_version: str,
) -> str:
    """sha256 hex of canonical_json(state) + canonical_json(questions) + model_version.

    canonical JSON = sorted keys + ensure_ascii=False (preserves unicode).
    """
    payload = (
        json.dumps(state, sort_keys=True, ensure_ascii=False)
        + "|"
        + json.dumps(questions, sort_keys=True, ensure_ascii=False)
        + "|"
        + model_version
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LRUCache:
    """Bounded LRU with hit/miss tracking."""

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        self._data: OrderedDict[str, Any] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        if key not in self._data:
            self.misses += 1
            return None
        # Promote to most-recent
        self._data.move_to_end(key)
        self.hits += 1
        return self._data[key]

    def set(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self._capacity:
            self._data.popitem(last=False)  # evict oldest

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    @property
    def size(self) -> int:
        return len(self._data)