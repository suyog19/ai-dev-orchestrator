import os
import re
import json
import logging
import urllib.request

logger = logging.getLogger("orchestrator")

_APPROVAL_RE = re.compile(r"^(APPROVE|REJECT|REGENERATE)\s+(\d+)$", re.IGNORECASE)


def parse_approval_command(text: str) -> tuple[str, int] | None:
    """Parse an approval command from Telegram message text.

    Returns ("APPROVE"|"REJECT"|"REGENERATE", run_id) or None if not a valid command.
    """
    m = _APPROVAL_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))


def set_webhook(webhook_url: str) -> dict:
    """Register webhook_url with the Telegram Bot API. Returns Telegram's JSON response."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    url = f"https://api.telegram.org/bot{token}/setWebhook"
    data = json.dumps({"url": webhook_url}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def send_message(event: str, status: str, details: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
        return

    env_name = os.environ.get("ENV_NAME", "").strip()
    prefix = f"[{env_name}] " if env_name else ""
    text = f"{prefix}[Orchestrator]\nEvent: {event}\nStatus: {status}\nDetails: {details}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()

    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        logger.info("Telegram notification sent: %s / %s", event, status)
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)
