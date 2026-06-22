"""Test MCP server functionality."""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_mcp_server_exists():
    """Test that MCP server can be initialized."""
    from ask_human.server import MCP_SERVER_INSTRUCTIONS, mcp

    assert mcp is not None
    assert mcp.name == "ask-human"
    assert mcp._mcp_server.instructions == MCP_SERVER_INSTRUCTIONS
    assert "may remain pending for hours" in MCP_SERVER_INSTRUCTIONS
    assert "intermediate statuses or yields as non-terminal" in MCP_SERVER_INSTRUCTIONS


def test_ask_human_tool():
    """Test that the main tool function exists."""
    from ask_human import server
    from ask_human.server import ask_human

    assert callable(ask_human)
    assert set(server.mcp._tool_manager._tools) == {"ask_human"}
    tool = server.mcp._tool_manager._tools["ask_human"]
    description = tool.description
    assert "Use normal multiline Markdown" in description
    assert tool.parameters["properties"]["question"]["type"] == "string"
    assert tool.parameters["properties"]["context"]["type"] == "string"
