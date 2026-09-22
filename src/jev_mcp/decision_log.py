"""Decision log: append-only JSONL with privacy default (metadata only).

PLAN.md §5: state may contain credentials/PII → default to no response body.
Set log_full_response=True to log full responses (debug mode).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class LogEntry:
    """One decision log line. response=None means metadata-only mode."""

    timestamp: str
    tool: str
    args_hash: str
    latency_ms: int
    tokens: dict[str, int]
    cost_usd: float
    action: str
    cached: bool
    response: Any = None

    @classmethod
    def now(
        cls,
        tool: str,
        args_hash: str,
        latency_ms: int,
        tokens: dict[str, int],
        cost_usd: float,
        action: str,
        cached: bool,
        response: Any = None,
    ) -> "LogEntry":
        return cls(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            tool=tool,
            args_hash=args_hash,
            latency_ms=latency_ms,
            tokens=tokens,
            cost_usd=cost_usd,
            action=action,
            cached=cached,
            response=response,
        )

    def to_dict(self) -> dict[str, Any]:
        # Privacy default: drop response if None
        d = {
            "timestamp": self.timestamp,
            "tool": self.tool,
            "args_hash": self.args_hash,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "action": self.action,
            "cached": self.cached,
        }
        if self.response is not None:
            d["response"] = self.response
        return d

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


class DecisionLog:
    """Append-only JSONL writer. File mode is 0600 (privacy)."""

    def __init__(self, path: str, log_full_response: bool):
        self.path = Path(path)
        self.log_full_response = log_full_response
        self.parent = self.path.parent
        self._fh = None
        self._ensure_open()

    def _ensure_open(self) -> None:
        if self._fh is not None:
            return
        self.parent.mkdir(parents=True, exist_ok=True)
        # Open in append mode; create if missing
        self._fh = open(self.path, "a", encoding="utf-8")
        # Enforce 0600 on first creation
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass  # some FS may not support chmod; best effort

    def append(self, entry: LogEntry) -> None:
        """Write one entry. If log_full_response=False, strip response body."""
        if not self.log_full_response:
            # Defensive copy — don't mutate caller's entry
            entry = LogEntry(
                timestamp=entry.timestamp,
                tool=entry.tool,
                args_hash=entry.args_hash,
                latency_ms=entry.latency_ms,
                tokens=entry.tokens,
                cost_usd=entry.cost_usd,
                action=entry.action,
                cached=entry.cached,
                response=None,
            )
        self._ensure_open()
        self._fh.write(entry.to_jsonl() + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass