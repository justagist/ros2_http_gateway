"""Audit logger tests. No ROS needed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import orjson
import pytest

from ros2_http_gateway.audit import AuditLogger, Decision
from ros2_http_gateway.config import AuditConfig


def read_lines(path: Path) -> list[dict[str, Any]]:
    text = path.read_bytes().decode()
    return [orjson.loads(line) for line in text.splitlines() if line.strip()]


def allow(logger: AuditLogger, **overrides: Any) -> None:
    logger.record(
        **{
            "method": "GET",
            "path": "/api/v1/mission/status",
            "decision": Decision.ALLOW,
            "status": 200,
            "latency_ms": 1.2345,
            "user": "hmi",
            "roles": frozenset({"operator", "viewer"}),
            **overrides,
        }
    )


@pytest.fixture
async def logger_and_path(tmp_path: Path) -> AsyncIterator[tuple[AuditLogger, Path]]:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(AuditConfig(sink="file", path=path))
    await logger.start()
    try:
        yield logger, path
    finally:
        await logger.stop()


async def test_record_is_written_as_one_json_line(
    logger_and_path: tuple[AuditLogger, Path],
) -> None:
    logger, path = logger_and_path
    allow(logger)
    await logger.stop()

    (entry,) = read_lines(path)
    assert entry["user"] == "hmi"
    assert entry["roles"] == ["operator", "viewer"]
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/v1/mission/status"
    assert entry["decision"] == "allow"
    assert entry["reason"] is None
    assert entry["status"] == 200
    assert entry["outcome"] == "ok"
    assert entry["latency_ms"] == 1.234  # rounded, so the log stays readable
    assert entry["ts"].endswith("Z")


async def test_a_denial_carries_its_reason(logger_and_path: tuple[AuditLogger, Path]) -> None:
    logger, path = logger_and_path
    logger.record(
        method="GET",
        path="/api/v1/mission/status",
        decision=Decision.DENY,
        reason="unknown bearer token",
        status=401,
        outcome="UNAUTHENTICATED",
        latency_ms=0.1,
    )
    await logger.stop()

    (entry,) = read_lines(path)
    assert entry["decision"] == "deny"
    assert entry["reason"] == "unknown bearer token"
    assert entry["user"] is None
    assert entry["roles"] == []


async def test_no_record_can_carry_a_token_or_a_body(
    logger_and_path: tuple[AuditLogger, Path],
) -> None:
    """Structural, not a matter of care at the call site: there are no fields for them."""
    logger, path = logger_and_path
    allow(logger)
    await logger.stop()

    (entry,) = read_lines(path)
    assert set(entry) == {
        "ts",
        "user",
        "roles",
        "method",
        "path",
        "decision",
        "reason",
        "status",
        "outcome",
        "latency_ms",
    }


async def test_recording_does_not_block_the_caller(
    logger_and_path: tuple[AuditLogger, Path],
) -> None:
    """record() is called from the request path, so it may never await or block."""
    logger, path = logger_and_path
    for index in range(500):
        allow(logger, status=200 + index % 3)
    # Nothing has been awaited yet, so the writer has not necessarily run at all.
    await logger.stop()
    assert len(read_lines(path)) == 500


async def test_many_records_are_batched_into_few_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of requests should cost one syscall, not one per record."""
    writes: list[int] = []
    real = AuditLogger._write

    def counting(self: AuditLogger, batch: Any) -> None:
        writes.append(len(batch))
        real(self, batch)

    monkeypatch.setattr(AuditLogger, "_write", counting)

    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(AuditConfig(sink="file", path=path))
    await logger.start()
    for _ in range(200):
        allow(logger)
    await logger.stop()

    assert sum(writes) == 200
    assert len(writes) < 200, f"expected batching, got {len(writes)} writes"


async def test_a_full_queue_drops_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dropping is bounded and visible; a silent gap in an audit trail is worse."""
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(AuditConfig(sink="file", path=path), queue_size=5)
    await logger.start()
    with caplog.at_level("WARNING"):
        for _ in range(20):
            allow(logger)
    assert "audit queue full" in caplog.text

    # The count is carried on the next record that does get through, so the log shows
    # exactly where the hole is.
    await asyncio.sleep(0.05)
    allow(logger)
    await logger.stop()
    entries = read_lines(path)
    assert any("dropped" in entry for entry in entries)
    assert sum(entry.get("dropped", 0) for entry in entries) == 15


async def test_stderr_sink_writes_there(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    logger = AuditLogger(AuditConfig(sink="stderr", path=None))
    await logger.start()
    allow(logger)
    await logger.stop()
    assert b'"decision":"allow"' in capsysbinary.readouterr().err


async def test_stop_is_safe_without_start() -> None:
    await AuditLogger(AuditConfig(sink="stderr", path=None)).stop()


async def test_file_sink_creates_missing_directories(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "audit.jsonl"
    logger = AuditLogger(AuditConfig(sink="file", path=path))
    await logger.start()
    allow(logger)
    await logger.stop()
    assert read_lines(path)


async def test_file_sink_appends_rather_than_truncating(tmp_path: Path) -> None:
    """Restarting the gateway must not discard the previous audit trail."""
    path = tmp_path / "audit.jsonl"
    for _ in range(2):
        logger = AuditLogger(AuditConfig(sink="file", path=path))
        await logger.start()
        allow(logger)
        await logger.stop()
    assert len(read_lines(path)) == 2
