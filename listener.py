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
]
REQUIRED_PHRASES = ["מקור האיום", "יציאות", "צפי אזעקות"]
WEBHOOK_RETRIES = 3
HEALTHCHECK_FILE = Path("/tmp/healthcheck")

TEST_MODE = os.environ.get("TEST", "false").lower() == "true"
TEST_GROUP = os.environ.get("TEST_GROUP", "")
SESSION_PATH = os.environ.get("SESSION_PATH", "session/telegram")

# Track processed messages to avoid duplicates
# Key: message_id, Value: set of matched keywords that were already sent
MAX_TRACKED_MESSAGES = 50  # Keep last 50 messages in memory
processed_messages: OrderedDict[int, set[str]] = OrderedDict()

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

# Global session for webhook reuse
http_session = None


def track_message(message_id: int, keywords: list[str]) -> list[str]:
    """
    Track a processed message and return only NEW keywords not previously sent.
    This prevents spam when a message is edited multiple times.
    """
    global processed_messages
    
    keywords_set = set(keywords)
    
    if message_id in processed_messages:
        # Get keywords we haven't sent yet for this message
        already_sent = processed_messages[message_id]
        new_keywords = keywords_set - already_sent
        
        if new_keywords:
            # Update with new keywords
            processed_messages[message_id].update(new_keywords)
            # Move to end (most recent)
            processed_messages.move_to_end(message_id)
            return list(new_keywords)
        else:
            # All keywords were already sent - skip
            return []
    else:
        # New message - track it
        processed_messages[message_id] = keywords_set
        
        # Prune old messages if we exceed the limit
        while len(processed_messages) > MAX_TRACKED_MESSAGES:
            processed_messages.popitem(last=False)
        
        return keywords


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

    log.error("All %s webhook attempts failed for message: %s", WEBHOOK_RETRIES, payload.get("text", "")[:80])


async def healthcheck_loop():
    while True:
        update_healthcheck()
        await asyncio.sleep(30)


async def main():
    global http_session
    http_session = aiohttp.ClientSession()
    
    while True:
        try:
            await client.start()
            
            # Prime the cache to ensure instant push notifications instead of slow polling
            log.info("Fetching dialogs to prime the entity cache for real-time updates...")
            await client.get_dialogs()

            chats = await resolve_groups()
            log.info("Listening to %d group(s)", len(chats))
            log.info("Keywords: %s", KEYWORDS)
            log.info("Webhook: %s", WEBHOOK_URL)

            # Handler for both new messages AND edited messages
            @client.on(events.NewMessage(chats=chats))
            @client.on(events.MessageEdited(chats=chats))
            async def handler(event):
                is_edit = isinstance(event, events.MessageEdited.Event)
                
                # 1. Zero-latency memory checks
                text = event.message.text or ""
                if not text:
                    return

                matched = matches_keywords(text)
                if not matched:
                    return

                # 2. Check if we already processed this message with these keywords
                new_keywords = track_message(event.message.id, matched)
                if not new_keywords:
                    log.debug("Skipping message %s - already processed with same keywords", event.message.id)
                    return

                # 3. Fire-and-forget background task
                async def process_and_send():
                    try:
                        cleaned_text = clean_message(text)

                        payload = {
                            "text": cleaned_text,
                            "matched_keywords": new_keywords,
                            "sender": "Alert_Channel",
                            "message_id": event.message.id,
                            "timestamp": event.message.date.isoformat(),
                            "received_at": datetime.now(timezone.utc).isoformat(),
                            "group": str(GROUP),
                            "is_edit": is_edit,
                        }

                        action = "EDIT" if is_edit else "NEW"
                        log.info("[%s] Matched keywords %s in message %s: %s", 
                                 action, new_keywords, event.message.id, text[:80])
                        await send_to_webhook(payload)
                    except Exception as e:
                        log.error("Failed to process and send message %s: %s", event.message.id, e)

                asyncio.create_task(process_and_send())

            asyncio.create_task(healthcheck_loop())
            update_healthcheck()
            await client.run_until_disconnected()

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