import os
import json
import logging
import urllib.request

logger = logging.getLogger("orchestrator")


def send_message(event: str, status: str, details: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
        return

    text = f"[Orchestrator]\nEvent: {event}\nStatus: {status}\nDetails: {details}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()

    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        logger.info("Telegram notification sent: %s / %s", event, status)
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)
