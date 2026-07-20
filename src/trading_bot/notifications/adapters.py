"""Notification adapters: console, email (SMTP), Telegram.

Secrets come exclusively from the environment via SecretProvider and are
registered with the redactor, so they cannot leak into logs. Notification
failure never crashes the trading loop — it is recorded and surfaced in
health notes instead.
"""

from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText

import requests

from trading_bot.security.secrets import SecretProvider

log = logging.getLogger(__name__)


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, subject: str, body: str, severity: str = "info") -> bool: ...

    def verify_connectivity(self) -> bool:
        """Read-only credential/connectivity probe used by the LIVE gate."""
        return False


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, subject: str, body: str, severity: str = "info") -> bool:
        level = logging.CRITICAL if severity == "critical" else logging.INFO
        log.log(level, "NOTIFY [%s] %s\n%s", severity.upper(), subject, body)
        return True

    def verify_connectivity(self) -> bool:
        return True


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, secrets: SecretProvider) -> None:
        self._secrets = secrets

    def send(self, subject: str, body: str, severity: str = "info") -> bool:
        token = self._secrets.get("TELEGRAM_BOT_TOKEN")
        chat_id = self._secrets.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            log.warning("telegram notifier enabled but TELEGRAM_* env vars missing")
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"*{subject}*\n{body}"[:4000],
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException as exc:
            log.error("telegram send failed: %s", type(exc).__name__)
            return False

    def verify_connectivity(self) -> bool:
        """Verify the bot token can access the configured chat without sending."""
        token = self._secrets.get("TELEGRAM_BOT_TOKEN")
        chat_id = self._secrets.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return False
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": chat_id},
                timeout=10,
            )
            if resp.status_code != 200:
                return False
            payload = resp.json()
            return isinstance(payload, dict) and payload.get("ok") is True
        except (requests.RequestException, ValueError):
            return False


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, secrets: SecretProvider, use_tls: bool = True) -> None:
        self._secrets = secrets
        self._use_tls = use_tls

    def send(self, subject: str, body: str, severity: str = "info") -> bool:
        host = self._secrets.get("SMTP_HOST")
        sender = self._secrets.get("SMTP_FROM")
        recipient = self._secrets.get("SMTP_TO")
        if not host or not sender or not recipient:
            log.warning("email notifier enabled but SMTP_* env vars missing")
            return False
        port = int(self._secrets.get("SMTP_PORT") or "587")
        username = self._secrets.get("SMTP_USERNAME")
        password = self._secrets.get("SMTP_PASSWORD")
        msg = MIMEText(body)
        msg["Subject"] = f"[trading-bot {severity}] {subject}"
        msg["From"] = sender
        msg["To"] = recipient
        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if self._use_tls:
                    smtp.starttls()
                if username and password:
                    smtp.login(username, password)
                smtp.sendmail(sender, [recipient], msg.as_string())
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.error("email send failed: %s", type(exc).__name__)
            return False

    def verify_connectivity(self) -> bool:
        """Verify SMTP connection, TLS and optional authentication without mail."""
        host = self._secrets.get("SMTP_HOST")
        sender = self._secrets.get("SMTP_FROM")
        recipient = self._secrets.get("SMTP_TO")
        if not host or not sender or not recipient:
            return False
        try:
            port = int(self._secrets.get("SMTP_PORT") or "587")
        except ValueError:
            return False
        username = self._secrets.get("SMTP_USERNAME")
        password = self._secrets.get("SMTP_PASSWORD")
        if bool(username) != bool(password):
            return False
        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                if self._use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if username and password:
                    smtp.login(username, password)
                code, _message = smtp.noop()
            return 200 <= int(code) < 300
        except (smtplib.SMTPException, OSError, ValueError):
            return False


class NotificationHub:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, subject: str, body: str, severity: str = "info") -> dict[str, bool]:
        results: dict[str, bool] = {}
        for n in self.notifiers:
            try:
                results[n.name] = n.send(subject, body, severity)
            except Exception as exc:  # a broken notifier must never stop trading safety
                log.error("notifier %s raised %s", n.name, type(exc).__name__)
                results[n.name] = False
        return results

    def verify_external_connectivity(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for notifier in self.notifiers:
            if isinstance(notifier, ConsoleNotifier):
                continue
            try:
                results[notifier.name] = notifier.verify_connectivity()
            except Exception:
                results[notifier.name] = False
        return results


def build_notification_hub(cfg, secrets: SecretProvider) -> NotificationHub:
    """Build the configured hub consistently for runtime and unlock checks."""
    notifiers: list[Notifier] = [ConsoleNotifier()] if cfg.notifications.console else []
    if cfg.notifications.telegram.enabled:
        notifiers.append(TelegramNotifier(secrets))
    if cfg.notifications.email.enabled:
        notifiers.append(EmailNotifier(secrets, cfg.notifications.email.use_tls))
    return NotificationHub(notifiers)
