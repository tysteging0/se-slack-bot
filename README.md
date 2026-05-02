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
| Hosting | Render |
| Slack integration | `slack-sdk` |
| Salesforce | `simple-salesforce` |
| Scheduling | cron-job.org (hits `/cron/*` endpoints) |

## Setup

### Environment variables

Set these in your Render dashboard (or `.env` for local dev):

```
SLACK_BOT_TOKEN        xoxb-...
SLACK_SIGNING_SECRET   from Slack App settings
CRON_SECRET            any strong random string, must match cron-job.org X-Cron-Secret header
SF_USERNAME
SF_PASSWORD
SF_SECURITY_TOKEN
SF_DOMAIN              "login" (production) or "test" (sandbox)
```

### Local development

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in values
python app.py
```

### Cron endpoints

Configure cron-job.org to POST to your Render URL with the `X-Cron-Secret` header:

| Endpoint | UTC Schedule | Purpose |
|---|---|---|
| `/cron/digest` | 23:00 Mon–Fri | Daily ticket digest |
| `/cron/recap` | 00:00 Tue–Sat | End-of-day recap |
| `/cron/nudge` | 16:00 Mon–Fri | Morning nudge for yesterday's open tickets |

## Project structure

```
app.py              Flask routes and cron handlers
blocks.py           Slack Block Kit message builders
digest.py           Salesforce ticket fetch + processing logic
recap.py            Manager recap builder
dashboard_refresh.py  Ticket trend dashboard refresh
sf_client.py        Salesforce session helpers
sf_writes.py        Salesforce write operations (close, update, etc.)
```
