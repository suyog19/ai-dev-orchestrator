import os
import re
import json
import logging
import urllib.request

logger = logging.getLogger("orchestrator")

_APPROVAL_RE = re.compile(r"^(APPROVE|REJECT|REGENERATE)\s+(\d+)(?:\s+.*)?$", re.IGNORECASE | re.DOTALL)
_CLARIFICATION_RE = re.compile(
    r"^(ANSWER|CANCEL|CLARIFY)\s+(\d+)(?:\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)


def parse_approval_command(text: str) -> tuple[str, int] | None:
    """Parse an approval command from Telegram message text.

    Returns ("APPROVE"|"REJECT"|"REGENERATE", run_id) or None if not a valid command.
    """
    m = _APPROVAL_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))


def parse_clarification_command(text: str) -> tuple[str, int, str | None] | None:
    """Parse ANSWER/CANCEL/CLARIFY commands from Telegram text.

    Returns (command, clarification_id, answer_text_or_None) or None.
    """
    m = _CLARIFICATION_RE.match(text.strip())
    if not m:
        return None
    cmd = m.group(1).upper()
    cid = int(m.group(2))
    answer = m.group(3).strip() if m.group(3) else None
    return cmd, cid, answer


def send_clarification_request(clarification: dict) -> str | None:
    """Send a clarification question to Telegram.

    Returns the Telegram message_id string if sent, else None.
    Formats the message with question, numbered options, and reply instructions.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping clarification notification")
        return None

    env_name = os.environ.get("ENV_NAME", "").strip()
    prefix = f"[{env_name}] " if env_name else ""

    cid = clarification["id"]
    run_id = clarification.get("run_id", "?")
    issue_key = clarification.get("issue_key") or "?"
    question = clarification.get("question", "")
    options = clarification.get("options") or []

    options_block = ""
    if options:
        numbered = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
        options_block = f"\nOptions:\n{numbered}\n"

    text = (
        f"{prefix}clarification_required\n\n"
        f"Run: {run_id}\n"
        f"Issue: {issue_key}\n\n"
        f"Question:\n{question}\n"
        f"{options_block}\n"
        f"Reply:\n"
        f"ANSWER {cid} <your answer>\n"
        f"CANCEL {cid}\n"
        f"CLARIFY {cid}"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()

    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            telegram_message_id = str(result.get("result", {}).get("message_id", ""))
            logger.info("Clarification sent to Telegram: clarification_id=%s", cid)
            return telegram_message_id or None
    except Exception as exc:
        logger.error("Failed to send clarification to Telegram: %s", exc)
        return None


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
