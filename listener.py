import os
import re
import asyncio
import logging
import time
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
]
REQUIRED_PHRASES = ["מקור האיום", "יציאות", "צפי אזעקות"]
WEBHOOK_RETRIES = 3
HEALTHCHECK_FILE = Path("/tmp/healthcheck")

TEST_MODE = os.environ.get("TEST", "false").lower() == "true"
TEST_GROUP = os.environ.get("TEST_GROUP", "")
SESSION_PATH = os.environ.get("SESSION_PATH", "session/telegram")

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)


def update_healthcheck():
    """Write current timestamp to healthcheck file so Docker can verify we're alive."""
    HEALTHCHECK_FILE.write_text(str(time.time()))


JUNK_PATTERNS = [
    re.compile(r'https?://\S+'),                # URLs
    re.compile(r't\.me/\S+'),                    # t.me links without https
    re.compile(r'.*התרעה חריגה.*'),              # header line
    re.compile(r'.*התראות לפני כולם.*'),         # promo line
    re.compile(r'.*לשיתוף ב.*לחצו כאן.*'),      # WhatsApp share line
    re.compile(r'.*לשיתוף ב\s*\u200f?WhatsApp.*'),  # WhatsApp share variant
    re.compile(r'.*לחצו כאן.*💬.*'),             # "click here" promo
    re.compile(r'^\s*Telegram\s*$'),             # standalone "Telegram"
    re.compile(r'^\s*Image\s*$'),                # standalone "Image"
]


def clean_message(text: str) -> str:
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        skip = False
        for pattern in JUNK_PATTERNS:
            if pattern.search(line):
                # For URL patterns, remove just the URL from the line instead of the whole line
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
    # Collapse multiple blank lines into one
    result = re.sub(r'\n{3,}', '\n\n', result)
    # Remove emojis
    result = re.sub(r'[\U0001F300-\U0001FAFF\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0000200D\U00002600-\U000026FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF]+', '', result)
    # Remove flag emojis (regional indicators)
    result = re.sub(r'[\U0001F1E0-\U0001F1FF]{2}', '', result)
    # Clean up extra spaces left behind
    result = re.sub(r'  +', ' ', result)
    return result


def matches_keywords(text: str) -> list[str]:
    if REQUIRED_PHRASES and not any(phrase in text for phrase in REQUIRED_PHRASES):
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
            async with aiohttp.ClientSession() as session:
                async with session.post(WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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

    log.error("All %s webhook attempts failed for message: %s", WEBHOOK_RETRIES, payload.get("text", "")[:80])


async def healthcheck_loop():
    """Periodically update the healthcheck file to prove the event loop is alive."""
    while True:
        update_healthcheck()
        await asyncio.sleep(30)


async def main():
    while True:
        try:
            await client.start()

            chats = await resolve_groups()
            log.info("Listening to %d group(s)", len(chats))
            log.info("Keywords: %s", KEYWORDS)
            log.info("Webhook: %s", WEBHOOK_URL)

            @client.on(events.NewMessage(chats=chats))
            async def handler(event):
                text = event.message.text or ""
                if not text:
                    return

                matched = matches_keywords(text)
                if not matched:
                    return

                sender = await event.get_sender()
                sender_name = getattr(sender, "first_name", "") or getattr(sender, "title", "") or "Unknown"

                cleaned_text = clean_message(text)

                payload = {
                    "text": cleaned_text,
                    "matched_keywords": matched,
                    "sender": sender_name,
                    "message_id": event.message.id,
                    "timestamp": event.message.date.isoformat(),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "group": str(GROUP),
                }

                log.info("Matched keywords %s in message: %s", matched, text[:80])
                await send_to_webhook(payload)

            asyncio.create_task(healthcheck_loop())
            update_healthcheck()
            await client.run_until_disconnected()

        except (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError) as e:
            log.critical("Session is dead (%s). Delete session/ folder and re-authenticate.", type(e).__name__)
            raise SystemExit(1)

        except Exception:
            log.exception("Disconnected — reconnecting in 10s...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
