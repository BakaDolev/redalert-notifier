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
POLL_INTERVAL = 3   # seconds between polls
WEBHOOK_RETRIES = 3
HEALTHCHECK_FILE = Path("/tmp/healthcheck")

TEST_MODE = os.environ.get("TEST", "false").lower() == "true"
TEST_GROUP = os.environ.get("TEST_GROUP", "")
SESSION_PATH = os.environ.get("SESSION_PATH", "session/telegram")

# Track processed message IDs to avoid duplicates
MAX_TRACKED_MESSAGES = 200
processed_messages: OrderedDict[int, bool] = OrderedDict()

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
http_session = None


def is_processed(message_id: int) -> bool:
    return message_id in processed_messages


def mark_processed(message_id: int):
    processed_messages[message_id] = True
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


async def poll_chat(chat, min_id: int) -> int:
    """Poll for new messages in a chat since min_id. Returns the new highest message ID seen."""
    messages = await client.get_messages(chat, limit=10, min_id=min_id)
    if not messages:
        return min_id

    new_max_id = min_id
    for msg in reversed(messages):  # oldest first
        new_max_id = max(new_max_id, msg.id)
        text = msg.text or ""
        if not text or is_processed(msg.id):
            continue
        matched = matches_keywords(text)
        if not matched:
            continue
        mark_processed(msg.id)
        cleaned_text = clean_message(text)
        payload = {
            "text": cleaned_text,
            "matched_keywords": matched,
            "sender": "Alert_Channel",
            "message_id": msg.id,
            "timestamp": msg.date.isoformat(),
            "received_at": datetime.now(timezone.utc).isoformat(),
            "group": str(GROUP),
        }
        log.info("Matched keywords %s in message %s: %s", matched, msg.id, text[:80])
        await send_to_webhook(payload)

    return new_max_id


async def main():
    global http_session
    http_session = aiohttp.ClientSession()

    while True:
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

            asyncio.create_task(healthcheck_loop())
            update_healthcheck()

            while True:
                await asyncio.sleep(POLL_INTERVAL)
                update_healthcheck()
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
            if http_session and not http_session.closed:
                await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
