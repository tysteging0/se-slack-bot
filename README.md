# SE Ticket Digest Bot

A Flask-based Slack bot that surfaces open Salesforce tickets to the SE team each day and makes it easy to take action directly from Slack.

## What it does

- **Daily Digest** (4pm MT, weekdays) — Fetches all open SE Request tickets from Salesforce and posts an interactive summary to Slack with one-tap action buttons.
- **Recap** (5pm MT, weekdays) — Posts a closing summary showing what was actioned that day, with a per-SE breakdown sent to the manager.
- **Morning Nudge** (9am MT, weekdays) — DMs SEs about tickets that were still open at end of day yesterday.
- **Slack Actions** — Handles button clicks inline: close tickets, send DMs to customers, mark tickets as still-active, or use Claude to draft a solution.

## Stack

| Layer | Tech |
|---|---|
| Web server | Flask + Gunicorn |
| Hosting | Render (`se-card-game` service) |
| Slack integration | `slack-sdk` |
| Salesforce | `simple-salesforce` + OAuth refresh token |
| Scheduling | launchd (macOS) — curls the `/cron/*` endpoints on schedule |

## Setup

### Environment variables

Set these in the Render dashboard (or `.env` for local dev). See `.env.example` for the exact format.

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` from Slack App → OAuth & Permissions |
| `SLACK_SIGNING_SECRET` | From Slack App → Basic Information |
| `CRON_SECRET` | Any strong random string — must match the value in the launchd plists |
| `SFDX_AUTH_URL` | `force://clientId:clientSecret:refreshToken@instance` — from `sf org display --verbose` |
| `SESSION_DIR` | Set to `/data` on Render (persistent disk); omit for local dev |

### Local development

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in values
python app.py
```

### Scheduling (launchd)

Cron jobs are triggered by launchd agents on the host Mac, which POST to the Render service with an `X-Cron-Secret` header. The plists live in `~/Library/LaunchAgents/`:

| Plist | Endpoint | Time |
|---|---|---|
| `com.gusto.se-digest.plist` | `POST /cron/digest` | 4:00pm MT weekdays |
| `com.gusto.se-recap.plist` | `POST /cron/recap` | 5:00pm MT weekdays |
| `com.gusto.se-nudge.plist` | `POST /cron/nudge` | 9:00am MT weekdays |

To reload after changes:
```bash
launchctl unload ~/Library/LaunchAgents/com.gusto.se-digest.plist
launchctl load  ~/Library/LaunchAgents/com.gusto.se-digest.plist
```

## Project structure

```
app.py          Flask routes and cron handlers
blocks.py       Slack Block Kit message builders
digest.py       Salesforce ticket fetch + processing logic
recap.py        Manager recap builder
sf_client.py    Salesforce OAuth session + SOQL helpers
sf_writes.py    Salesforce write operations (close, update, chatter, etc.)
logic_map.py    Ticket routing / priority logic
```
