"""Tests for dialog title configuration."""
import os
import subprocess
import sys

from ask_human_for_context_mcp.server import (
    DEFAULT_DIALOG_TITLE,
    DIALOG_TITLE_ENV_VAR,
    GUIDialogHandler,
    resolve_dialog_title,
)


def test_resolve_dialog_title_defaults(monkeypatch):
    """Use the built-in title when no override is present."""
    monkeypatch.delenv(DIALOG_TITLE_ENV_VAR, raising=False)

    assert resolve_dialog_title() == DEFAULT_DIALOG_TITLE


def test_resolve_dialog_title_uses_env(monkeypatch):
    """Use the environment variable when set."""
    monkeypatch.setenv(DIALOG_TITLE_ENV_VAR, "My Persistent Title")

    assert resolve_dialog_title() == "My Persistent Title"


def test_resolve_dialog_title_prefers_explicit_value(monkeypatch):
    """Prefer the explicit startup option over the environment variable."""
    monkeypatch.setenv(DIALOG_TITLE_ENV_VAR, "Env Title")

    assert resolve_dialog_title("CLI Title") == "CLI Title"


def test_handler_uses_resolved_dialog_title(monkeypatch):
    """Initialize handlers with the resolved title."""
    monkeypatch.setenv(DIALOG_TITLE_ENV_VAR, "Env Title")

    handler = GUIDialogHandler()

    assert handler.dialog_title == "Env Title"


def test_help_mentions_dialog_title_option():
    """Expose the persistent title option in CLI help."""
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    env = os.environ.copy()
    src_dir = os.path.join(repo_root, "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        src_dir if not existing_pythonpath else src_dir + os.pathsep + existing_pythonpath
    )

    result = subprocess.run(
        [sys.executable, "-m", "ask_human_for_context_mcp", "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
    )

    assert result.returncode == 0
    assert "--dialog-title" in result.stdout
