#!/usr/bin/env python3
"""
se-slack-bot/app.py

Production Flask server for the SE Ticket Digest bot.

Routes:
  POST /slack/actions   — Slack button clicks + modal submissions
  POST /cron/digest     — Triggered by cron-job.org at 4:00pm MT (23:00 UTC) weekdays
  POST /cron/recap      — Triggered by cron-job.org at 5:00pm MT (00:00 UTC) weekdays
  POST /cron/nudge      — Triggered by cron-job.org at 9:00am MT (16:00 UTC) weekdays
                          (fires NEXT morning — checks YESTERDAY's incomplete tickets)
  GET  /health          — Render health check

Cron-job.org schedule (UTC, weekdays only Mon–Fri):
  /cron/digest  →  23:00 UTC  (4pm MT standard / 3pm MT daylight)
  /cron/recap   →  00:00 UTC  (5pm MT standard / 4pm MT daylight)
  /cron/nudge   →  16:00 UTC  (9am MT standard / 8am MT daylight)

Environment variables required (set in Render dashboard):
  SLACK_BOT_TOKEN       xoxb-...
  SLACK_SIGNING_SECRET  from Slack App settings
  CRON_SECRET           any strong random string, matches cron-job.org header
  SF_USERNAME
  SF_PASSWORD
  SF_SECURITY_TOKEN
  SF_DOMAIN             "login" (production) or "test" (sandbox)
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
from datetime import date, timedelta

from flask import Flask, abort, jsonify, request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from blocks import (
    block_close_confirmed,
    block_close_ticket_modal,
    block_connected_yes,
    block_digest_complete,
    block_dm_sent,
    block_not_yet,
    block_one_tap_confirm,
    block_one_tap_confirmed,
    block_opening,
    block_still_active_modal,
    block_update_confirmed,
)
from digest import (
    OWNERS,
    fetch_open_tickets,
    load_action_log_today,
    load_nudge_data,
    load_reviewed_today,
    process_tickets,
    save_digest_tickets,
)
from recap import MANAGER, build_recap_blocks
from sf_writes import (
    handle_close_ticket,
    handle_dm_send,
    handle_dm_skip,
    handle_one_tap_active,
    handle_still_active,
)

app   = Flask(__name__)
slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
CRON_SECRET          = os.environ.get("CRON_SECRET", "")

# ── In-memory digest session ──────────────────────────────────────────────────
# { owner_name: { tickets, index, total, channel_id } }
# Resets each afternoon when /cron/digest fires.
_sessions: dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slack_user_to_owner(slack_user_id: str) -> str | None:
    """Reverse-map a Slack user ID to an SE name."""
    for name, cfg in OWNERS.items():
        if cfg.get("slack_id") == slack_user_id:
            return name
    return None


def _dm_channel(user_id: str) -> str:
    """Open (or retrieve) a DM channel with a user."""
    r = slack.conversations_open(users=user_id)
    return r["channel"]["id"]


def _send(channel: str, blocks: list, text: str = "SE Ticket Digest") -> None:
    slack.chat_postMessage(channel=channel, blocks=blocks, text=text)


def _open_modal(trigger_id: str, view: dict) -> None:
    slack.views_open(trigger_id=trigger_id, view=view)


def _slack_id_for_email(email: str) -> str | None:
    """Look up a Slack user ID from a work email address."""
    if not email:
        return None
    try:
        r = slack.users_lookupByEmail(email=email)
        return r["user"]["id"]
    except SlackApiError as e:
        print(f"[WARN] Slack user not found for {email}: {e}")
        return None


def _ticket_by_id(owner_name: str, ticket_id: str) -> dict | None:
    session = _sessions.get(owner_name, {})
    for t in session.get("tickets", []):
        if t["id"] == ticket_id:
            return t
    return None


# ── Digest flow ───────────────────────────────────────────────────────────────

def _send_next_ticket(owner_name: str) -> None:
    """Send the next unanswered ticket card to the SE's DM."""
    session = _sessions.get(owner_name)
    if not session:
        return

    tickets = session["tickets"]
    idx     = session["index"]
    channel = session["channel_id"]
    total   = session["total"]
    first   = OWNERS[owner_name]["first_name"]

    if idx >= len(tickets):
        _send(channel, block_digest_complete(first, total))
        return

    ticket = tickets[idx]
    if ticket.get("use_one_tap"):
        blocks = block_one_tap_confirm(first, total, idx + 1, ticket)
    else:
        blocks = block_opening(first, total, idx + 1, ticket)

    _send(channel, blocks)


def _advance(owner_name: str) -> None:
    """Increment the session index and send the next card."""
    session = _sessions.get(owner_name)
    if session:
        session["index"] += 1
        _send_next_ticket(owner_name)


# ── Slack signature verification ──────────────────────────────────────────────

def _verify_slack(req: request) -> None:
    ts  = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if not ts or abs(time.time() - int(ts)) > 300:
        abort(403)
    base     = f"v0:{ts}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        abort(403)


def _verify_cron(req: request) -> None:
    if req.headers.get("X-Cron-Secret") != CRON_SECRET:
        abort(403)


# ── Action router ─────────────────────────────────────────────────────────────

def _handle_block_action(payload: dict) -> None:
    """Route a button click to the right handler."""
    action     = payload["actions"][0]
    action_id  = action["action_id"]
    trigger_id = payload.get("trigger_id", "")
    user_id    = payload["user"]["id"]
    channel    = payload["channel"]["id"]
    owner_name = _slack_user_to_owner(user_id)

    # Parse prefix and ticket_id from action_id (e.g. "connected_yes__Ticket-00864090")
    parts     = action_id.split("__", 1)
    prefix    = parts[0]
    ticket_id = parts[1] if len(parts) > 1 else ""
    ticket    = _ticket_by_id(owner_name, ticket_id) if owner_name else {}

    if prefix == "connected_yes":
        _send(channel, block_connected_yes(ticket))

    elif prefix == "connected_no":
        _send(channel, block_not_yet(ticket))

    elif prefix in ("status_active", "one_tap_update"):
        _open_modal(trigger_id, block_still_active_modal(ticket))

    elif prefix in ("status_close", "one_tap_close"):
        _open_modal(trigger_id, block_close_ticket_modal(ticket))

    elif prefix == "one_tap_active":
        handle_one_tap_active(ticket, owner_name)
        _send(channel, block_one_tap_confirmed(ticket))
        _advance(owner_name)

    elif prefix == "dm_send":
        se_first   = OWNERS.get(owner_name, {}).get("first_name", "")
        opp_email  = (ticket or {}).get("opp_owner_email", "")
        opp_slack_id = _slack_id_for_email(opp_email)

        def _send_opp_dm(uid, msg):  # uid unused — we resolved via email above
            if not opp_slack_id:
                print(f"[WARN] No Slack ID for opp owner email={opp_email!r}, skipping DM")
                return False
            _send(_dm_channel(opp_slack_id), [
                {"type": "section", "text": {"type": "mrkdwn", "text": msg}}
            ])
            return True

        handle_dm_send(ticket, owner_name, slack_fn=_send_opp_dm)
        _send(channel, block_dm_sent(ticket, se_first))
        _advance(owner_name)

    elif prefix == "dm_skip":
        handle_dm_skip(ticket, se_name=owner_name)
        _advance(owner_name)


def _handle_view_submission(payload: dict) -> None:
    """Route a modal submission to the right handler."""
    callback_id = payload["view"]["callback_id"]
    user_id     = payload["user"]["id"]
    owner_name  = _slack_user_to_owner(user_id)
    channel     = _dm_channel(user_id)

    parts     = callback_id.split("__", 1)
    prefix    = parts[0]
    ticket_id = parts[1] if len(parts) > 1 else ""
    ticket    = _ticket_by_id(owner_name, ticket_id) if owner_name else {}

    values = payload["view"]["state"]["values"]

    if prefix == "submit_update":
        note = values["next_steps_input"]["next_steps_text"]["value"]
        handle_still_active(ticket, owner_name, note)
        _send(channel, block_update_confirmed(ticket, note))
        _advance(owner_name)

    elif prefix == "submit_close":
        note = values["resolution_input"]["resolution_text"]["value"]
        handle_close_ticket(ticket, owner_name, note)
        _send(channel, block_close_confirmed(ticket, note))
        _advance(owner_name)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/slack/actions")
def slack_actions():
    _verify_slack(request)

    # Slack sends payload as URL-encoded form data
    raw     = request.form.get("payload", "{}")
    payload = json.loads(raw)
    ptype   = payload.get("type")

    if ptype == "block_actions":
        _handle_block_action(payload)
    elif ptype == "view_submission":
        _handle_view_submission(payload)

    # Slack requires a 200 within 3 seconds
    return "", 200


@app.post("/cron/test")
def cron_test():
    """
    Test endpoint — fetches real tickets for all SEs but sends everything
    to a single target Slack ID (e.g. the manager). Protected by CRON_SECRET.
    Usage: POST /cron/test  with header X-Cron-Secret and body {"slack_id": "U02JH87UQQP"}
    """
    _verify_cron(request)
    target_slack_id = (request.get_json(silent=True) or {}).get("slack_id", MANAGER["slack_id"])
    channel = _dm_channel(target_slack_id)

    for owner_name, cfg in OWNERS.items():
        try:
            raw     = fetch_open_tickets(owner_name)
            tickets = process_tickets(raw)

            _send(channel, [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*🧪 TEST — {owner_name}'s digest ({len(tickets)} tickets)*"}
            }])

            if not tickets:
                _send(channel, [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"No open tickets for {cfg['first_name']} today."}
                }])
                continue

            _sessions[owner_name] = {
                "tickets":    tickets,
                "index":      0,
                "total":      len(tickets),
                "channel_id": channel,
            }
            _send_next_ticket(owner_name)

        except Exception as e:
            print(f"[ERROR] Test digest failed for {owner_name}: {e}")
            _send(channel, [{"type": "section", "text": {"type": "mrkdwn", "text": f"❌ Error fetching {owner_name}: {e}"}}])

    return jsonify({"status": "test sent", "target": target_slack_id})


@app.post("/cron/digest")
def cron_digest():
    """
    Called by cron-job.org at 4:00pm MT (23:00 UTC) weekdays.
    Sends the afternoon digest to all SEs.
    """
    _verify_cron(request)

    for owner_name, cfg in OWNERS.items():
        try:
            slack_id = cfg["slack_id"]
            channel  = _dm_channel(slack_id)
            raw      = fetch_open_tickets(owner_name)
            tickets  = process_tickets(raw)

            if not tickets:
                _send(channel, [{
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"Hey {cfg['first_name']} 👋  No open tickets today — enjoy the rest of the day!"}
                }])
                continue

            _sessions[owner_name] = {
                "tickets":    tickets,
                "index":      0,
                "total":      len(tickets),
                "channel_id": channel,
            }

            # Persist ticket IDs so the next-morning nudge knows what was sent
            save_digest_tickets(owner_name, [t["id"] for t in tickets])

            _send_next_ticket(owner_name)

        except Exception as e:
            print(f"[ERROR] Digest failed for {owner_name}: {e}")

    return jsonify({"status": "digest sent"})


@app.post("/cron/recap")
def cron_recap():
    """
    Called by cron-job.org at 5:00pm MT (00:00 UTC) weekdays.
    Sends manager DM recap of what each SE answered during today's digest.
    """
    _verify_cron(request)

    recap_date = date.today().strftime("%B %-d, %Y")
    log        = load_action_log_today()
    blocks     = build_recap_blocks(log, recap_date)

    try:
        channel = _dm_channel(MANAGER["slack_id"])
        _send(channel, blocks, text=f"SE Digest Recap — {recap_date}")
    except SlackApiError as e:
        print(f"[ERROR] Recap DM failed: {e}")
        return jsonify({"status": "error", "detail": str(e)}), 500

    return jsonify({"status": "recap sent"})


@app.post("/cron/nudge")
def cron_nudge():
    """
    Called by cron-job.org at 9:00am MT (16:00 UTC) weekdays.
    Checks YESTERDAY's digest for tickets the SE never answered, and sends
    a gentle reminder if any remain outstanding.
    """
    _verify_cron(request)

    yesterday   = date.today() - timedelta(days=1)
    nudge_data  = load_nudge_data(yesterday)   # {owner_name: n_remaining}

    for owner_name, n in nudge_data.items():
        cfg = OWNERS.get(owner_name)
        if not cfg:
            continue
        try:
            channel = _dm_channel(cfg["slack_id"])
            _send(channel, [{
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": (
                             f"👋  Good morning, {cfg['first_name']}! You still have "
                             f"*{n}* ticket{'s' if n != 1 else ''} "
                             f"left from yesterday's digest. "
                             f"Your recap sends at 5pm — want to knock these out first?"
                         )}
            }])
        except Exception as e:
            print(f"[ERROR] Nudge failed for {owner_name}: {e}")

    return jsonify({"status": "nudges sent", "nudged": list(nudge_data.keys())})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Local dev only — use gunicorn in production
    app.run(port=3000, debug=True)
