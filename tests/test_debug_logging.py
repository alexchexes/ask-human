"""Tests for opt-in Telegram debug logging."""

import json

from ask_human.debug_logging import (
    TELEGRAM_DEBUG_LOG_ENV,
    TelegramDebugLogger,
    resolve_telegram_debug_log_path,
)


def test_telegram_debug_logger_writes_jsonl_without_prompt_content(monkeypatch, tmp_path):
    """Record structured diagnostic events to the configured JSONL path."""
    monkeypatch.chdir(tmp_path)
    logger = TelegramDebugLogger.from_config("{cwd}/logs/telegram-debug.jsonl")

    assert logger is not None
    logger.event(
        "sample_event",
        prompt_id="QTEST-1234",
        path=tmp_path / "downloads",
        error=ValueError("connection timed out"),
    )

    lines = (tmp_path / "logs" / "telegram-debug.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "sample_event"
    assert payload["prompt_id"] == "QTEST-1234"
    assert payload["path"] == str(tmp_path / "downloads")
    assert payload["error"] == {
        "type": "ValueError",
        "message": "connection timed out",
    }
    assert "pid" in payload
    assert "ts" in payload


def test_resolve_telegram_debug_log_path_uses_environment(monkeypatch, tmp_path):
    """Allow the debug log path to be configured with an environment variable."""
    log_path = tmp_path / "telegram-debug.jsonl"
    monkeypatch.setenv(TELEGRAM_DEBUG_LOG_ENV, str(log_path))

    assert resolve_telegram_debug_log_path() == log_path.resolve()
    assert resolve_telegram_debug_log_path("  ") is None
