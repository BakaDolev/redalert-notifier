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
from telethon import TelegramClient
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
REQUIRED_PHRASES = ["מקור האיום", "יציאות", "צפי אזעקות"]
POLL_INTERVAL = 3
WEBHOOK_RETRIES = 3
HEALTHCHECK_FILE = Path("/tmp/healthcheck")

TEST_MODE = os.environ.get("TEST", "false").lower() == "true"
TEST_GROUP = os.environ.get("TEST_GROUP", "")
SESSION_PATH = os.environ.get("SESSION_PATH", "session/telegram")

# Track processed messages: id -> text hash (to detect edits)
MAX_TRACKED_MESSAGES = 200
processed_messages: OrderedDict[int, str] = OrderedDict()

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
http_session = None


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
    """Check a message against keywords and send to webhook if matched."""
    text = msg.text or ""
    if not text:
        return False

    if not should_process(msg.id, text):
        return False

    matched = matches_keywords(text)
    if not matched:
        # Still mark non-matching messages so we re-check if text changes
        mark_processed(msg.id, text)
        return False

    mark_processed(msg.id, text)
    cleaned_text = clean_message(text)

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
    }

    log.info("[%s] Matched keywords %s in message %s: %s", action, matched, msg.id, text[:80])
    await send_to_webhook(payload)
    return True


async def poll_chat(chat, min_id: int) -> int:
    """Poll for new messages and check recent messages for edits."""
    # 1. Check for NEW messages
    messages = await client.get_messages(chat, limit=10, min_id=min_id)
    new_max_id = min_id

    if messages:
        for msg in reversed(messages):
            new_max_id = max(new_max_id, msg.id)
            await process_message(msg, is_edit=False)

    # 2. Re-check last 5 messages for edits (text changes)
    recent = await client.get_messages(chat, limit=5)
    if recent:
        for msg in recent:
            await process_message(msg, is_edit=True)

    return new_max_id


async def main():
    global http_session

    while True:
        if http_session is None or http_session.closed:
            http_session = aiohttp.ClientSession()
        healthcheck_task = None
        try:
            await client.start()

            chats = await resolve_groups()
            log.info("Polling %d group(s) every %ss", len(chats), POLL_INTERVAL)
            log.info("Keywords: %s", KEYWORDS)
            log.info("Webhook: %s", WEBHOOK_URL)

            # Seed min_id with the latest message so we only process new ones
            min_ids = {}
            for chat in chats:
                msgs = await client.get_messages(chat, limit=1)
                min_ids[id(chat)] = msgs[0].id if msgs else 0
                # Pre-populate processed cache for recent messages
                recent = await client.get_messages(chat, limit=5)
                for msg in recent:
                    if msg.text:
                        mark_processed(msg.id, msg.text)

            healthcheck_task = asyncio.create_task(healthcheck_loop())
            update_healthcheck()

            poll_count = 0
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                update_healthcheck()
                poll_count += 1

                # Log every 100 polls (~5 min) to confirm bot is alive
                if poll_count % 100 == 0:
                    log.info("Poll #%d — still running, connection: %s", poll_count, client.is_connected())

                # Reconnect if connection dropped
                if not client.is_connected():
                    log.warning("Connection lost, reconnecting...")
                    await client.connect()

                for chat in chats:
                    key = id(chat)
                    min_ids[key] = await poll_chat(chat, min_ids[key])

        except (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError) as e:
            log.critical("Session is dead (%s). Delete session/ folder and re-authenticate.", type(e).__name__)
            raise SystemExit(1)

        except Exception:
            log.exception("Disconnected — reconnecting in 10s...")
            await asyncio.sleep(10)

        finally:
            if healthcheck_task:
                healthcheck_task.cancel()
            if http_session and not http_session.closed:
                await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
