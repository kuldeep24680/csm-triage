"""Operational alerts: posts to a Slack-compatible incoming webhook.

Set ``TRIAGE_ALERT_WEBHOOK_URL``. Without it, alerts are only logged at
ERROR level (still visible in logs / your log alerting).
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("triage.alerts")


def send(title: str, details: dict | None = None) -> bool:
    details = details or {}
    lines = [f"*Triage alert:* {title}"] + [f"• {k}: {v}" for k, v in details.items() if v is not None]
    text = "\n".join(lines)
    log.error("ALERT %s %s", title, details)
    if not settings.alert_webhook_url:
        return False
    try:
        httpx.post(settings.alert_webhook_url, json={"text": text}, timeout=10).raise_for_status()
        return True
    except Exception:
        log.exception("could not deliver alert")
        return False
