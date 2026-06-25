"""Tests for dialog timeout behavior."""

import asyncio
import os
import subprocess
import sys

from ask_human import server
from ask_human.dialogs import wrap_text_by_pixel_width
from ask_human.server import (
    DEFAULT_DIALOG_TIMEOUT_SECONDS,
    GUIDialogHandler,
)


class FakeRoot:
    """Minimal Tk root stand-in for timeout scheduling tests."""

    def __init__(self):
        self.after_calls = []
        self.after_cancel_calls = []
        self.destroy_calls = 0

    def after(self, delay_ms, callback):
        self.after_calls.append((delay_ms, callback))
        return "timeout-id"

    def after_cancel(self, timeout_id):
        self.after_cancel_calls.append(timeout_id)

    def destroy(self):
        self.destroy_calls += 1


class FakeSimpleDialog:
    """Minimal simpledialog stand-in that records askstring arguments."""

    def __init__(self):
        self.calls = []

    def askstring(self, title, question, parent=None):
        self.calls.append((title, question, parent))
        return "typed answer"


def test_tool_uses_configured_dialog_timeout(monkeypatch):
    """Use the configured dialog timeout instead of a hardcoded 90 seconds."""

    class StubDialogHandler:
        def __init__(self):
            self.timeout = None

        async def get_user_input(self, question, timeout):
            self.timeout = timeout
            return "ok"

    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 1200)

    result = asyncio.run(server.ask_human("Question?"))

    assert result == "✅ User response: ok"
    assert stub.timeout == 1200


def test_tool_warns_for_empty_dialog_response(monkeypatch):
    """Do not report an accidental empty dialog answer as a successful response."""

    class StubDialogHandler:
        async def get_user_input(self, question, timeout):
            return ""

    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())

    result = asyncio.run(server.ask_human("Question?"))

    assert result.startswith("⚠️ Empty response received.")


def test_tool_warns_for_whitespace_only_dialog_response(monkeypatch):
    """Treat whitespace-only dialog answers the same as empty answers."""

    class StubDialogHandler:
        async def get_user_input(self, question, timeout):
            return "   "

    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())

    result = asyncio.run(server.ask_human("Question?"))

    assert result.startswith("⚠️ Empty response received.")


def test_tool_warns_when_dialog_returns_no_response(monkeypatch):
    """Report a closed, cancelled, or timed-out dialog as no response."""

    class StubDialogHandler:
        async def get_user_input(self, question, timeout):
            return None

    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())
    monkeypatch.setattr(server, "dialog_timeout_seconds", 120)

    result = asyncio.run(server.ask_human("Question?"))

    assert result.startswith("⚠️ Timeout: No response received within 2 minutes.")
    assert "timed out or been cancelled" in result


def test_tool_allows_prompt_at_combined_length_limit(monkeypatch):
    """Allow question and context to use the full combined prompt budget."""

    class StubDialogHandler:
        async def get_user_input(self, question, timeout):
            return "ok"

    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())

    result = asyncio.run(server.ask_human("Q" * 5000, "C" * 3000))

    assert result == "✅ User response: ok"


def test_tool_rejects_prompt_over_combined_length_limit():
    """Reject prompts that exceed the shared question/context budget."""
    result = asyncio.run(server.ask_human("Q" * 5000, "C" * 3001))

    assert result == (
        "❌ Error: prompt is too long "
        "(max 8000 characters total across question and context). Please shorten it."
    )


def test_windows_string_dialog_schedules_timeout():
    """Schedule timeout inside Tk so Windows askstring is no longer unbounded."""
    handler = GUIDialogHandler()
    root = FakeRoot()
    simpledialog = FakeSimpleDialog()

    result = handler._ask_windows_string(
        root,
        simpledialog,
        "Title",
        "Question?",
        1200,
    )

    assert result == "typed answer"
    assert root.after_calls == [(1200 * 1000, root.destroy)]
    assert root.after_cancel_calls == ["timeout-id"]
    assert simpledialog.calls == [("Title", "Question?", root)]


def test_windows_prompt_wrapping_hard_breaks_long_tokens():
    """Wrap Windows dialog prompts by measured width and hard-break no-space text."""

    def measure_text(text):
        return len(text) * 10

    wrapped = wrap_text_by_pixel_width(
        "alpha beta\n\nabcdefghij",
        measure_text,
        50,
    )

    assert wrapped == "alpha\nbeta\n\nabcde\nfghij"


def test_help_mentions_timeout_option():
    """Expose the dialog timeout option in CLI help."""
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    env = os.environ.copy()
    src_dir = os.path.join(repo_root, "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        src_dir if not existing_pythonpath else src_dir + os.pathsep + existing_pythonpath
    )

    result = subprocess.run(
        [sys.executable, "-m", "ask_human", "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
    )

    assert result.returncode == 0
    assert "--timeout-seconds" in result.stdout
    assert str(DEFAULT_DIALOG_TIMEOUT_SECONDS) in result.stdout
