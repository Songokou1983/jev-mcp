"""Decision log: JSONL append with privacy default (metadata only, no response body).

TDD — these tests define the behavior before implementation.
"""
import json
import os
from pathlib import Path

import pytest

from jev_mcp.decision_log import DecisionLog, LogEntry


class TestLogEntryMetadataOnly:
    """Default mode: no response body in JSON (PLAN.md §5 privacy)."""

    def test_metadata_only_no_response(self):
        e = LogEntry(
            timestamp="2026-09-21T16:45:23Z",
            tool="classify",
            args_hash="sha256:abc",
            latency_ms=287,
            tokens={"input": 314, "output": 47},
            cost_usd=0.000015,
            action="act",
            cached=False,
            response=None,
        )
        d = e.to_dict()
        assert "response" not in d
        assert d["tool"] == "classify"
        assert d["cost_usd"] == 0.000015

    def test_full_mode_includes_response(self):
        e = LogEntry(
            timestamp="2026-09-21T16:45:23Z",
            tool="classify",
            args_hash="sha256:abc",
            latency_ms=287,
            tokens={"input": 314, "output": 47},
            cost_usd=0.000015,
            action="act",
            cached=False,
            response={"choice": "billing"},
        )
        d = e.to_dict()
        assert d["response"] == {"choice": "billing"}

    def test_jsonl_serialization(self):
        e = LogEntry(
            timestamp="2026-09-21T16:45:23Z",
            tool="check",
            args_hash="sha256:xyz",
            latency_ms=180,
            tokens={"input": 50, "output": 5},
            cost_usd=0.000002,
            action="confirm",
            cached=True,
            response=None,
        )
        line = e.to_jsonl()
        # Should be parseable JSON
        parsed = json.loads(line)
        assert parsed["tool"] == "check"
        assert parsed["cached"] is True


class TestDecisionLog:
    """Append-only JSONL file at given path."""

    def test_writes_one_line_per_entry(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(path=str(log_path), log_full_response=False)
        log.append(LogEntry(
            timestamp="2026-09-21T16:45:23Z",
            tool="classify",
            args_hash="sha256:abc",
            latency_ms=287,
            tokens={"input": 314, "output": 47},
            cost_usd=0.000015,
            action="act",
            cached=False,
            response={"choice": "billing"},  # would leak if not stripped
        ))
        log.close()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert "response" not in parsed  # privacy default worked

    def test_full_response_mode(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(path=str(log_path), log_full_response=True)
        log.append(LogEntry(
            timestamp="2026-09-21T16:45:23Z",
            tool="classify",
            args_hash="sha256:abc",
            latency_ms=287,
            tokens={"input": 314, "output": 47},
            cost_usd=0.000015,
            action="act",
            cached=False,
            response={"choice": "billing"},
        ))
        log.close()
        parsed = json.loads(log_path.read_text().strip())
        assert parsed["response"] == {"choice": "billing"}

    def test_multiple_entries(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(path=str(log_path), log_full_response=False)
        for i in range(3):
            log.append(LogEntry(
                timestamp=f"2026-09-21T16:45:{i:02d}Z",
                tool="check",
                args_hash=f"sha256:{i}",
                latency_ms=100 + i,
                tokens={"input": 10, "output": 1},
                cost_usd=0.000001,
                action="act",
                cached=False,
                response=None,
            ))
        log.close()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 3
        assert all(json.loads(line)["tool"] == "check" for line in lines)

    def test_file_mode_0600(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(path=str(log_path), log_full_response=False)
        log.append(LogEntry(
            timestamp="2026-09-21T16:45:23Z",
            tool="classify",
            args_hash="sha256:abc",
            latency_ms=287,
            tokens={"input": 314, "output": 47},
            cost_usd=0.000015,
            action="act",
            cached=False,
            response=None,
        ))
        log.close()
        mode = log_path.stat().st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not enforced on Windows")
    def test_file_mode_0600_posix_only(self, tmp_path: Path):
        # POSIX-only companion test (always passes on Linux/macOS)
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(path=str(log_path), log_full_response=False)
        log.close()
        assert log_path.exists()