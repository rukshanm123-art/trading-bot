"""Notification failure isolation and read-only monitoring endpoints."""

from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.request
from typing import Any

import pytest
import requests

from trading_bot.core.enums import ComponentHealth
from trading_bot.monitoring.health import HEALTH
from trading_bot.monitoring.metrics import METRICS
from trading_bot.monitoring.server import MonitoringServer
from trading_bot.notifications.adapters import (
    ConsoleNotifier,
    EmailNotifier,
    NotificationHub,
    Notifier,
    TelegramNotifier,
)
from trading_bot.security.secrets import StaticSecretProvider


def test_console_notifier_logs_and_metrics_get_returns_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert ConsoleNotifier().send("subject", "body", "critical") is True
    assert "NOTIFY [CRITICAL] subject" in caplog.text

    METRICS.inc("unit_counter", 3)
    METRICS.set_gauge("unit_gauge", 12)
    assert METRICS.get("unit_counter") == 3
    assert METRICS.get("unit_gauge") == 12
    assert METRICS.get("missing_metric") == 0


def test_telegram_notifier_requires_configured_secrets() -> None:
    notifier = TelegramNotifier(StaticSecretProvider({}))

    assert notifier.send("subject", "body") is False


def test_telegram_notifier_success_and_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Resp:
        status_code = 200

    def ok_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return Resp()

    monkeypatch.setattr("trading_bot.notifications.adapters.requests.post", ok_post)
    notifier = TelegramNotifier(
        StaticSecretProvider(
            {"TELEGRAM_BOT_TOKEN": "telegram-token-012345", "TELEGRAM_CHAT_ID": "123"}
        )
    )

    assert notifier.send("subject", "body", "critical") is True
    assert calls[0]["url"].endswith("/sendMessage")
    assert calls[0]["json"]["parse_mode"] == "Markdown"

    def failing_post(url, json, timeout):
        raise requests.RequestException("down")

    monkeypatch.setattr("trading_bot.notifications.adapters.requests.post", failing_post)
    assert notifier.send("subject", "body") is False


def test_email_notifier_success_missing_config_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    assert EmailNotifier(StaticSecretProvider({})).send("subject", "body") is False

    events: list[tuple[str, Any]] = []

    class SMTP:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def sendmail(self, sender, recipients, msg):
            events.append(("sendmail", sender, recipients, msg))

    monkeypatch.setattr("trading_bot.notifications.adapters.smtplib.SMTP", SMTP)
    notifier = EmailNotifier(
        StaticSecretProvider(
            {
                "SMTP_HOST": "smtp.invalid",
                "SMTP_PORT": "2525",
                "SMTP_FROM": "bot@example.invalid",
                "SMTP_TO": "ops@example.invalid",
                "SMTP_USERNAME": "user",
                "SMTP_PASSWORD": "smtp-password-012345",
            }
        )
    )

    assert notifier.send("subject", "body", "warning") is True
    assert ("starttls",) in events
    assert any(event[0] == "sendmail" for event in events)

    class FailingSMTP(SMTP):
        def sendmail(self, sender, recipients, msg):
            raise smtplib.SMTPException("mail down")

    monkeypatch.setattr("trading_bot.notifications.adapters.smtplib.SMTP", FailingSMTP)
    assert notifier.send("subject", "body") is False


def test_notification_hub_records_broken_adapter_as_false() -> None:
    class Broken(Notifier):
        name = "broken"

        def send(self, subject: str, body: str, severity: str = "info") -> bool:
            raise RuntimeError("broken notifier")

    class Working(Notifier):
        name = "working"

        def send(self, subject: str, body: str, severity: str = "info") -> bool:
            return True

    assert NotificationHub([Broken(), Working()]).send("s", "b") == {
        "broken": False,
        "working": True,
    }


def _request(
    port: int, path: str, token: str | None = None, method: str = "GET"
) -> tuple[int, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_monitoring_server_is_read_only_and_token_gated() -> None:
    HEALTH.update(database=ComponentHealth.OK, application=ComponentHealth.OK)
    HEALTH.note("component", "ok")
    METRICS.inc("orders_total", 2)
    METRICS.set_gauge("equity", 30)

    server = MonitoringServer("127.0.0.1", 0, token="monitor-token")
    server.start()
    assert server._server is not None
    port = int(server._server.server_address[1])
    try:
        status, body = _request(port, "/health/live")
        assert status == 401
        assert "unauthorized" in body

        status, body = _request(port, "/health/live", "monitor-token")
        assert status == 200
        assert json.loads(body) == {"live": True}

        status, body = _request(port, "/health/ready", "monitor-token")
        assert status == 200
        assert json.loads(body) == {"ready": True}

        status, body = _request(port, "/health", "monitor-token")
        assert status == 200
        assert json.loads(body)["notes"] == {"component": "ok"}

        status, body = _request(port, "/metrics", "monitor-token")
        assert status == 200
        assert "orders_total 2.0" in body
        assert "equity 30.0" in body

        status, body = _request(port, "/missing", "monitor-token")
        assert status == 404
        assert "not found" in body

        status, body = _request(port, "/health", "monitor-token", method="POST")
        assert status == 405
        assert "read-only" in body
    finally:
        server.stop()
