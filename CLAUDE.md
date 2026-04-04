# Red Alert Notifier

Telegram listener that monitors a channel for red alert (rocket attack) messages and forwards matching ones to an n8n webhook.

## Architecture

Single-file Python app (`listener.py`) using Telethon to listen to Telegram channels. It uses a **Hybrid Event + Polling approach**: Telethon pushes events (`events.NewMessage`) for zero-latency triggers, while a fallback loop polls every 10 seconds to catch edge cases and dropped connections.
Runs in Docker, deployed on Unraid. Docker image is auto-built via GitHub Actions and published to `ghcr.io/bakadolev/redalert-notifier:latest`.

## How message matching works

1. **Standard Alerts:** 
   - Message must contain at least one **trigger phrase**: `מקור האיום`, `יציאות`, `צפי אזעקות`, `שיגורים`, `איום לישראל`, `זוהה`, or `גם`.
   - Then it checks for **location keywords** (Hebrew, with prefix variations for ב/ל/ה).
   - Both conditions must be true to trigger the webhook.

2. **Interception Follow-ups:**
   - If a message contains an interception phrase (`יורט`).
   - It checks if a valid standard alert was sent within the `FOLLOWUP_WINDOW` (30 minutes). If true, it triggers the webhook with the keyword `["יורט"]` and attaches the previous alert text as payload `context`.

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

Everything else (keywords, required phrases, interception logic, retries, healthcheck path) is hardcoded.

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
