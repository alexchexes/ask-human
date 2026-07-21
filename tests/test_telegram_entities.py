"""Tests for restoring Telegram Bot API entities to agent-facing Markdown."""

from __future__ import annotations

from typing import Any

from ask_human.telegram_entities import telegram_entities_to_markdown


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _entity(text: str, value: str, entity_type: str, **payload: Any) -> dict[str, Any]:
    start = text.index(value)
    return {
        "type": entity_type,
        "offset": _utf16_length(text[:start]),
        "length": _utf16_length(value),
        **payload,
    }


def test_telegram_entities_to_markdown_restores_supported_semantics():
    text = (
        "prefix 😀 <&> BOLDITALIC | code`tick | print(<x> & y) | linked | Named User | "
        "underlined | struck | secret | 🧪 | date\nquote line\nexpandable line | FUTURE"
    )
    entities = [
        _entity(text, "BOLDITALIC", "bold"),
        _entity(text, "ITALIC", "italic"),
        _entity(text, "code`tick", "code"),
        _entity(text, "print(<x> & y)", "pre", language="bash"),
        _entity(text, "linked", "text_link", url="https://example.com/a?x=1&y=2"),
        _entity(
            text,
            "Named User",
            "text_mention",
            user={"id": 123456789, "is_bot": False, "first_name": "Named"},
        ),
        _entity(text, "underlined", "underline"),
        _entity(text, "struck", "strikethrough"),
        _entity(text, "secret", "spoiler"),
        _entity(text, "🧪", "custom_emoji", custom_emoji_id="5368324170671202286"),
        _entity(
            text,
            "date",
            "date_time",
            unix_time=1784653200,
            date_time_format="wDT",
        ),
        _entity(text, "quote line", "blockquote"),
        _entity(text, "expandable line", "expandable_blockquote"),
        _entity(text, "FUTURE", "unknown_future"),
    ]

    rendered = telegram_entities_to_markdown(text, entities)

    assert rendered == (
        "prefix 😀 <&> **BOLD*ITALIC*** | `` code`tick `` | "
        "```bash\nprint(<x> & y)\n``` | "
        "[linked](<https://example.com/a?x=1&y=2>) | "
        "[Named User](<tg://user?id=123456789>) | "
        "<u>underlined</u> | ~~struck~~ | "
        "<telegram-spoiler>secret</telegram-spoiler> | 🧪 | "
        "[date](<tg://time?unix=1784653200&format=wDT>)\n"
        "> quote line\n> expandable line | FUTURE"
    )


def test_telegram_entities_to_markdown_preserves_code_backslashes_and_backticks():
    text = "path\\* and code`tick"
    entities = [_entity(text, text, "code")]

    rendered = telegram_entities_to_markdown(text, entities)

    assert rendered == "`` path\\* and code`tick ``"


def test_telegram_entities_to_markdown_ignores_malformed_ranges():
    text = "😀 *visible*"
    entities = [
        {"type": "bold", "offset": 1, "length": 2},
        {"type": "italic", "offset": 500, "length": 2},
        {"offset": 3, "length": 9},
        "not an entity",
    ]

    assert telegram_entities_to_markdown(text, entities) == text


def test_telegram_entities_to_markdown_ignores_missing_required_payloads():
    text = "valid mention date"
    entities = [
        _entity(text, "valid", "bold"),
        _entity(text, "mention", "text_mention"),
        _entity(text, "date", "date_time"),
    ]

    assert telegram_entities_to_markdown(text, entities) == "**valid** mention date"


def test_telegram_entities_to_markdown_preserves_unformatted_markdown_like_text():
    text = "# heading\n- item\n1. item\n---\nliteral ` *word* _ [x] <em> ~~"

    assert telegram_entities_to_markdown(text, None) == text


def test_telegram_entities_to_markdown_allows_multiline_markers_and_literal_html():
    text = "first\nliteral <em> and *stars*\nlast"

    assert (
        telegram_entities_to_markdown(
            text,
            [{"type": "italic", "offset": 0, "length": _utf16_length(text)}],
        )
        == f"*{text}*"
    )


def test_telegram_entities_to_markdown_keeps_punctuation_bounded_marker():
    text = "a!b"

    assert (
        telegram_entities_to_markdown(
            text,
            [_entity(text, "!", "italic")],
        )
        == "a*!*b"
    )


def test_telegram_entities_to_markdown_rejects_crossing_ranges_without_changing_text():
    text = "abcdefghij"
    entities = [
        {"type": "bold", "offset": 0, "length": 5},
        {"type": "italic", "offset": 3, "length": 5},
    ]

    assert telegram_entities_to_markdown(text, entities) == text


def test_telegram_entities_to_markdown_normalizes_same_offset_entities_outer_first():
    text = "abcdef"
    entities = [
        {"type": "italic", "offset": 0, "length": 3},
        {"type": "bold", "offset": 0, "length": 6},
    ]

    assert telegram_entities_to_markdown(text, entities) == "***abc*def**"


def test_telegram_entities_to_markdown_preserves_non_lf_line_separators_in_quotes():
    text = "first\r\nsecond\u2028third"

    assert (
        telegram_entities_to_markdown(
            text,
            [{"type": "blockquote", "offset": 0, "length": _utf16_length(text)}],
        )
        == "> first\r\n> second\u2028third"
    )


def test_telegram_entities_to_markdown_keeps_terminal_lf_in_malformed_quote_range():
    text = "quoted\n"

    assert (
        telegram_entities_to_markdown(
            text,
            [{"type": "blockquote", "offset": 0, "length": _utf16_length(text)}],
        )
        == "> quoted\n> "
    )


def test_telegram_entities_to_markdown_keeps_automatic_entities_transparent():
    entity_types = [
        "mention",
        "hashtag",
        "cashtag",
        "bot_command",
        "url",
        "email",
        "phone_number",
        "custom_emoji",
    ]
    text = " | ".join(entity_types)
    entities = [_entity(text, entity_type, entity_type) for entity_type in entity_types]

    assert telegram_entities_to_markdown(text, entities) == text


def test_telegram_entities_to_markdown_rejects_boolean_offsets_and_payload_ids():
    text = "bold mention date"
    entities = [
        {"type": "bold", "offset": False, "length": 4},
        {
            **_entity(text, "mention", "text_mention"),
            "user": {"id": True},
        },
        {
            **_entity(text, "date", "date_time"),
            "unix_time": False,
        },
    ]

    assert telegram_entities_to_markdown(text, entities) == text
