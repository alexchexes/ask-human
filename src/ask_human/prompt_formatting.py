"""Prompt formatting helpers for dialogs and Telegram delivery."""

import datetime as dt
import html
import locale
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from markdown_it import MarkdownIt
from markdown_it.token import Token

DEFAULT_DIALOG_TITLE = "Agent asks..."
TELEGRAM_DOWNLOAD_LIMIT_LABEL = "20 MB"
TELEGRAM_PROMPT_SEPARATOR = "─" * 12
TIMING_INFO_TIMEOUT_NOTE = "client may time out sooner"
TELEGRAM_HTML_BLANK_LINES_PATTERN = re.compile(r"\n{3,}")
TELEGRAM_HTML_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]+$")
TELEGRAM_HTML_TAG_PATTERN = re.compile(r"<[^>\n]*>")
TELEGRAM_MARKDOWN = MarkdownIt("commonmark", {"html": False})


@dataclass
class _TelegramListState:
    ordered: bool
    next_number: int = 1


def resolve_dialog_title(dialog_title: Optional[str] = None) -> str:
    """Resolve the dialog title from CLI input or default."""
    if dialog_title and dialog_title.strip():
        return dialog_title.strip()

    return DEFAULT_DIALOG_TITLE


def generate_prompt_id(now: Optional[dt.datetime] = None) -> str:
    """Generate a short human-readable prompt identifier for Telegram workflows."""
    moment = (now or dt.datetime.now().astimezone()).astimezone()
    return f"Q{moment.strftime('%y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"


def format_dialog_timestamp(moment: dt.datetime) -> str:
    """Format dialog timestamps using the current locale's short date/time format."""
    try:
        formatted = moment.astimezone().strftime("%x %X").strip()
        if formatted:
            return formatted
    except Exception:
        pass

    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def escape_telegram_html(text: str, *, quote: bool = False) -> str:
    """Escape arbitrary text for Telegram HTML parse mode."""
    return html.escape(text, quote=quote)


def initialize_time_locale() -> None:
    """Initialize process-wide time formatting from the OS locale once at startup."""
    try:
        locale.setlocale(locale.LC_TIME, "")
    except Exception:
        pass


def render_markdown_to_telegram_html(markdown_text: str) -> str:
    """Render common Markdown to Telegram-supported HTML."""
    tokens = TELEGRAM_MARKDOWN.parse(markdown_text.strip())
    parts: list[str] = []
    list_stack: list[_TelegramListState] = []
    list_item_depth = 0
    previous_token_type: Optional[str] = None

    def has_trailing_newline() -> bool:
        return bool(parts) and parts[-1].endswith("\n")

    def ensure_newline() -> None:
        if parts and not has_trailing_newline():
            parts.append("\n")

    def append_fenced_code(token: Token) -> None:
        ensure_newline()
        language = _extract_telegram_code_language(token.info)
        escaped_content = escape_telegram_html(token.content)
        if language:
            parts.append(f'<pre><code class="language-{language}">{escaped_content}</code></pre>\n')
        else:
            parts.append(f"<pre>{escaped_content}</pre>\n")

    for token in tokens:
        token_type = token.type

        if token_type in {"heading_open", "paragraph_open"}:
            previous_token_type = token_type
            continue
        if token_type == "heading_close":
            parts.append("</b>\n")
            previous_token_type = token_type
            continue
        if token_type == "paragraph_close":
            if list_item_depth == 0:
                ensure_newline()
            previous_token_type = token_type
            continue
        if token_type == "inline":
            if previous_token_type == "heading_open":
                parts.append("<b>")
            parts.append(_render_inline_telegram_html(token.children or []))
            previous_token_type = token_type
            continue
        if token_type == "blockquote_open":
            ensure_newline()
            parts.append("<blockquote>")
            previous_token_type = token_type
            continue
        if token_type == "blockquote_close":
            parts.append("</blockquote>\n")
            previous_token_type = token_type
            continue
        if token_type == "bullet_list_open":
            ensure_newline()
            list_stack.append(_TelegramListState(ordered=False))
            previous_token_type = token_type
            continue
        if token_type == "ordered_list_open":
            ensure_newline()
            list_stack.append(
                _TelegramListState(ordered=True, next_number=_ordered_list_start(token))
            )
            previous_token_type = token_type
            continue
        if token_type in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
            ensure_newline()
            previous_token_type = token_type
            continue
        if token_type == "list_item_open":
            list_item_depth += 1
            ensure_newline()
            parts.append(_list_item_prefix(list_stack, token, list_item_depth))
            previous_token_type = token_type
            continue
        if token_type == "list_item_close":
            list_item_depth = max(0, list_item_depth - 1)
            ensure_newline()
            previous_token_type = token_type
            continue
        if token_type in {"fence", "code_block"}:
            append_fenced_code(token)
            previous_token_type = token_type
            continue
        if token_type == "hr":
            ensure_newline()
            parts.append(f"{TELEGRAM_PROMPT_SEPARATOR}\n")
            previous_token_type = token_type
            continue
        if token.content:
            parts.append(escape_telegram_html(token.content))
        previous_token_type = token_type

    return _normalize_telegram_html_text("".join(parts))


def telegram_html_to_plain_text(prompt_text: str) -> str:
    """Convert Telegram HTML prompt text to a readable plain-text fallback."""
    plain_text = prompt_text.replace("</blockquote>", "\n")
    plain_text = TELEGRAM_HTML_TAG_PATTERN.sub("", plain_text)
    plain_text = html.unescape(plain_text)
    return _normalize_telegram_html_text(plain_text)


def _render_inline_telegram_html(tokens: list[Token]) -> str:
    parts: list[str] = []
    link_stack: list[bool] = []

    for token in tokens:
        token_type = token.type
        if token_type == "text":
            parts.append(escape_telegram_html(token.content))
        elif token_type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token_type == "strong_open":
            parts.append("<b>")
        elif token_type == "strong_close":
            parts.append("</b>")
        elif token_type == "em_open":
            parts.append("<i>")
        elif token_type == "em_close":
            parts.append("</i>")
        elif token_type == "code_inline":
            parts.append(f"<code>{escape_telegram_html(token.content)}</code>")
        elif token_type == "link_open":
            href = _token_attribute(token, "href")
            if href:
                parts.append(f'<a href="{escape_telegram_html(href, quote=True)}">')
                link_stack.append(True)
            else:
                link_stack.append(False)
        elif token_type == "link_close":
            if link_stack and link_stack.pop():
                parts.append("</a>")
        elif token.children:
            parts.append(_render_inline_telegram_html(token.children))
        elif token.content:
            parts.append(escape_telegram_html(token.content))

    return "".join(parts)


def _token_attribute(token: Token, name: str) -> Optional[str]:
    value = token.attrs.get(name) if token.attrs else None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _extract_telegram_code_language(info: str) -> Optional[str]:
    language = info.strip().split(maxsplit=1)[0] if info.strip() else ""
    if TELEGRAM_HTML_LANGUAGE_PATTERN.fullmatch(language):
        return language
    return None


def _ordered_list_start(token: Token) -> int:
    start_value = token.attrs.get("start") if token.attrs else None
    if isinstance(start_value, int):
        return start_value
    if isinstance(start_value, str) and start_value.isdigit():
        return int(start_value)
    return 1


def _list_item_prefix(
    list_stack: list[_TelegramListState], token: Token, list_item_depth: int
) -> str:
    indentation = "  " * max(0, list_item_depth - 1)
    if not list_stack:
        return f"{indentation}- "

    current_list = list_stack[-1]
    if not current_list.ordered:
        return f"{indentation}- "

    if token.info.strip().isdigit():
        number = int(token.info.strip())
        current_list.next_number = number + 1
    else:
        number = current_list.next_number
        current_list.next_number += 1
    return f"{indentation}{number}. "


def _normalize_telegram_html_text(text: str) -> str:
    without_trailing_spaces = re.sub(r"[ \t]+\n", "\n", text)
    compacted = TELEGRAM_HTML_BLANK_LINES_PATTERN.sub("\n\n", without_trailing_spaces)
    return compacted.strip()


def build_timing_info_lines(issued_at: dt.datetime, timeout_seconds: int) -> list[str]:
    """Build optional timing metadata lines for dialogs or Telegram prompts."""
    answer_until = issued_at + dt.timedelta(seconds=timeout_seconds)
    return [
        f"Issued at: {format_dialog_timestamp(issued_at)}",
        f"Answer until: {format_dialog_timestamp(answer_until)}",
        f"({TIMING_INFO_TIMEOUT_NOTE})",
    ]


def build_timing_info_block(issued_at: dt.datetime, timeout_seconds: int) -> str:
    """Build the optional timing metadata shown in dialogs."""
    timing_lines = build_timing_info_lines(issued_at, timeout_seconds)
    return f"{timing_lines[0]} | {timing_lines[1]} {timing_lines[2]}"


def build_dialog_telegram_notice(platform_name: str) -> str:
    """Explain that the prompt was also delivered through Telegram."""
    if platform_name == "Windows":
        return (
            "📨 Also sent to Telegram. ⚠️ If you reply there first, this dialog will stay "
            "open. Any later answer here will be ignored."
        )

    return "📨 Also sent to Telegram."


def build_prompt_text(
    question: str,
    context: str,
    *,
    timeout_seconds: int,
    include_timing_info: bool,
    extra_note: str = "",
    issued_at: Optional[dt.datetime] = None,
) -> str:
    """Build the formatted prompt text for native dialogs."""
    separator = "─" * 40
    question_block = f"❓ Question:\n{question.strip()}"
    if extra_note.strip():
        question_block = f"{question_block}\n\n{extra_note.strip()}"

    if context.strip():
        full_question = f"📋 Context:\n{context.strip()}\n\n{separator}\n\n{question_block}"
    else:
        full_question = question_block

    if include_timing_info:
        effective_issued_at = issued_at or dt.datetime.now().astimezone()
        return (
            f"{full_question}\n\n{separator}\n\n"
            f"{build_timing_info_block(effective_issued_at, timeout_seconds)}"
        )

    return full_question


def build_telegram_prompt_text(
    question: str,
    context: str,
    *,
    prompt_id: str,
    timeout_seconds: int,
    include_timing_info: bool,
    issued_at: Optional[dt.datetime] = None,
    broker_label: Optional[str] = None,
    broker_id: Optional[str] = None,
) -> str:
    """Build a Telegram-specific prompt using HTML parse mode and compact metadata."""
    effective_issued_at = issued_at or dt.datetime.now().astimezone()
    parts: list[str] = []

    if context.strip():
        parts.extend(
            [
                "<b>📋 Context:</b>",
                render_markdown_to_telegram_html(context.strip()),
                "",
                TELEGRAM_PROMPT_SEPARATOR,
                "",
            ]
        )

    parts.extend(
        [
            "<b>❓ Question:</b>",
            render_markdown_to_telegram_html(question.strip()),
            "",
            TELEGRAM_PROMPT_SEPARATOR,
            "",
        ]
    )

    metadata_lines: list[str] = []
    if include_timing_info:
        metadata_lines.extend(build_timing_info_lines(effective_issued_at, timeout_seconds))

    metadata_lines.extend(
        [
            f"Answers support text or files up to {TELEGRAM_DOWNLOAD_LIMIT_LABEL}.",
            f"Prompt ID: {prompt_id}",
        ]
    )
    if broker_label and broker_id:
        metadata_lines.append(f"Broker: {broker_label} [{broker_id}]")
    metadata_block = "\n".join(escape_telegram_html(line) for line in metadata_lines)
    parts.extend(
        [
            f"<blockquote expandable>{metadata_block}</blockquote>",
            "",
            '↩️ Use "Reply" on this message to answer.',
        ]
    )

    return "\n".join(parts)
