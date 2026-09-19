# SHIV  Bot

Python-only Telegram bot for TeraBox and DiskWalla links, direct media links,
Telegram file saving, quotas, referrals, UPI payment review, and admin tools.
The old React website/dashboard has been removed completely.

## Features

- `/start`, `/help`, `/stats`, `/plan`, `/premium`, `/referral`, and `/myfiles`
- TeraBox and DiskWalla resolver adapters
- Direct public video, audio, image, and PDF links
- Force-join verification, referral tracking, and daily quotas
- Telegram uploads saved with private `start=file_<id>` links
- UPI QR generation plus admin approve/reject workflow
- Admin commands: `/pending`, `/approve`, `/reject`, `/addpremium`,
  `/removepremium`, `/refreshpremium`, `/checkuser`, `/broadcast`
- Premium refresh automatically resets expired paid plans and writes an audit
  record to the logger chat and rotating local log file
- SQLite database, download-size limits, concurrency limits, SSRF protection,
  auto-delete timers, custom Telegram Premium emoji support, structured
  logging, and deployment files

## Project layout

```text
SHIV_BOT/
  SHIV_BOT.py          # Telegram entrypoint
  SHIV_config.py       # environment-backed settings
  SHIV_database.py     # SQLite data layer
  SHIV_extractors.py   # safe URL/resolver adapters
  .env.example
tests/
  test_shiv_bot.py
```

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Get `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).
3. Copy `.env.example` to `.env` and set at least:

   ```env
   BOT_TOKEN=your_botfather_token
   API_ID=123456
   API_HASH=your_api_hash
   ADMIN_IDS=123456789
   OWNER_ID=123456789
   ```

4. Install and run:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   python -m SHIV_BOT.SHIV_BOT
   ```

   Or use `./run-bot.sh`.

The bot deliberately fails fast with a clear configuration error when required
Telegram values are missing. This is preferable to a worker that starts but
never responds.

## Premium emoji and logger setup

The bot supports Telegram custom emoji entities without making up IDs. Add
verified numeric IDs from your Telegram Premium emoji pack to `.env`:

```env
CUSTOM_EMOJI_IDS=premium:1234567890123456789,success:1234567890123456790,logger:1234567890123456791,refresh:1234567890123456792
LOG_FILE=data/shiv_deltatera.log
```

Supported keys include `welcome`, `success`, `error`, `download`, `upload`,
`premium`, `broadcast`, `logger`, `refresh`, `admin`, and `stats`. If the
setting is empty, the bot uses suitable Unicode fallbacks, so it still looks
correct on every account.

`/broadcast your message` retries once after Telegram flood-wait responses,
counts delivery failures, and writes the final sent/failed totals to the
logger chat. `/refreshpremium` lets an admin clean expired premium records
manually; the background watcher also performs this refresh automatically.

## TeraBox/DiskWalla resolver

Those providers change private endpoints and access rules frequently. This
repository does not contain fake extraction URLs or unauthorized scraping.
Configure a resolver service that you own or are authorized to use:

```env
TERABOX_API_URL=https://your-authorized-resolver.example/terabox
DISKWALLA_API_URL=https://your-authorized-resolver.example/diskwalla
```

The bot sends:

```json
{"url": "https://example-share-link"}
```

The service must return JSON like:

```json
{
  "direct_url": "https://cdn.example/file.mp4",
  "title": "file.mp4",
  "mime": "video/mp4",
  "size": 123456
}
```

Without an endpoint, the bot responds to the user with a clear setup message;
it never sends a made-up download link.

## Testing

The included offline test suite covers:

- Python compilation/imports
- SQLite user, quota, payment, and ownership flows
- direct-link validation and resolver configuration errors
- configuration validation

Run:

```bash
python -m unittest discover -s tests -v
```

Live Telegram delivery still requires the user's real BotFather token and
Telegram credentials, so that final network check must be performed after
deployment.

## Deployment

The root `Dockerfile`, `Procfile`, and `railway.json` all start:

```bash
python -m SHIV_BOT.SHIV_BOT
```

Persist the `data/` directory so the SQLite database survives restarts. For
multiple workers, replace SQLite with a shared database before scaling.
