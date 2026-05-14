# Red Alert Notifier

Real-time Telegram listener that monitors Israeli Red Alert (Tzeva Adom) channels and forwards matching alerts to a webhook. Filters by region keywords and handles message correlation, deduplication, edit detection, and interception follow-ups.

Built to solve a specific problem: Red Alert Telegram channels are noisy. This filters by your region and pushes only relevant alerts to your own notification pipeline (n8n, Home Assistant, etc.).

## How It Works

```
Telegram Red Alert Channel
        │
        ▼
   ┌──────────┐
   │ listener  │  Event-driven + 10s fallback poll
   │  .py      │  Keyword matching (region-based)
   │           │  Message correlation & deduplication
   └────┬─────┘
        │
        ▼
   n8n Webhook ──► Push notification / Home Assistant / etc.
```

1. Connects to a Telegram group via [Telethon](https://github.com/LonamiWebs/Telethon) (userbot API)
2. Listens for new messages and edits using both event handlers (instant) and a fallback poller (catches missed messages)
3. Matches messages against configurable region keywords and required trigger phrases
4. Correlates split messages — some alerts arrive as trigger + location in separate messages
5. Cleans junk (links, share prompts, emojis) and deduplicates within a 5-minute window
6. Forwards matched alerts as JSON to a webhook endpoint

## Features

- **Dual delivery** — event-driven for instant alerts + polling fallback for reliability
- **Region filtering** — Hebrew keyword matching with false-positive protection
- **Message correlation** — combines split trigger + location messages within a 30s window
- **Edit detection** — re-evaluates edited messages for new keyword matches
- **Interception tracking** — forwards "intercepted" follow-ups within 30 minutes of an alert
- **Text deduplication** — suppresses duplicate content within a 5-minute window
- **Docker healthcheck** — built-in liveness check with configurable threshold
- **Auto-reconnect** — recovers from disconnections with exponential backoff
- **CI/CD** — GitHub Actions builds and pushes to GHCR on every commit

## Tech Stack

- **Python 3.12** with async/await
- **Telethon** — Telegram client library
- **aiohttp** — async HTTP for webhook delivery
- **Docker** — containerized deployment with health checks
- **GitHub Actions** — CI/CD pipeline to GHCR

## Setup

### Prerequisites

- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- A webhook endpoint (n8n, Make, Zapier, or any HTTP endpoint)
- Docker (recommended) or Python 3.12+

### Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `TELEGRAM_API_ID` | Telegram API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | Telegram API hash |
| `TELEGRAM_GROUP` | Group invite hash (`+XXXXX`) or numeric chat ID |
| `N8N_WEBHOOK_URL` | Webhook endpoint for forwarded alerts |
| `TEST` | Enable test mode (`true`/`false`) |
| `TEST_GROUP` | Test group for debugging |

### Run with Docker (recommended)

```bash
docker compose up -d
```

On first run, Telethon will prompt for your phone number and auth code. Run interactively first:

```bash
docker compose run --rm redalert-notifier
```

The session is persisted in `./session/` so subsequent starts are automatic.

### Run locally

```bash
pip install -r requirements.txt
python listener.py
```

## Webhook Payload

```json
{
  "text": "Cleaned alert text",
  "matched_keywords": ["נתניה", "השרון"],
  "sender": "Alert_Channel",
  "message_id": 12345,
  "timestamp": "2026-01-15T10:30:00+00:00",
  "received_at": "2026-01-15T10:30:01+00:00",
  "group": "+ABCDefgh12345678",
  "is_edit": false,
  "context": "Previous alert text (if follow-up)"
}
```

## License

[MIT](LICENSE)
