"""Convert Telegram Bot API text entities into agent-readable Markdown."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, TypeGuard

_BACKTICK_RUN_PATTERN = re.compile(r"`+")


@dataclass
class _EntityNode:
    entity_type: str
    offset: int
    length: int
    payload: dict[str, Any]
    children: list[_EntityNode] = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.offset + self.length


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _link_destination(url: str) -> str:
    """Retain the entity URL while keeping the Markdown destination bounded."""
    return (
        url.replace("\\", "\\\\")
        .replace("<", "%3C")
        .replace(">", "\\>")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _format_link(value: str, url: str) -> str:
    return f"[{value}](<{_link_destination(url)}>)" if url else value


def _format_code(value: str) -> str:
    max_backticks = max(
        (len(match.group(0)) for match in _BACKTICK_RUN_PATTERN.finditer(value)),
        default=0,
    )
    delimiter = "`" * (max_backticks + 1)
    padding = " " if max_backticks or value.startswith(" ") or value.endswith(" ") else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"


def _format_pre(value: str, language: str) -> str:
    max_backticks = max(
        (len(match.group(0)) for match in _BACKTICK_RUN_PATTERN.finditer(value)),
        default=0,
    )
    fence = "`" * max(3, max_backticks + 1)
    safe_language = " ".join(language.replace("`", "").split())
    final_newline = "" if value.endswith("\n") else "\n"
    return f"{fence}{safe_language}\n{value}{final_newline}{fence}"


def _decorate(node: _EntityNode, value: str) -> str:
    entity_type = node.entity_type
    if entity_type == "bold":
        return f"**{value}**"
    if entity_type == "italic":
        return f"*{value}*"
    if entity_type == "code":
        return _format_code(value)
    if entity_type == "pre":
        language = node.payload.get("language")
        return _format_pre(value, language if isinstance(language, str) else "")
    if entity_type == "underline":
        return f"<u>{value}</u>"
    if entity_type == "strikethrough":
        return f"~~{value}~~"
    if entity_type == "spoiler":
        return f"<telegram-spoiler>{value}</telegram-spoiler>"
    if entity_type in {"blockquote", "expandable_blockquote"}:
        return "\n".join(f"> {line}" for line in value.split("\n"))
    if entity_type == "text_link":
        return _format_link(value, node.payload["url"])
    if entity_type == "text_mention":
        return _format_link(value, f"tg://user?id={node.payload['user']['id']}")
    if entity_type == "date_time":
        query = {"unix": str(node.payload["unix_time"])}
        date_time_format = node.payload.get("date_time_format")
        if isinstance(date_time_format, str) and date_time_format:
            query["format"] = date_time_format
        return _format_link(value, f"tg://time?{urllib.parse.urlencode(query)}")

    # Automatically detected, custom-emoji, and future entity types keep their visible text.
    return value


def _parse_entity(
    raw_entity: Any,
    utf16_boundaries: dict[int, int],
) -> _EntityNode | None:
    if not isinstance(raw_entity, dict):
        return None

    entity_type = raw_entity.get("type")
    offset = raw_entity.get("offset")
    length = raw_entity.get("length")
    if (
        not isinstance(entity_type, str)
        or not entity_type
        or not _is_int(offset)
        or not _is_int(length)
        or length <= 0
        or offset not in utf16_boundaries
        or offset + length not in utf16_boundaries
    ):
        return None

    if entity_type == "text_link" and not isinstance(raw_entity.get("url"), str):
        return None
    if entity_type == "text_mention":
        user = raw_entity.get("user")
        if not isinstance(user, dict) or not _is_int(user.get("id")):
            return None
    if entity_type == "date_time" and not _is_int(raw_entity.get("unix_time")):
        return None

    return _EntityNode(
        entity_type=entity_type,
        offset=offset,
        length=length,
        payload=raw_entity,
    )


def _render_range(
    text: str,
    utf16_boundaries: dict[int, int],
    start: int,
    end: int,
    children: list[_EntityNode],
) -> str:
    parts: list[str] = []
    cursor = start
    for child in children:
        parts.append(text[utf16_boundaries[cursor] : utf16_boundaries[child.offset]])
        value = _render_range(
            text,
            utf16_boundaries,
            child.offset,
            child.end,
            child.children,
        )
        parts.append(_decorate(child, value))
        cursor = child.end
    parts.append(text[utf16_boundaries[cursor] : utf16_boundaries[end]])
    return "".join(parts)


def telegram_entities_to_markdown(text: str, raw_entities: Any = None) -> str:
    """Annotate valid Telegram entities without rewriting the visible text."""
    if not isinstance(raw_entities, list):
        return text

    # Telegram entity offsets count UTF-16 code units, not Python characters.
    utf16_boundaries = {0: 0}
    utf16_offset = 0
    for index, character in enumerate(text, start=1):
        utf16_offset += 2 if ord(character) > 0xFFFF else 1
        utf16_boundaries[utf16_offset] = index

    entities = [
        entity
        for raw_entity in raw_entities
        if (entity := _parse_entity(raw_entity, utf16_boundaries)) is not None
    ]
    entities.sort(key=lambda entity: (entity.offset, -entity.length))

    roots: list[_EntityNode] = []
    containing_entities: list[_EntityNode] = []
    for entity in entities:
        while containing_entities and entity.offset >= containing_entities[-1].end:
            containing_entities.pop()
        if containing_entities and entity.end > containing_entities[-1].end:
            return text

        siblings = containing_entities[-1].children if containing_entities else roots
        siblings.append(entity)
        containing_entities.append(entity)

    return _render_range(text, utf16_boundaries, 0, utf16_offset, roots)
