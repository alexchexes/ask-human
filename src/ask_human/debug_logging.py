"""Opt-in JSONL debug logging for Telegram timing diagnostics."""

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

TELEGRAM_DEBUG_LOG_ENV = "ASK_HUMAN_TELEGRAM_DEBUG_LOG"


class TelegramDebugLogger:
    """Write best-effort JSONL timing events without prompt text or credentials."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, log_path: Optional[str] = None) -> Optional["TelegramDebugLogger"]:
        resolved_log_path = resolve_telegram_debug_log_path(log_path)
        if resolved_log_path is None:
            return None
        return cls(resolved_log_path)

    def event(self, event: str, **fields: Any) -> None:
        """Append one diagnostic event, suppressing logging failures."""
        payload = {
            "ts": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "event": event,
        }
        payload.update({key: _json_safe(value) for key, value in fields.items()})

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            with self._lock:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            return


def resolve_telegram_debug_log_path(log_path: Optional[str] = None) -> Optional[Path]:
    """Resolve the configured Telegram debug log path from CLI or environment."""
    configured_path = log_path if log_path is not None else os.environ.get(TELEGRAM_DEBUG_LOG_ENV)
    if configured_path is None or not configured_path.strip():
        return None

    expanded_path = configured_path.replace("{cwd}", os.getcwd())
    expanded_path = os.path.expandvars(expanded_path)
    expanded_path = os.path.expanduser(expanded_path)
    return Path(expanded_path).resolve()


def duration_ms(started_at: float) -> int:
    """Return elapsed monotonic time as whole milliseconds."""
    return round((time.monotonic() - started_at) * 1000)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": str(value),
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)
