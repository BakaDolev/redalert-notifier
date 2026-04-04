# Red Alert Notifier

Telegram listener that monitors a channel for red alert (rocket attack) messages and forwards matching ones to an n8n webhook.

## Architecture

Single-file Python app (`listener.py`) using Telethon to listen to Telegram channels. Runs in Docker, deployed on Unraid. Docker image is auto-built via GitHub Actions and published to `ghcr.io/bakadolev/redalert-notifier:latest`.

## How message matching works

1. Message must contain at least one **trigger phrase** (confirms it's an actual alert)
2. Then it checks for **location keywords** (Hebrew, with prefix variations for ב/ל/ה)
3. Both conditions must be true to trigger the webhook
4. **Exception — interceptions (`יורט`)**: no location keyword needed, but requires a matching alert to have been sent within the last 30 minutes
5. **Exception — `גם` follow-ups**: only triggers if a matching alert was sent within the last 30 minutes (prevents false positives like weather forecasts)

## Trigger phrases

Hardcoded in `listener.py` as `REQUIRED_PHRASES`:
- `מקור האיום` — structured alert format
- `יציאות` — launch detected
- `צפי אזעקות` — siren forecast
- `שיגורים` — launches
- `איום לישראל` — threat to Israel
- `זוהה` — detected
- `גם` — follow-up addition (requires recent alert within 30min)

## Interception alerts

`יורט` is handled separately via `INTERCEPTION_PHRASES`. No location keyword required — sent only if a relevant alert was fired within `FOLLOWUP_WINDOW` (30 minutes).

## Follow-up context

When a message is sent, the previous alert text is included in the webhook payload as `context`. This allows n8n to show the original alert alongside follow-up messages like `גם לשרון`.

## Keywords

Keywords are **hardcoded** in `listener.py`, not in env vars. When adding a new location, always include Hebrew prefix variations:
- Base word: `שרון`
- With ב (in): `בשרון`
- With ל (to): `לשרון`
- With ה (the): `השרון`

## Message delivery

Hybrid approach — both methods run simultaneously:
- **Events** (`NewMessage` + `MessageEdited`) — instant delivery via Telegram MTProto push
- **Polling every 10s** — fallback safety net for archived groups, flaky connections, or missed events

Deduplication via `processed_messages` (tracks `message_id → text`) ensures no double-sends even if both fire for the same message.

## Message cleaning

Before sending to webhook, messages are cleaned:
- URLs and t.me links removed
- Promo lines (התרעה חריגה, התראות לפני כולם, WhatsApp share) removed
- Emojis stripped
- Standalone "Telegram" / "Image" artifacts removed

## Environment variables

Only sensitive/deployment-specific values are env vars (in `.env`, gitignored):
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — Telegram API credentials
- `TELEGRAM_GROUP` — invite hash of the channel to monitor
- `N8N_WEBHOOK_URL` — webhook endpoint
- `TEST` — `true`/`false`, enables listening to test group
- `TEST_GROUP` — invite hash of the test group
- `SESSION_PATH` — path to Telethon session file

Everything else (keywords, trigger phrases, retries, healthcheck path, poll interval) is hardcoded.

## Deployment

- Docker image auto-published to GHCR on push to `master`
- Deployed on Unraid via Docker GUI
- Session volume must be persisted at `/app/session`

## Rules

- **Keep this file up to date** — update CLAUDE.md whenever changes affect project structure, matching logic, env vars, deployment, or conventions
- Never commit `.env` — it contains API keys
- Always push changes so GitHub Actions builds a new image
- When editing keywords, include all Hebrew prefix variations (ב/ל/ה)
- Telethon logger is set to WARNING to avoid log spam — don't change this
- The trigger phrase + keyword filter is critical — without it, location keywords match unrelated messages
