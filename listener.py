import os
import re
import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedBanError,
)
from telethon.tl.functions.messages import CheckChatInviteRequest

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Suppress noisy Telethon update logs
logging.getLogger("telethon").setLevel(logging.WARNING)

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
GROUP = os.environ["TELEGRAM_GROUP"]
WEBHOOK_URL = os.environ["N8N_WEBHOOK_URL"]

KEYWORDS = [
    "השרון", "לשרון", "בשרון", "שרון",
    "נתניה", "לנתניה", "בנתניה",
    "כפר יונה", "בכפר יונה", "לכפר יונה",
    "מרכז", "המרכז", "למרכז", "במרכז",
    'איו"ש', "שומרון", "בשומרון", "לשומרון",
    "גוש דן", "בגוש דן", "לגוש דן",
]
REQUIRED_PHRASES = ["מקור האיום", "יציאות", "צפי אזעקות", "שיגורים", "איום לישראל", "זוהה", "גם"]
INTERCEPTION_PHRASES = ["יורט"]
FOLLOWUP_WINDOW = 30 * 60  # seconds — interception alerts sent only within this window after a match
POLL_INTERVAL = 10  # fallback poll interval in seconds
WEBHOOK_RETRIES = 3
TEXT_DEDUP_WINDOW = 5 * 60  # seconds — suppress duplicate text content within this window
HEALTHCHECK_FILE = Path("/tmp/healthcheck")

TEST_MODE = os.environ.get("TEST", "false").lower() == "true"
TEST_GROUP = os.environ.get("TEST_GROUP", "")
SESSION_PATH = os.environ.get("SESSION_PATH", "session/telegram")

# Track processed messages: id -> text (to detect edits)
MAX_TRACKED_MESSAGES = 200
processed_messages: OrderedDict[int, str] = OrderedDict()

# Track recently forwarded text content: text -> timestamp (to suppress duplicate text)
recent_texts: dict[str, float] = {}

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
http_session = None

# Track last sent alert for follow-up context
last_alert_text: str = ""
last_alert_time: datetime | None = None


def should_process(message_id: int, text: str) -> bool:
    """Returns True if this message+text combo hasn't been processed yet."""
    prev = processed_messages.get(message_id)
    return prev != text


def mark_processed(message_id: int, text: str):
    processed_messages[message_id] = text
    processed_messages.move_to_end(message_id)
    while len(processed_messages) > MAX_TRACKED_MESSAGES:
        processed_messages.popitem(last=False)


def update_healthcheck():
    HEALTHCHECK_FILE.write_text(str(time.time()))


JUNK_PATTERNS = [
    re.compile(r'https?://\S+'),
    re.compile(r't\.me/\S+'),
    re.compile(r'.*התרעה חריגה.*'),
    re.compile(r'.*התראות לפני כולם.*'),
    re.compile(r'.*לשיתוף ב.*לחצו כאן.*'),
    re.compile(r'.*לשיתוף ב\s*\u200f?WhatsApp.*'),
    re.compile(r'.*לחצו כאן.*💬.*'),
    re.compile(r'^\s*Telegram\s*$'),
    re.compile(r'^\s*Image\s*$'),
]


def clean_message(text: str) -> str:
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        skip = False
        for pattern in JUNK_PATTERNS:
            if pattern.search(line):
                if pattern.pattern in (r'https?://\S+', r't\.me/\S+'):
                    line = pattern.sub('', line).strip()
                    if not line:
                        skip = True
                else:
                    skip = True
                    break
        if not skip:
            cleaned.append(line)
    result = '\n'.join(cleaned).strip()
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = re.sub(r'[\U0001F300-\U0001FAFF\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0000200D\U00002600-\U000026FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF]+', '', result)
    result = re.sub(r'[\U0001F1E0-\U0001F1FF]{2}', '', result)
    result = re.sub(r'  +', ' ', result)
    return result


def matches_keywords(text: str) -> list[str]:
    # Interception alerts — no location keyword needed, but require recent alert
    if any(p in text for p in INTERCEPTION_PHRASES):
        if last_alert_time is not None:
            elapsed = (datetime.now(timezone.utc) - last_alert_time).total_seconds()
            if elapsed <= FOLLOWUP_WINDOW:
                return ["יורט"]
        return []

    if not any(phrase in text for phrase in REQUIRED_PHRASES):
        return []
    return [kw for kw in KEYWORDS if kw in text]


async def resolve_invite(group_str: str):
    group = group_str.strip()
    try:
        return int(group)
    except ValueError:
        pass
    if group.startswith("+"):
        invite_hash = group[1:]
        result = await client(CheckChatInviteRequest(invite_hash))
        if hasattr(result, "chat"):
            log.info("Resolved invite link to chat: %s (id: %s)", result.chat.title, result.chat.id)
            return result.chat
        raise RuntimeError(f"Cannot resolve invite link — you may not have joined this group yet. Result: {result}")
    return group


async def resolve_groups():
    groups = [await resolve_invite(GROUP)]
    if TEST_MODE and TEST_GROUP:
        log.info("TEST mode enabled — also listening to test group")
        groups.append(await resolve_invite(TEST_GROUP))
    return groups


async def send_to_webhook(payload: dict) -> None:
    for attempt in range(1, WEBHOOK_RETRIES + 1):
        try:
            async with http_session.post(WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status < 400:
                    log.info("Sent to webhook (status %s)", resp.status)
                    return
                body = await resp.text()
                log.error("Webhook returned %s: %s (attempt %s/%s)", resp.status, body, attempt, WEBHOOK_RETRIES)
        except Exception:
            log.exception("Webhook request failed (attempt %s/%s)", attempt, WEBHOOK_RETRIES)
        if attempt < WEBHOOK_RETRIES:
            wait = 2 ** attempt
            log.info("Retrying in %ss...", wait)
            await asyncio.sleep(wait)
    log.error("All %s webhook attempts failed", WEBHOOK_RETRIES)


async def healthcheck_loop():
    while True:
        update_healthcheck()
        await asyncio.sleep(30)


async def process_message(msg, is_edit: bool = False):
    global last_alert_text, last_alert_time

    text = msg.text or ""
    if not text or not should_process(msg.id, text):
        return False

    matched = matches_keywords(text)
    mark_processed(msg.id, text)

    if not matched:
        return False

    cleaned_text = clean_message(text)

    # Suppress duplicate text content forwarded within the dedup window
    now = time.time()
    # Evict expired entries
    for k in list(recent_texts.keys()):
        if now - recent_texts[k] > TEXT_DEDUP_WINDOW:
            del recent_texts[k]
    if cleaned_text in recent_texts:
        log.info("Skipping duplicate text (msg %s, last seen %.0fs ago)", msg.id, now - recent_texts[cleaned_text])
        return False
    recent_texts[cleaned_text] = now

    action = "EDIT" if is_edit else "NEW"

    payload = {
        "text": cleaned_text,
        "matched_keywords": matched,
        "sender": "Alert_Channel",
        "message_id": msg.id,
        "timestamp": msg.date.isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "group": str(GROUP),
        "is_edit": is_edit,
        "context": last_alert_text if last_alert_text else None,
    }

    log.info("[%s] Matched %s in msg %s: %s", action, matched, msg.id, text[:80])
    await send_to_webhook(payload)

    # Update last alert context (not for interceptions — they are follow-ups, not the alert itself)
    if "יורט" not in matched:
        last_alert_text = cleaned_text
        last_alert_time = datetime.now(timezone.utc)

    return True


async def poll_loop(chats, min_ids: dict):
    """Fallback poller — catches anything events missed (archived group, flaky connection)."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        update_healthcheck()
        for chat in chats:
            key = id(chat)
            try:
                messages = await client.get_messages(chat, limit=10, min_id=min_ids[key])
                if messages:
                    for msg in reversed(messages):
                        min_ids[key] = max(min_ids[key], msg.id)
                        await process_message(msg, is_edit=False)
            except Exception:
                log.exception("Poll error for chat %s", key)


async def main():
    global http_session

    while True:
        if http_session is None or http_session.closed:
            http_session = aiohttp.ClientSession()
        healthcheck_task = None
        poll_task = None
        on_new = None
        on_edit = None
        try:
            await client.start()

            chats = await resolve_groups()
            log.info("Listening to %d group(s) — events + %ss fallback poll", len(chats), POLL_INTERVAL)
            log.info("Keywords: %s", KEYWORDS)
            log.info("Webhook: %s", WEBHOOK_URL)

            # Seed cache and min_ids with recent messages
            min_ids = {}
            for chat in chats:
                msgs = await client.get_messages(chat, limit=5)
                min_ids[id(chat)] = msgs[0].id if msgs else 0
                for msg in msgs:
                    if msg.text:
                        mark_processed(msg.id, msg.text)

            # Event handlers — instant delivery
            @client.on(events.NewMessage(chats=chats))
            async def on_new(event):
                await process_message(event.message, is_edit=False)

            @client.on(events.MessageEdited(chats=chats))
            async def on_edit(event):
                await process_message(event.message, is_edit=True)

            # Fallback poller — safety net
            poll_task = asyncio.create_task(poll_loop(chats, min_ids))
            healthcheck_task = asyncio.create_task(healthcheck_loop())
            update_healthcheck()

            await client.run_until_disconnected()

        except (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError) as e:
            log.critical("Session is dead (%s). Delete session/ folder and re-authenticate.", type(e).__name__)
            raise SystemExit(1)

        except Exception:
            log.exception("Disconnected — reconnecting in 10s...")
            await asyncio.sleep(10)

        finally:
            if on_new:
                client.remove_event_handler(on_new)
            if on_edit:
                client.remove_event_handler(on_edit)
            if poll_task:
                poll_task.cancel()
            if healthcheck_task:
                healthcheck_task.cancel()
            if http_session and not http_session.closed:
                await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
