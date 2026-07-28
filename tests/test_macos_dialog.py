"""Tests for macOS dialog behavior."""

import asyncio
from unittest.mock import patch

from ask_human.server import GUIDialogHandler


class FakeProcess:
    """Minimal async subprocess stub for dialog tests."""

    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        """Return the configured AppleScript response."""
        return (self.stdout, b"")


def test_macos_dialog_uses_configured_title():
    """Build the macOS dialog script without missing imports."""
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess(returncode=1)

    handler = GUIDialogHandler("Custom Title")

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result is None
    assert captured["args"][0] == "osascript"
    assert captured["args"][1] == "-e"
    script = captured["args"][2]
    assert 'with title "Custom Title"' in script
    assert "giving up after 10" in script
    assert "if gave up of dialog_result then error number -128" in script
    assert "return text returned of dialog_result" in script


def test_macos_dialog_preserves_commas_in_response():
    """Return the entered text directly instead of parsing AppleScript's result record."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess(stdout=b"ok, thanks, works\n")

    handler = GUIDialogHandler()

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result == "ok, thanks, works"


def test_macos_dialog_preserves_empty_ok_response():
    """Keep an empty submitted answer distinct from cancellation or timeout."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess(stdout=b"\n")

    handler = GUIDialogHandler()

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result == ""
