"""Tests for client-side local Telegram broker discovery and prompting."""

import asyncio
import datetime as dt

import pytest

from ask_human import __version__
from ask_human import telegram_broker_client as broker_client_module
from ask_human.broker_state import (
    TelegramBrokerHealth,
    load_or_create_broker_identity,
    persist_broker_listen_url,
)
from ask_human.telegram_broker_client import TelegramBrokerClient
from ask_human.telegram_models import (
    TelegramConfig,
    TelegramPromptError,
    resolve_telegram_target_key,
)


def test_broker_client_builds_prompt_with_broker_metadata(monkeypatch, tmp_path):
    """Use broker health metadata when formatting Telegram prompts."""
    client = TelegramBrokerClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    captured = {}

    async def fake_ensure_local_broker():
        return TelegramBrokerHealth(
            broker_id="abcd1234",
            broker_label="Alex Laptop",
            listen_url="http://127.0.0.1:7456",
            target_key="feedbeef",
        )

    async def fake_broker_request(listen_url, path, payload, *, timeout, method="POST"):
        captured["listen_url"] = listen_url
        captured["path"] = path
        captured["payload"] = payload
        captured["timeout"] = timeout
        captured["method"] = method
        return {"status": "ok", "response": "telegram answer"}

    monkeypatch.setattr(client, "_ensure_local_broker", fake_ensure_local_broker)
    monkeypatch.setattr(client, "_broker_request", fake_broker_request)

    result = asyncio.run(
        client.ask_question(
            "Prompt text",
            "Context text.",
            prompt_id="QTEST-1234",
            timeout_seconds=300,
            include_timing_info=True,
            issued_at=dt.datetime(2026, 5, 11, 10, 0, 0),
        )
    )

    assert result == "telegram answer"
    assert captured["listen_url"] == "http://127.0.0.1:7456"
    assert captured["path"] == "prompts"
    assert captured["method"] == "POST"
    assert "Prompt ID: QTEST-1234" in captured["payload"]["prompt_text"]
    assert "Broker: Alex Laptop [abcd1234]" in captured["payload"]["prompt_text"]
    assert captured["payload"]["prompt_texts"] == [captured["payload"]["prompt_text"]]
    assert captured["payload"]["download_dir"] == str((tmp_path / "downloads").resolve())


def test_broker_client_uses_delivery_time_for_telegram_timing(monkeypatch, tmp_path):
    """Build Telegram timing metadata after broker startup, close to delivery time."""
    client = TelegramBrokerClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    original_issued_at = dt.datetime(1999, 1, 1, 0, 0, 0)
    delivery_issued_at = dt.datetime(2026, 5, 11, 10, 0, 0, tzinfo=dt.timezone.utc)
    captured = {}

    async def fake_ensure_local_broker():
        captured["broker_ready"] = True
        return TelegramBrokerHealth(
            broker_id="abcd1234",
            broker_label="Alex Laptop",
            listen_url="http://127.0.0.1:7456",
            target_key="feedbeef",
        )

    class FakeDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            assert captured["broker_ready"] is True
            if tz is None:
                return delivery_issued_at
            return delivery_issued_at.astimezone(tz)

    def fake_build_telegram_prompt_text(*args, **kwargs):
        captured["prompt_issued_at"] = kwargs["issued_at"]
        return "Prompt text"

    def fake_build_telegram_prompt_texts(*args, **kwargs):
        captured["prompt_texts_issued_at"] = kwargs["issued_at"]
        return ["Prompt text"]

    async def fake_broker_request(listen_url, path, payload, *, timeout, method="POST"):
        return {"status": "ok", "response": "telegram answer"}

    monkeypatch.setattr(client, "_ensure_local_broker", fake_ensure_local_broker)
    monkeypatch.setattr(client, "_broker_request", fake_broker_request)
    monkeypatch.setattr(broker_client_module.dt, "datetime", FakeDateTime)
    monkeypatch.setattr(
        broker_client_module,
        "build_telegram_prompt_text",
        fake_build_telegram_prompt_text,
    )
    monkeypatch.setattr(
        broker_client_module,
        "build_telegram_prompt_texts",
        fake_build_telegram_prompt_texts,
    )

    result = asyncio.run(
        client.ask_question(
            "Prompt text",
            "Context text.",
            prompt_id="QTEST-1234",
            timeout_seconds=300,
            include_timing_info=True,
            issued_at=original_issued_at,
        )
    )

    assert result == "telegram answer"
    assert captured["prompt_issued_at"] == delivery_issued_at
    assert captured["prompt_texts_issued_at"] == delivery_issued_at


def test_broker_client_waits_for_started_broker(monkeypatch, tmp_path):
    """Start a local broker and then reuse its persisted healthy state."""
    client = TelegramBrokerClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    health = TelegramBrokerHealth(
        broker_id="abcd1234",
        broker_label="Alex Laptop",
        listen_url="http://127.0.0.1:7456",
        target_key="feedbeef",
    )
    calls = {"probe": 0, "spawn": 0, "replace_mismatched": []}

    async def fake_probe_persisted_broker(*, replace_mismatched=False):
        calls["probe"] += 1
        calls["replace_mismatched"].append(replace_mismatched)
        if calls["probe"] <= 2:
            return None
        return health

    async def fake_wait_for_local_broker():
        return health

    monkeypatch.setattr(client, "_probe_persisted_broker", fake_probe_persisted_broker)
    monkeypatch.setattr(client, "_wait_for_local_broker", fake_wait_for_local_broker)
    monkeypatch.setattr(
        client,
        "_spawn_local_broker",
        lambda: calls.__setitem__("spawn", calls["spawn"] + 1),
    )

    result = asyncio.run(client._ensure_local_broker())

    assert result == health
    assert calls["spawn"] == 1
    assert calls["probe"] >= 2
    assert calls["replace_mismatched"] == [False, True]


def test_broker_client_shuts_down_version_mismatched_broker(monkeypatch, tmp_path):
    """Replace a broker from another package version before sending prompts."""
    telegram_target = TelegramConfig("123456:ABCDEF", "-1009876543210")
    client = TelegramBrokerClient(
        telegram_target,
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    identity = load_or_create_broker_identity(client.target_state_dir, broker_label="old")
    persist_broker_listen_url(client.target_state_dir, "http://127.0.0.1:7456")
    shutdown_calls = []

    async def fake_fetch_health(listen_url):
        return TelegramBrokerHealth(
            broker_id=identity.broker_id,
            broker_label=identity.broker_label,
            listen_url=listen_url,
            target_key=resolve_telegram_target_key(telegram_target),
            version="0.0.0",
        )

    async def fake_shutdown_broker(listen_url):
        shutdown_calls.append(listen_url)
        return True

    monkeypatch.setattr(client, "_fetch_health", fake_fetch_health)
    monkeypatch.setattr(client, "_shutdown_broker", fake_shutdown_broker)

    result = asyncio.run(client._probe_persisted_broker(replace_mismatched=True))

    assert result is None
    assert shutdown_calls == ["http://127.0.0.1:7456"]


def test_broker_client_read_only_probe_does_not_shutdown_mismatched_broker(
    monkeypatch,
    tmp_path,
):
    """Keep broker replacement serialized by the startup lock."""
    telegram_target = TelegramConfig("123456:ABCDEF", "-1009876543210")
    client = TelegramBrokerClient(
        telegram_target,
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    identity = load_or_create_broker_identity(client.target_state_dir, broker_label="old")
    persist_broker_listen_url(client.target_state_dir, "http://127.0.0.1:7456")

    async def fake_fetch_health(listen_url):
        return TelegramBrokerHealth(
            broker_id=identity.broker_id,
            broker_label=identity.broker_label,
            listen_url=listen_url,
            target_key=resolve_telegram_target_key(telegram_target),
            version="0.0.0",
        )

    async def fail_shutdown(_listen_url):
        raise AssertionError("Read-only probe should not shut down mismatched brokers")

    monkeypatch.setattr(client, "_fetch_health", fake_fetch_health)
    monkeypatch.setattr(client, "_shutdown_broker", fail_shutdown)

    result = asyncio.run(client._probe_persisted_broker(replace_mismatched=False))

    assert result is None


def test_broker_client_refuses_when_mismatched_broker_cannot_shutdown(
    monkeypatch,
    tmp_path,
):
    """Avoid starting a competing broker when the old broker cannot shut down."""
    telegram_target = TelegramConfig("123456:ABCDEF", "-1009876543210")
    client = TelegramBrokerClient(
        telegram_target,
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    identity = load_or_create_broker_identity(client.target_state_dir, broker_label="old")
    persist_broker_listen_url(client.target_state_dir, "http://127.0.0.1:7456")

    async def fake_fetch_health(listen_url):
        return TelegramBrokerHealth(
            broker_id=identity.broker_id,
            broker_label=identity.broker_label,
            listen_url=listen_url,
            target_key=resolve_telegram_target_key(telegram_target),
            version="0.0.0",
        )

    async def fake_shutdown_broker(listen_url):
        return False

    monkeypatch.setattr(client, "_fetch_health", fake_fetch_health)
    monkeypatch.setattr(client, "_shutdown_broker", fake_shutdown_broker)

    with pytest.raises(TelegramPromptError, match="Tell the user to stop"):
        asyncio.run(client._probe_persisted_broker(replace_mismatched=True))


def test_broker_client_reuses_same_version_broker(monkeypatch, tmp_path):
    """Keep using persisted brokers that match the current package version."""
    telegram_target = TelegramConfig("123456:ABCDEF", "-1009876543210")
    client = TelegramBrokerClient(
        telegram_target,
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    identity = load_or_create_broker_identity(client.target_state_dir, broker_label="current")
    persist_broker_listen_url(client.target_state_dir, "http://127.0.0.1:7456")
    health = TelegramBrokerHealth(
        broker_id=identity.broker_id,
        broker_label=identity.broker_label,
        listen_url="http://127.0.0.1:7456",
        target_key=resolve_telegram_target_key(telegram_target),
        version=__version__,
    )

    async def fake_fetch_health(_listen_url):
        return health

    async def fail_shutdown(_listen_url):
        raise AssertionError("Same-version broker should not be shut down")

    monkeypatch.setattr(client, "_fetch_health", fake_fetch_health)
    monkeypatch.setattr(client, "_shutdown_broker", fail_shutdown)

    assert asyncio.run(client._probe_persisted_broker()) == health


def test_broker_client_target_state_dir_is_per_target(tmp_path):
    """Separate brokers by Telegram target so different bots can coexist locally."""
    first_client = TelegramBrokerClient(
        TelegramConfig("111:AAA", "-1001"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    second_client = TelegramBrokerClient(
        TelegramConfig("222:BBB", "-1001"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )

    assert first_client.target_state_dir != second_client.target_state_dir
