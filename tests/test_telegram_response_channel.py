"""Tests for Telegram response-channel support."""

import asyncio
import datetime as dt

import pytest

from ask_human import server
from ask_human.broker_state import TelegramBrokerIdentity
from ask_human.prompt_formatting import (
    TELEGRAM_MESSAGE_CHAR_LIMIT,
    build_dialog_telegram_notice,
    build_telegram_prompt_text,
    build_telegram_prompt_texts,
    render_markdown_to_telegram_html,
    telegram_html_to_plain_text,
)
from ask_human.telegram_client import TelegramBotApiError, TelegramPromptClient
from ask_human.telegram_models import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    TelegramConfig,
    TelegramPromptError,
    parse_telegram_target,
)


def test_parse_telegram_target_parses_token_and_chat_id():
    """Parse the single CLI argument into token and chat id."""
    config = parse_telegram_target("123456:ABCDEF -1009876543210")

    assert config == TelegramConfig(
        bot_token="123456:ABCDEF",
        chat_id="-1009876543210",
    )


def test_parse_telegram_target_rejects_invalid_shape():
    """Reject telegram target values without both pieces."""
    with pytest.raises(ValueError):
        parse_telegram_target("123456:ABCDEF")


def test_telegram_client_shutdown_waits_for_poller_to_stop(tmp_path):
    """Do not report broker shutdown complete while a Telegram long-poll is active."""

    async def run_shutdown_flow():
        client = TelegramPromptClient(
            TelegramConfig("123456:ABCDEF", "-1009876543210"),
            download_dir=tmp_path,
        )
        poll_started = asyncio.Event()
        release_poll = asyncio.Event()

        async def fake_bot_api_request(method, payload, timeout):
            if method == "sendMessage":
                return {"message_id": 101}
            if method == "getUpdates":
                if payload["timeout"] == DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS:
                    poll_started.set()
                    await release_poll.wait()
                return []
            raise AssertionError(f"Unexpected Telegram method: {method}")

        client._bot_api_request = fake_bot_api_request  # type: ignore[method-assign]

        prompt_task = asyncio.create_task(client.ask_question("Prompt text", 30, "QTEST-1234"))
        await poll_started.wait()

        shutdown_task = asyncio.create_task(client.shutdown(timeout=1))
        await asyncio.sleep(0)
        assert shutdown_task.done() is False

        release_poll.set()
        await shutdown_task

        with pytest.raises(TelegramPromptError, match="broker shutdown requested"):
            await prompt_task
        assert client._poller_task is None

    asyncio.run(run_shutdown_flow())


def test_telegram_client_retries_transient_get_updates_timeout(monkeypatch, tmp_path):
    """Keep a pending prompt alive when one Telegram long-poll read times out."""
    monkeypatch.setattr(TelegramPromptClient, "POLL_RETRY_DELAYS_SECONDS", (0.0,))

    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    poll_calls = 0
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        nonlocal poll_calls
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            poll_calls += 1
            if poll_calls == 1:
                raise TelegramBotApiError(
                    "Telegram getUpdates request failed: The read operation timed out",
                    method="getUpdates",
                    transport_error=True,
                )
            if poll_calls == 2:
                return [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 210,
                            "chat": {"id": -1009876543210},
                            "reply_to_message": {"message_id": 101},
                            "text": "proper reply",
                        },
                    }
                ]
            return []
        raise AssertionError(f"Unexpected Telegram method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "proper reply"
    assert poll_calls >= 2
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_stops_after_poll_retry_budget(monkeypatch, tmp_path):
    """Surface a real polling outage instead of retrying until the prompt timeout."""
    monkeypatch.setattr(TelegramPromptClient, "POLL_RETRY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr(TelegramPromptClient, "POLL_RETRY_MAX_ELAPSED_SECONDS", 0.0)

    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    poll_calls = 0

    async def fake_bot_api_request(method, payload, timeout):
        nonlocal poll_calls
        if method == "sendMessage":
            return {"message_id": 101}
        if method == "getUpdates":
            poll_calls += 1
            raise TelegramBotApiError(
                "Telegram getUpdates request failed: The read operation timed out",
                method="getUpdates",
                transport_error=True,
            )
        raise AssertionError(f"Unexpected Telegram method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    with pytest.raises(
        TelegramPromptError,
        match="Telegram polling failed: Telegram getUpdates request failed",
    ):
        asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))
    assert poll_calls >= 2


def test_telegram_client_does_not_retry_non_retryable_get_updates_error(
    monkeypatch,
    tmp_path,
):
    """Abort promptly for polling errors such as conflicts or bad configuration."""
    monkeypatch.setattr(TelegramPromptClient, "POLL_RETRY_DELAYS_SECONDS", (0.0,))

    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    poll_calls = 0

    async def fake_bot_api_request(method, payload, timeout):
        nonlocal poll_calls
        if method == "sendMessage":
            return {"message_id": 101}
        if method == "getUpdates":
            poll_calls += 1
            raise TelegramBotApiError(
                "Telegram getUpdates failed with HTTP 409: conflict",
                method="getUpdates",
                http_status=409,
            )
        raise AssertionError(f"Unexpected Telegram method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    with pytest.raises(
        TelegramPromptError,
        match="Telegram polling failed: Telegram getUpdates failed with HTTP 409",
    ):
        asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))
    assert poll_calls == 1


def test_build_dialog_telegram_notice_is_platform_specific():
    """Use the Windows stale-dialog warning only on Windows."""
    assert build_dialog_telegram_notice("Linux") == "📨 Also sent to Telegram."
    assert "will stay open" in build_dialog_telegram_notice("Windows")


def test_build_telegram_prompt_text_adds_prompt_id_and_reply_instruction():
    """Format Telegram prompts with compact metadata and reply guidance."""
    prompt_text = build_telegram_prompt_text(
        "Prompt text with **markdown**.",
        "Context text with `code` and <literal angle brackets>.",
        prompt_id="QTEST-1234",
        timeout_seconds=300,
        include_timing_info=True,
        broker_label="Alex Laptop",
        broker_id="abcd1234",
    )

    assert "<b>📋 Context:</b>" in prompt_text
    assert "Context text with <code>code</code> and &lt;literal angle brackets&gt;." in prompt_text
    assert "<b>❓ Question:</b>" in prompt_text
    assert "Prompt text with <b>markdown</b>." in prompt_text
    assert "<blockquote expandable>" in prompt_text
    assert "Prompt ID: QTEST-1234" in prompt_text
    assert "Broker: Alex Laptop [abcd1234]" in prompt_text
    assert '↩️ Use "Reply" on this message to answer.' in prompt_text


def test_build_telegram_prompt_texts_keeps_short_prompt_single_message():
    """Keep existing Telegram prompt shape when it fits in one message."""
    prompt_kwargs = {
        "prompt_id": "QTEST-1234",
        "timeout_seconds": 300,
        "include_timing_info": False,
        "broker_label": "Alex Laptop",
        "broker_id": "abcd1234",
    }

    assert build_telegram_prompt_texts("Short question?", "Short context.", **prompt_kwargs) == [
        build_telegram_prompt_text("Short question?", "Short context.", **prompt_kwargs)
    ]


def test_build_telegram_prompt_texts_splits_long_prompt_with_reply_target():
    """Split long Telegram prompts and leave the final message as the reply target."""
    long_context = "\n\n".join(f"Context paragraph {index}: " + "word " * 40 for index in range(35))
    long_question = "Question details: " + "word " * 450

    prompt_texts = build_telegram_prompt_texts(
        long_question,
        long_context,
        prompt_id="QTEST-1234",
        timeout_seconds=300,
        include_timing_info=True,
        broker_label="Alex Laptop",
        broker_id="abcd1234",
    )

    assert len(prompt_texts) > 1
    assert prompt_texts[0].startswith("<b>📋 Context (1/")
    assert any(message.startswith("<b>❓ Question") for message in prompt_texts)
    assert "Prompt ID: QTEST-1234" in prompt_texts[-1]
    assert "Broker: Alex Laptop [abcd1234]" in prompt_texts[-1]
    assert '↩️ Use "Reply" on this message to answer.' in prompt_texts[-1]
    assert all(
        len(telegram_html_to_plain_text(message)) <= TELEGRAM_MESSAGE_CHAR_LIMIT
        for message in prompt_texts
    )


def test_render_markdown_to_telegram_html_for_common_agent_markdown():
    """Render common Markdown to Telegram-supported HTML."""
    text = (
        "# Heading\n"
        "**Bold text**\n"
        "* asterisk bullet\n"
        "- dash bullet\n"
        "`**code stays literal**`\n\n"
        "> Quote stays visible and **bold**\n\n"
        "[OpenAI](https://openai.com/?a=1&b=2)\n\n"
        "```python\n"
        'print("<literal>")\n'
        "```"
    )

    assert render_markdown_to_telegram_html(text) == (
        "<b>Heading</b>\n"
        "<b>Bold text</b>\n"
        "- asterisk bullet\n"
        "- dash bullet\n"
        "<code>**code stays literal**</code>\n"
        "<blockquote>Quote stays visible and <b>bold</b>\n"
        "</blockquote>\n"
        '<a href="https://openai.com/?a=1&amp;b=2">OpenAI</a>\n'
        '<pre><code class="language-python">print("&lt;literal&gt;")\n'
        "</code></pre>"
    )


def test_telegram_html_to_plain_text_removes_markup_but_preserves_text():
    """Produce a readable fallback when Telegram rejects HTML parsing."""
    assert (
        telegram_html_to_plain_text(
            "<b>Heading</b>\n"
            "Text with <code>code</code> and &lt;literal&gt;.\n"
            "<blockquote expandable>Prompt ID: QTEST-1234</blockquote>"
        )
        == "Heading\nText with code and <literal>.\nPrompt ID: QTEST-1234"
    )


def test_telegram_client_sends_prompt_with_html_parse_mode(monkeypatch, tmp_path):
    """Ask Telegram to render the agent prompt as HTML."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    client.ATTACHMENT_REPLY_DEBOUNCE_SECONDS = 0.01
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            return {"message_id": 101}
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    message_id = asyncio.run(client._send_prompt("Prompt with <b>markdown</b>\n- item"))

    assert message_id == 101
    assert sent_messages == [
        {
            "chat_id": "-1009876543210",
            "text": "Prompt with <b>markdown</b>\n- item",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ]


def test_telegram_client_falls_back_to_plain_text_when_html_is_rejected(monkeypatch, tmp_path):
    """Do not lose the prompt when Telegram rejects HTML parsing."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if payload.get("parse_mode") == "HTML":
                raise TelegramPromptError(
                    "Telegram sendMessage failed: Bad Request: can't parse entities"
                )
            return {"message_id": 101}
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    message_id = asyncio.run(client._send_prompt("Prompt with <b>markdown</b> &amp; tags"))

    assert message_id == 101
    assert len(sent_messages) == 2
    assert sent_messages[0]["parse_mode"] == "HTML"
    assert sent_messages[0]["text"] == "Prompt with <b>markdown</b> &amp; tags"
    assert sent_messages[1] == {
        "chat_id": "-1009876543210",
        "text": "Prompt with markdown & tags",
        "disable_web_page_preview": True,
    }


def test_telegram_client_resolves_reply_to_sent_message(monkeypatch, tmp_path):
    """Resolve a Telegram reply that references the sent prompt message and ack it."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "telegram answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "telegram answer"
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_includes_selected_quote_context(monkeypatch, tmp_path):
    """Include Telegram's manual selected quote in the agent-facing response."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "quote": {
                        "text": "selected prompt phrase",
                        "position": 14,
                        "is_manual": True,
                    },
                    "text": "telegram answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == (
        "User quoted your prompt:\n"
        "selected prompt phrase\n"
        "\n"
        "User reply:\n"
        "telegram answer"
    )
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_accepts_reply_to_any_prompt_chunk(monkeypatch, tmp_path):
    """Register every multipart prompt message as a valid reply target."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 102},
                    "text": "telegram answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []
    prompt_message_ids = iter([101, 102, 103])

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": next(prompt_message_ids)}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(
        client.ask_question(
            ["Prompt chunk 1", "Prompt chunk 2", "Prompt metadata"],
            5,
            "QTEST-1234",
        )
    )

    assert result == "telegram answer"
    assert [payload["text"] for payload in sent_messages[:3]] == [
        "Prompt chunk 1",
        "Prompt chunk 2",
        "Prompt metadata",
    ]
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"
    assert sent_messages[-1]["reply_to_message_id"] == 201
    assert client._pending_by_message_id == {}


def test_telegram_client_combines_split_text_replies_in_one_update_page(
    monkeypatch,
    tmp_path,
):
    """Combine likely Telegram-split text reply parts from one update page."""
    monkeypatch.setattr(TelegramPromptClient, "TEXT_REPLY_SPLIT_MIN_LENGTH", 10)
    monkeypatch.setattr(TelegramPromptClient, "TEXT_REPLY_SPLIT_DEBOUNCE_SECONDS", 0.01)
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "first-long",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 202,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": " final",
                },
            },
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "first-long final"
    assert not any(
        payload["text"].startswith("⚠️ Message is ignored.") for payload in sent_messages
    )
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"
    assert sent_messages[-1]["reply_to_message_id"] == 202


def test_telegram_client_separates_split_text_replies_without_boundary_whitespace(
    monkeypatch,
    tmp_path,
):
    """Avoid silently gluing words when Telegram drops split-boundary whitespace."""
    monkeypatch.setattr(TelegramPromptClient, "TEXT_REPLY_SPLIT_MIN_LENGTH", 10)
    monkeypatch.setattr(TelegramPromptClient, "TEXT_REPLY_SPLIT_DEBOUNCE_SECONDS", 0.01)
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "first sentence.",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 202,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "Second",
                },
            },
        ],
        [],
    ]

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "first sentence.\nSecond"


def test_telegram_client_combines_split_text_replies_across_update_pages(
    monkeypatch,
    tmp_path,
):
    """Keep a likely split text reply pending long enough for the next poll."""
    monkeypatch.setattr(TelegramPromptClient, "TEXT_REPLY_SPLIT_MIN_LENGTH", 10)
    monkeypatch.setattr(TelegramPromptClient, "TEXT_REPLY_SPLIT_DEBOUNCE_SECONDS", 0.5)
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "first-long",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 202,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": " final",
                },
            }
        ],
        [],
    ]

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "first-long final"


def test_telegram_client_confirms_consumed_updates_before_stopping(monkeypatch, tmp_path):
    """Perform one final offset-advancing poll so consumed replies do not replay after restart."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "telegram answer",
                },
            }
        ],
        [],
    ]
    get_updates_payloads = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            get_updates_payloads.append(payload.copy())
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "telegram answer"
    assert [payload["offset"] for payload in get_updates_payloads] == [None, 2]
    assert [payload["timeout"] for payload in get_updates_payloads] == [
        DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
        0,
    ]


def test_telegram_client_ignores_backlog_updates_before_prompt_message(monkeypatch, tmp_path):
    """Do not warn for old updates delivered after a fresh prompt starts."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
        broker_identity=TelegramBrokerIdentity("abcd1234", "Alex Laptop"),
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 98,
                    "chat": {"id": -1009876543210},
                    "text": "old plain message before prompt",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 99,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 40},
                    "text": "old reply before prompt",
                },
            },
            {
                "update_id": 3,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "current reply",
                },
            },
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "current reply"
    assert [payload["text"] for payload in sent_messages] == [
        "Prompt text",
        "✅ Received [QTEST-1234]",
    ]


def test_prompt_send_failure_fails_prompt(monkeypatch, tmp_path):
    """Fail immediately if Telegram will not deliver the initial question."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            raise TelegramPromptError("initial send failed")
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    with pytest.raises(TelegramPromptError, match="initial send failed"):
        asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))


def test_telegram_client_rejects_too_large_file_and_waits_for_valid_text(monkeypatch, tmp_path):
    """Send a retry error for oversized files and keep waiting for a valid reply."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "document": {
                        "file_id": "file-too-big",
                        "file_size": 25 * 1024 * 1024,
                        "file_name": "huge.zip",
                    },
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "fallback text answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            if len(updates) == 2 and not any(
                "File too large for [QTEST-1234]" in message["text"] for message in sent_messages
            ):
                return []
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "fallback text answer"
    assert any("File too large for [QTEST-1234]" in payload["text"] for payload in sent_messages)
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_formats_file_reply_as_user_attachment(monkeypatch, tmp_path):
    """Tell the agent that the user attached the downloaded Telegram file."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "caption": "please inspect this",
                    "document": {
                        "file_id": "file-ok",
                        "file_size": 1024,
                        "file_name": "report.txt",
                    },
                },
            }
        ],
        [],
    ]

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        if method == "getFile":
            return {"file_path": "documents/report.txt", "file_size": 1024}
        raise AssertionError(f"Unexpected method: {method}")

    def fake_download_file_sync(telegram_file_path, target_path):
        assert telegram_file_path == "documents/report.txt"
        target_path.write_text("downloaded content", encoding="utf-8")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)
    monkeypatch.setattr(client, "_download_telegram_file_sync", fake_download_file_sync)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    expected_path = (tmp_path / "QTEST-1234" / "report.txt").resolve()
    assert result == (
        "[telegram document reply]\n"
        "Caption: please inspect this\n"
        f"User attached file: {expected_path}"
    )


def test_telegram_client_combines_media_group_photos(monkeypatch, tmp_path):
    """Treat Telegram albums as one reply instead of rejecting each item."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    client.ATTACHMENT_REPLY_DEBOUNCE_SECONDS = 0.01
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "media_group_id": "album-1",
                    "photo": [{"file_id": "photo-1", "file_size": 1024}],
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "media_group_id": "album-1",
                    "caption": "second caption",
                    "photo": [{"file_id": "photo-2", "file_size": 2048}],
                },
            },
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        if method == "getFile":
            return {"file_path": f"photos/{payload['file_id']}.jpg", "file_size": 1024}
        raise AssertionError(f"Unexpected method: {method}")

    def fake_download_file_sync(telegram_file_path, target_path):
        target_path.write_text("downloaded content", encoding="utf-8")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)
    monkeypatch.setattr(client, "_download_telegram_file_sync", fake_download_file_sync)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    expected_path_1 = (tmp_path / "QTEST-1234" / "photo-1.jpg").resolve()
    expected_path_2 = (tmp_path / "QTEST-1234" / "photo-2.jpg").resolve()
    assert result == (
        "[telegram media group reply]\n\n"
        "Items: 2\n\n"
        "Item 1/2:\n"
        "[telegram photo reply]\n"
        f"User attached file: {expected_path_1}\n\n"
        "Item 2/2:\n"
        "[telegram photo reply]\n"
        "Caption: second caption\n"
        f"User attached file: {expected_path_2}"
    )
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"
    assert sent_messages[-1]["reply_to_message_id"] == 211


def test_telegram_client_combines_ungrouped_attachment_burst(monkeypatch, tmp_path):
    """Collect a short burst of ungrouped attachments before resolving."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    client.ATTACHMENT_REPLY_DEBOUNCE_SECONDS = 0.01
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "document": {
                        "file_id": "file-1",
                        "file_size": 1024,
                        "file_name": "report.txt",
                    },
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "caption": "same visible filename",
                    "document": {
                        "file_id": "file-2",
                        "file_size": 1024,
                        "file_name": "report.txt",
                    },
                },
            },
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        if method == "getFile":
            return {"file_path": f"documents/{payload['file_id']}.txt", "file_size": 1024}
        raise AssertionError(f"Unexpected method: {method}")

    def fake_download_file_sync(telegram_file_path, target_path):
        target_path.write_text("downloaded content", encoding="utf-8")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)
    monkeypatch.setattr(client, "_download_telegram_file_sync", fake_download_file_sync)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    expected_path_1 = (tmp_path / "QTEST-1234" / "report.txt").resolve()
    expected_path_2 = (tmp_path / "QTEST-1234" / "report-2.txt").resolve()
    assert result == (
        "[telegram attachment group reply]\n\n"
        "Items: 2\n\n"
        "Item 1/2:\n"
        "[telegram document reply]\n"
        f"User attached file: {expected_path_1}\n\n"
        "Item 2/2:\n"
        "[telegram document reply]\n"
        "Caption: same visible filename\n"
        f"User attached file: {expected_path_2}\n"
        "Original file name: report.txt"
    )
    assert [payload["text"] for payload in sent_messages] == [
        "Prompt text",
        "✅ Received [QTEST-1234]",
    ]


def test_telegram_client_rejects_bad_attachment_group_once(monkeypatch, tmp_path):
    """Reject the whole collected group once if any item cannot be used."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    client.ATTACHMENT_REPLY_DEBOUNCE_SECONDS = 0.01
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "media_group_id": "album-1",
                    "document": {
                        "file_id": "file-ok",
                        "file_size": 1024,
                        "file_name": "small.txt",
                    },
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "media_group_id": "album-1",
                    "document": {
                        "file_id": "file-too-big",
                        "file_size": 25 * 1024 * 1024,
                        "file_name": "huge.zip",
                    },
                },
            },
        ],
        [
            {
                "update_id": 3,
                "message": {
                    "message_id": 212,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "fallback answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            if len(updates) == 2 and not any(
                "Unsupported attachment group" in message["text"] for message in sent_messages
            ):
                return []
            return updates.pop(0) if updates else []
        if method == "getFile":
            return {"file_path": "documents/small.txt", "file_size": 1024}
        raise AssertionError(f"Unexpected method: {method}")

    def fake_download_file_sync(telegram_file_path, target_path):
        target_path.write_text("downloaded content", encoding="utf-8")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)
    monkeypatch.setattr(client, "_download_telegram_file_sync", fake_download_file_sync)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "fallback answer"
    group_warnings = [
        payload["text"]
        for payload in sent_messages
        if "Unsupported attachment group" in payload["text"]
    ]
    assert len(group_warnings) == 1
    assert "File too large" in group_warnings[0]
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_parses_file_collection_commands_with_bot_username():
    """Accept Telegram's group-chat /command@BotUsername form."""
    parse_command = TelegramPromptClient._parse_series_command

    assert parse_command("/files_start") == "begin"
    assert parse_command("/files_start@AskHumanBot") == "begin"
    assert parse_command("/files_finish@AskHumanBot") == "commit"
    assert parse_command("/files_cancel@AskHumanBot") == "cancel"
    assert parse_command("/files_start now") is None
    assert parse_command("/files_start\tsoon") is None
    assert parse_command("/files_start@") is None
    assert parse_command("/files_start@bad-name") is None


def test_telegram_client_collects_explicit_attachment_series(monkeypatch, tmp_path):
    """Let users explicitly collect many/delayed Telegram items before resolving."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "/files_start",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "document": {
                        "file_id": "file-1",
                        "file_size": 1024,
                        "file_name": "report.txt",
                    },
                },
            }
        ],
        [
            {
                "update_id": 3,
                "message": {
                    "message_id": 212,
                    "chat": {"id": -1009876543210},
                    "photo": [{"file_id": "photo-1", "file_size": 2048}],
                },
            }
        ],
        [
            {
                "update_id": 4,
                "message": {
                    "message_id": 213,
                    "chat": {"id": -1009876543210},
                    "text": "extra note",
                },
            }
        ],
        [
            {
                "update_id": 5,
                "message": {
                    "message_id": 214,
                    "chat": {"id": -1009876543210},
                    "text": "/files_finish",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 300 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        if method == "getFile":
            file_id = payload["file_id"]
            if file_id == "photo-1":
                return {"file_path": "photos/photo-1.jpg", "file_size": 2048}
            return {"file_path": f"documents/{file_id}.txt", "file_size": 1024}
        raise AssertionError(f"Unexpected method: {method}")

    def fake_download_file_sync(telegram_file_path, target_path):
        target_path.write_text("downloaded content", encoding="utf-8")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)
    monkeypatch.setattr(client, "_download_telegram_file_sync", fake_download_file_sync)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    expected_file_path = (tmp_path / "QTEST-1234" / "report.txt").resolve()
    expected_photo_path = (tmp_path / "QTEST-1234" / "photo-1.jpg").resolve()
    assert result == (
        "[telegram attachment group reply]\n\n"
        "Items: 3\n\n"
        "Item 1/3:\n"
        "[telegram document reply]\n"
        f"User attached file: {expected_file_path}\n\n"
        "Item 2/3:\n"
        "[telegram photo reply]\n"
        f"User attached file: {expected_photo_path}\n\n"
        "Item 3/3:\n"
        "extra note"
    )
    assert [payload["text"] for payload in sent_messages] == [
        "Prompt text",
        "✅ File collection started for [QTEST-1234]. Send attachments or text, "
        "then send /files_finish to finalize and send. Send /files_cancel to discard this collection.",
        "✅ Received [QTEST-1234]",
    ]
    assert sent_messages[-1]["reply_to_message_id"] == 214


def test_telegram_client_cancels_explicit_attachment_series(monkeypatch, tmp_path):
    """Discard a series without resolving the original prompt."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "/files_start",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "document": {
                        "file_id": "file-1",
                        "file_size": 1024,
                        "file_name": "discard.txt",
                    },
                },
            }
        ],
        [
            {
                "update_id": 3,
                "message": {
                    "message_id": 212,
                    "chat": {"id": -1009876543210},
                    "text": "/files_cancel",
                },
            }
        ],
        [
            {
                "update_id": 4,
                "message": {
                    "message_id": 213,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "fallback answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 300 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        if method == "getFile":
            raise AssertionError("Cancelled series should not download files")
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "fallback answer"
    assert [payload["text"] for payload in sent_messages] == [
        "Prompt text",
        "✅ File collection started for [QTEST-1234]. Send attachments or text, "
        "then send /files_finish to finalize and send. Send /files_cancel to discard this collection.",
        "✅ File collection [QTEST-1234] cancelled. The prompt is still waiting; "
        "<b>Reply</b> ↪ normally or start a new file collection. Previous files are discarded 🗑️.",
        "✅ Received [QTEST-1234]",
    ]


def test_telegram_client_reply_to_other_prompt_escapes_series(monkeypatch, tmp_path):
    """A reply to another active prompt must not be captured by the active series."""

    async def run_two_prompt_flow():
        client = TelegramPromptClient(
            TelegramConfig("123456:ABCDEF", "-1009876543210"),
            tmp_path,
        )
        prompt_message_ids = {"Prompt one": 101, "Prompt two": 102}
        prompts_sent = 0
        sent_messages = []
        updates = [
            [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 210,
                        "chat": {"id": -1009876543210},
                        "reply_to_message": {"message_id": 101},
                        "text": "/files_start",
                    },
                }
            ],
            [
                {
                    "update_id": 2,
                    "message": {
                        "message_id": 211,
                        "chat": {"id": -1009876543210},
                        "document": {
                            "file_id": "file-1",
                            "file_size": 1024,
                            "file_name": "series.txt",
                        },
                    },
                }
            ],
            [
                {
                    "update_id": 3,
                    "message": {
                        "message_id": 212,
                        "chat": {"id": -1009876543210},
                        "reply_to_message": {"message_id": 102},
                        "text": "answer to second prompt",
                    },
                }
            ],
            [
                {
                    "update_id": 4,
                    "message": {
                        "message_id": 213,
                        "chat": {"id": -1009876543210},
                        "text": "/files_finish",
                    },
                }
            ],
            [],
        ]

        async def fake_bot_api_request(method, payload, timeout):
            nonlocal prompts_sent
            if method == "sendMessage":
                sent_messages.append(payload)
                if "parse_mode" in payload:
                    prompts_sent += 1
                    return {"message_id": prompt_message_ids[payload["text"]]}
                return {"message_id": 300 + len(sent_messages)}
            if method == "getUpdates":
                if prompts_sent < 2:
                    return []
                return updates.pop(0) if updates else []
            if method == "getFile":
                return {"file_path": "documents/series.txt", "file_size": 1024}
            raise AssertionError(f"Unexpected method: {method}")

        def fake_download_file_sync(telegram_file_path, target_path):
            target_path.write_text("downloaded content", encoding="utf-8")

        monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)
        monkeypatch.setattr(client, "_download_telegram_file_sync", fake_download_file_sync)

        first_prompt = asyncio.create_task(client.ask_question("Prompt one", 5, "QFIRST-1234"))
        second_prompt = asyncio.create_task(client.ask_question("Prompt two", 5, "QSECOND-1234"))

        first_result, second_result = await asyncio.gather(first_prompt, second_prompt)
        return first_result, second_result, sent_messages

    first_result, second_result, sent_messages = asyncio.run(run_two_prompt_flow())

    expected_path = (tmp_path / "QFIRST-1234" / "series.txt").resolve()
    assert first_result is not None
    assert first_result == ("[telegram document reply]\n" f"User attached file: {expected_path}")
    assert second_result == "answer to second prompt"
    assert "answer to second prompt" not in first_result
    assert [payload["text"] for payload in sent_messages] == [
        "Prompt one",
        "Prompt two",
        "✅ File collection started for [QFIRST-1234]. Send attachments or text, "
        "then send /files_finish to finalize and send. Send /files_cancel to discard this collection.",
        "✅ Received [QSECOND-1234]",
        "✅ Received [QFIRST-1234]",
    ]


def test_telegram_client_keeps_original_file_name_when_local_name_changes(monkeypatch, tmp_path):
    """Only include the original Telegram filename when the saved filename differs."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "document": {
                        "file_id": "file-ok",
                        "file_size": 1024,
                        "file_name": "bad:name.txt",
                    },
                },
            }
        ],
        [],
    ]

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        if method == "getFile":
            return {"file_path": "documents/fallback.txt", "file_size": 1024}
        raise AssertionError(f"Unexpected method: {method}")

    def fake_download_file_sync(telegram_file_path, target_path):
        assert telegram_file_path == "documents/fallback.txt"
        target_path.write_text("downloaded content", encoding="utf-8")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)
    monkeypatch.setattr(client, "_download_telegram_file_sync", fake_download_file_sync)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    expected_path = (tmp_path / "QTEST-1234" / "bad_name.txt").resolve()
    assert result == (
        "[telegram document reply]\n"
        f"User attached file: {expected_path}\n"
        "Original file name: bad:name.txt"
    )


def test_reply_rejection_status_send_failure_fails_prompt(monkeypatch, tmp_path):
    """Fail the prompt if Telegram will not deliver a retry/error message."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "document": {
                        "file_id": "file-too-big",
                        "file_size": 25 * 1024 * 1024,
                        "file_name": "huge.zip",
                    },
                },
            }
        ],
    ]

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if payload["text"] == "Prompt text" and "parse_mode" in payload:
                return {"message_id": 101}
            raise TelegramPromptError("status send failed")
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    with pytest.raises(TelegramPromptError, match="Telegram polling failed: status send failed"):
        asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))


def test_telegram_client_warns_for_each_non_reply_message(monkeypatch, tmp_path):
    """Hint each time the user sends a non-reply message while a local prompt is pending."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "text": "plain message without Reply",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "text": "another plain message without Reply",
                },
            }
        ],
        [
            {
                "update_id": 3,
                "message": {
                    "message_id": 212,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "proper reply",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "proper reply"
    assert [payload["text"] for payload in sent_messages].count(
        TelegramPromptClient.NON_REPLY_HINT_TEXT
    ) == 2
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_adds_series_hint_for_non_reply_attachment(monkeypatch, tmp_path):
    """Mention explicit series only when the ignored non-reply message has an attachment."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "document": {
                        "file_id": "file-1",
                        "file_size": 1024,
                        "file_name": "report.txt",
                    },
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "proper reply",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "proper reply"
    assert (
        TelegramPromptClient.NON_REPLY_HINT_TEXT + TelegramPromptClient.SERIES_ATTACHMENT_HINT_TEXT
    ) in [payload["text"] for payload in sent_messages]
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_non_reply_warning_send_failure_fails_prompt(monkeypatch, tmp_path):
    """Fail the prompt if Telegram will not deliver a non-reply warning."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "text": "plain message without Reply",
                },
            }
        ],
    ]

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if payload["text"] == "Prompt text" and "parse_mode" in payload:
                return {"message_id": 101}
            raise TelegramPromptError("status send failed")
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    with pytest.raises(TelegramPromptError, match="Telegram polling failed: status send failed"):
        asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))


def test_telegram_client_warns_for_stale_local_reply(monkeypatch, tmp_path):
    """Warn when the user replies to one of this broker's own older inactive prompts."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
        broker_identity=TelegramBrokerIdentity("abcd1234", "Alex Laptop"),
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 220,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {
                        "message_id": 100,
                        "text": (
                            "<blockquote expandable>\n"
                            "Answers support text or files up to 20 MB.\n"
                            "Prompt ID: QOLD-0001\n"
                            "Broker: Alex Laptop [abcd1234]\n"
                            "</blockquote>"
                        ),
                    },
                    "text": "answer to old prompt",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 221,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "answer to current prompt",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 400 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "answer to current prompt"
    assert any(
        payload["text"]
        == (
            "⚠️ Message is ignored. Prompt [QOLD-0001] is no longer active. Ask the "
            "agent to send a new question."
        )
        for payload in sent_messages
    )
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_adds_series_hint_for_stale_attachment_reply(monkeypatch, tmp_path):
    """Mention explicit series only when the stale reply itself has an attachment."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
        broker_identity=TelegramBrokerIdentity("abcd1234", "Alex Laptop"),
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 220,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {
                        "message_id": 100,
                        "text": (
                            "<blockquote expandable>\n"
                            "Answers support text or files up to 20 MB.\n"
                            "Prompt ID: QOLD-0001\n"
                            "Broker: Alex Laptop [abcd1234]\n"
                            "</blockquote>"
                        ),
                    },
                    "document": {
                        "file_id": "file-1",
                        "file_size": 1024,
                        "file_name": "report.txt",
                    },
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 221,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "answer to current prompt",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 400 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "answer to current prompt"
    assert (
        "⚠️ Message is ignored. Prompt [QOLD-0001] is no longer active. Ask the "
        "agent to send a new question." + TelegramPromptClient.SERIES_ATTACHMENT_HINT_TEXT
    ) in [payload["text"] for payload in sent_messages]
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_warns_for_foreign_broker_reply(monkeypatch, tmp_path):
    """Warn when this broker consumes a reply to another broker's prompt."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
        broker_identity=TelegramBrokerIdentity("abcd1234", "Alex Laptop"),
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 240,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {
                        "message_id": 100,
                        "text": (
                            "<blockquote expandable>\n"
                            "Answers support text or files up to 20 MB.\n"
                            "Prompt ID: QOTHER-0001\n"
                            "Broker: MacBook [feed5678]\n"
                            "</blockquote>"
                        ),
                    },
                    "text": "answer for the other broker",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 241,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "answer to current prompt",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 600 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "answer to current prompt"
    warning_messages = [
        payload for payload in sent_messages if "just consumed your reply" in payload["text"]
    ]
    assert len(warning_messages) == 1
    assert warning_messages[0]["reply_to_message_id"] == 240
    assert "Alex Laptop [abcd1234]" in warning_messages[0]["text"]
    assert "MacBook [feed5678]" in warning_messages[0]["text"]
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_unmatched_reply_without_prompt_text_gets_generic_warning(monkeypatch, tmp_path):
    """Warn generically when Telegram omits the replied-to prompt text."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
        broker_identity=TelegramBrokerIdentity("abcd1234", "Alex Laptop"),
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 230,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 100},
                    "text": "reply without nested prompt text",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 231,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "answer to current prompt",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 500 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0) if updates else []
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "answer to current prompt"
    warning_messages = [
        payload
        for payload in sent_messages
        if payload["text"] == TelegramPromptClient.UNMATCHED_REPLY_HINT_TEXT
    ]
    assert len(warning_messages) == 1
    assert warning_messages[0]["reply_to_message_id"] == 230
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_tool_uses_telegram_only_mode_without_dialog(monkeypatch):
    """Skip the local dialog when the telegram-only mode is selected."""

    class StubTelegramClient:
        def __init__(self):
            self.question = None
            self.context = None
            self.timeout = None
            self.prompt_id = None
            self.include_timing_info = None
            self.issued_at = None

        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            self.question = question
            self.context = context
            self.timeout = timeout_seconds
            self.prompt_id = prompt_id
            self.include_timing_info = include_timing_info
            self.issued_at = issued_at
            return "telegram answer"

    class StubDialogHandler:
        platform = "Linux"

        async def get_user_input(self, *args, **kwargs):
            raise AssertionError("Dialog should not be used in telegram mode")

    stub_telegram = StubTelegramClient()

    monkeypatch.setattr(server, "telegram_client", stub_telegram)
    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())
    monkeypatch.setattr(server, "response_channel", "telegram")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.ask_human("Where should I deploy?", "Need a quick answer."))

    assert result == "✅ Replied via Telegram:\ntelegram answer"
    assert stub_telegram.timeout == 300
    assert stub_telegram.question == "Where should I deploy?"
    assert stub_telegram.context == "Need a quick answer."
    assert stub_telegram.prompt_id is not None
    assert stub_telegram.prompt_id.startswith("Q")
    assert stub_telegram.include_timing_info is False
    assert isinstance(stub_telegram.issued_at, dt.datetime)


def test_tool_labels_structured_telegram_reply_by_channel(monkeypatch):
    """Do not make Telegram quote metadata look like literal user-authored text."""

    class StubTelegramClient:
        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            return (
                "User quoted your prompt:\n"
                "quoted prompt text\n"
                "\n"
                "User reply:\n"
                "telegram answer"
            )

    class StubDialogHandler:
        platform = "Linux"

        async def get_user_input(self, *args, **kwargs):
            raise AssertionError("Dialog should not be used in telegram mode")

    monkeypatch.setattr(server, "telegram_client", StubTelegramClient())
    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())
    monkeypatch.setattr(server, "response_channel", "telegram")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.ask_human("Q?", "Context text."))

    assert result == (
        "✅ Replied via Telegram:\n"
        "User quoted your prompt:\n"
        "quoted prompt text\n"
        "\n"
        "User reply:\n"
        "telegram answer"
    )


def test_both_mode_adds_windows_warning_and_threads_dialog(monkeypatch):
    """Warn on Windows and run the local dialog path in a worker thread."""

    class StubTelegramClient:
        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            return "telegram answer"

    class StubDialogHandler:
        platform = "Windows"

        def __init__(self):
            self.calls = []

        async def get_user_input(
            self,
            question,
            timeout,
            *,
            cancel_event=None,
            run_in_thread=False,
        ):
            self.calls.append(
                {
                    "question": question,
                    "timeout": timeout,
                    "cancel_event": cancel_event,
                    "run_in_thread": run_in_thread,
                }
            )
            await asyncio.sleep(3600)
            return None

    stub_dialog = StubDialogHandler()

    monkeypatch.setattr(server, "telegram_client", StubTelegramClient())
    monkeypatch.setattr(server, "dialog_handler", stub_dialog)
    monkeypatch.setattr(server, "response_channel", "both")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.ask_human("Q?", "Context text."))

    assert result == "✅ Replied via Telegram:\ntelegram answer"
    assert stub_dialog.calls[0]["run_in_thread"] is True
    assert stub_dialog.calls[0]["cancel_event"] is None
    assert stub_dialog.calls[0]["question"] is not None
    assert "📨 Also sent to Telegram." in stub_dialog.calls[0]["question"]
    assert "will stay open" in stub_dialog.calls[0]["question"]


def test_both_mode_cancels_linux_dialog_when_telegram_wins(monkeypatch):
    """Signal subprocess-backed dialogs to close when Telegram wins the race."""

    class StubTelegramClient:
        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            return "telegram answer"

    class StubDialogHandler:
        platform = "Linux"

        def __init__(self):
            self.question = None
            self.cancel_event = None

        async def get_user_input(
            self,
            question,
            timeout,
            *,
            cancel_event=None,
            run_in_thread=False,
        ):
            self.question = question
            self.cancel_event = cancel_event
            assert cancel_event is not None
            await cancel_event.wait()
            return None

    stub_dialog = StubDialogHandler()

    monkeypatch.setattr(server, "telegram_client", StubTelegramClient())
    monkeypatch.setattr(server, "dialog_handler", stub_dialog)
    monkeypatch.setattr(server, "response_channel", "both")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.ask_human("Q?", "Context text."))

    assert result == "✅ Replied via Telegram:\ntelegram answer"
    assert stub_dialog.cancel_event is not None
    assert stub_dialog.question is not None
    assert stub_dialog.cancel_event.is_set() is True
    assert "📨 Also sent to Telegram." in stub_dialog.question
    assert "will stay open" not in stub_dialog.question


def test_both_mode_surfaces_telegram_failure_while_dialog_waits(monkeypatch):
    """Do not hide Telegram delivery failures behind a still-open local dialog."""

    class StubTelegramClient:
        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            raise TelegramPromptError("send failed before delivery")

    class StubDialogHandler:
        platform = "Linux"

        def __init__(self):
            self.cancel_event = None

        async def get_user_input(
            self,
            question,
            timeout,
            *,
            cancel_event=None,
            run_in_thread=False,
        ):
            self.cancel_event = cancel_event
            assert cancel_event is not None
            await cancel_event.wait()
            return None

    stub_dialog = StubDialogHandler()

    monkeypatch.setattr(server, "telegram_client", StubTelegramClient())
    monkeypatch.setattr(server, "dialog_handler", stub_dialog)
    monkeypatch.setattr(server, "response_channel", "both")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.ask_human("Q?", "Context text."))

    assert result == ("❌ User Prompt Error: Telegram prompt failed: send failed before delivery")
    assert stub_dialog.cancel_event is not None
    assert stub_dialog.cancel_event.is_set() is True


def test_both_mode_prefers_completed_dialog_answer_over_simultaneous_telegram_error(
    monkeypatch,
):
    """Keep first-reply-wins when a local answer is already completed."""

    class StubTelegramClient:
        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            raise TelegramPromptError("send failed")

    class StubDialogHandler:
        platform = "Linux"

        async def get_user_input(
            self,
            question,
            timeout,
            *,
            cancel_event=None,
            run_in_thread=False,
        ):
            return "local answer"

    monkeypatch.setattr(server, "telegram_client", StubTelegramClient())
    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())
    monkeypatch.setattr(server, "response_channel", "both")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.ask_human("Q?", "Context text."))

    assert result == "✅ User reply:\nlocal answer"
