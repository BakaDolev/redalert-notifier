# Red Alert Notifier

Telegram listener that monitors a channel for red alert (rocket attack) messages and forwards matching ones to an n8n webhook.

## Architecture

Single-file Python app (`listener.py`) using Telethon to listen to Telegram channels. Runs in Docker, deployed on Unraid. Docker image is auto-built via GitHub Actions and published to `ghcr.io/bakadolev/redalert-notifier:latest`.

## How message matching works

1. Message must contain the **required phrase** `מקור האיום` (confirms it's an actual alert, not news)
2. Then it checks for **location keywords** (Hebrew, with prefix variations for ב/ל/ה)
3. Both conditions must be true to trigger the webhook

## Keywords

Keywords are **hardcoded** in `listener.py`, not in env vars. When adding a new location, always include Hebrew prefix variations:
- Base word: `שרון`
- With ב (in): `בשרון`
- With ל (to): `לשרון`
- With ה (the): `השרון`

## Environment variables

Only sensitive/deployment-specific values are env vars (in `.env`, gitignored):
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — Telegram API credentials
- `TELEGRAM_GROUP` — invite hash of the channel to monitor
- `N8N_WEBHOOK_URL` — webhook endpoint
- `TEST` — `true`/`false`, enables listening to test group
- `TEST_GROUP` — invite hash of the test group
- `SESSION_PATH` — path to Telethon session file

Everything else (keywords, required phrase, retries, healthcheck path) is hardcoded.

## Deployment

- Docker image auto-published to GHCR on push to `master`
- Deployed on Unraid via Docker GUI
- Session volume must be persisted at `/app/session`

## Rules

- Never commit `.env` — it contains API keys
- Always push changes so GitHub Actions builds a new image
- When editing keywords, include all Hebrew prefix variations (ב/ל/ה)
- Telethon logger is set to WARNING to avoid log spam — don't change this
- The `REQUIRED_PHRASE` filter is critical — without it, location keywords match unrelated news messages
