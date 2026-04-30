#!/usr/bin/env python3
"""
se-slack-bot/recap.py

End-of-day managerial recap.

Reads today's action log from session state and sends a DM summary
to the manager covering what each SE answered across their digest.

Run to preview:
    python3 recap.py --dry-run
    python3 recap.py --send        (fires the actual Slack DM when live)
"""

import argparse
import json
import subprocess
from datetime import date
from typing import Optional

from digest import OWNERS, SF_ORG, load_action_log_today


# ── Config ────────────────────────────────────────────────────────────────────

MANAGER = {
    "name":     "Tyler Steging",
    "slack_id": "U02JH87UQQP",
}

ACTION_LABELS = {
    "still_active":    ("🔄", "Still Active"),
    "one_tap_active":  ("✅", "Confirmed Active"),   # one-tap, no new note
    "close":           ("✅", "Closed"),
    "dm_send":         ("📨", "DM Sent"),
    "skip":            ("⏭️",  "Skipped"),
}


# ── Formatting ────────────────────────────────────────────────────────────────

def format_mrr(amount: float) -> str:
    if not amount:
        return ""
    return f"  ·  💰 ${amount:,.0f}"


def build_recap_blocks(log: list, recap_date: str) -> list:
    """
    Build Slack Block Kit blocks for the managerial recap DM.
    Groups entries by SE, shows action + note for each ticket.
    """
    from blocks import divider, section, header, context

    if not log:
        return [
            header(f"SE Digest Recap — {recap_date}"),
            section("No tickets were reviewed today."),
        ]

    # Group by SE
    by_se: dict[str, list] = {}
    for entry in log:
        se = entry.get("se") or "Unknown"
        by_se.setdefault(se, []).append(entry)

    # Count totals
    total_reviewed  = len(log)
    total_closed    = sum(1 for e in log if e.get("action") == "close")
    total_active    = sum(1 for e in log if e.get("action") == "still_active")
    total_dm        = sum(1 for e in log if e.get("action") == "dm_send")
    total_skipped   = sum(1 for e in log if e.get("action") == "skip")

    blocks = [
        header(f"📊  SE Digest Recap — {recap_date}"),
        section(
            f"*{total_reviewed}* ticket{'s' if total_reviewed != 1 else ''} reviewed today  ·  "
            f"*{total_closed}* closed  ·  "
            f"*{total_active}* still active  ·  "
            f"*{total_dm}* DM{'s' if total_dm != 1 else ''} sent  ·  "
            f"*{total_skipped}* skipped"
        ),
        divider(),
    ]

    for se_name, entries in by_se.items():
        first = OWNERS.get(se_name, {}).get("first_name", se_name.split()[0])
        se_closed  = sum(1 for e in entries if e.get("action") == "close")
        se_active  = sum(1 for e in entries if e.get("action") == "still_active")
        se_dm      = sum(1 for e in entries if e.get("action") == "dm_send")
        se_skipped = sum(1 for e in entries if e.get("action") == "skip")

        blocks.append(section(
            f"*{se_name}*  ·  {len(entries)} ticket{'s' if len(entries) != 1 else ''} reviewed  ·  "
            f"{se_closed} closed · {se_active} active · {se_dm} DM · {se_skipped} skipped"
        ))

        for e in entries:
            emoji, label = ACTION_LABELS.get(e.get("action", ""), ("·", e.get("action", "")))
            mrr = format_mrr(e.get("opp_amount", 0))
            ticket_line = (
                f"{emoji}  *{e['ticket_id']}*  ·  {e.get('account', '')}  "
                f"·  _{e.get('opp_stage', '')}_{mrr}"
            )
            note = e.get("note", "").strip()
            if note:
                ticket_line += f"\n>_{label}_: {note}"
            else:
                ticket_line += f"\n>_{label}_"

            blocks.append(section(ticket_line))

        blocks.append(divider())

    # Remove trailing divider, replace with context footer
    if blocks and blocks[-1].get("type") == "divider":
        blocks.pop()

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"SE Tickets Bot  ·  {recap_date}"}]
    })

    return blocks


def format_recap_text(log: list, recap_date: str) -> str:
    """Plain-text version for dry-run preview."""
    if not log:
        return f"SE Digest Recap — {recap_date}\nNo tickets reviewed today."

    by_se: dict[str, list] = {}
    for entry in log:
        se = entry.get("se") or "Unknown"
        by_se.setdefault(se, []).append(entry)

    lines = [f"📊  SE Digest Recap — {recap_date}", ""]

    total = len(log)
    closed = sum(1 for e in log if e.get("action") == "close")
    lines.append(f"{total} tickets reviewed  ·  {closed} closed")
    lines.append("━" * 50)

    for se_name, entries in by_se.items():
        lines.append(f"\n{se_name}  ({len(entries)} tickets)")
        for e in entries:
            emoji, label = ACTION_LABELS.get(e.get("action", ""), ("·", ""))
            mrr = format_mrr(e.get("opp_amount", 0))
            lines.append(
                f"  {emoji}  {e['ticket_id']}  ·  {e.get('account','')}  "
                f"·  {e.get('opp_stage','')}{mrr}  [{e.get('time','')}]"
            )
            note = e.get("note", "").strip()
            if note:
                lines.append(f"       {label}: \"{note}\"")
            else:
                lines.append(f"       {label}")

    return "\n".join(lines)


# ── Slack send ────────────────────────────────────────────────────────────────

def send_recap_dm(blocks: list, dry_run: bool = True) -> bool:
    """
    Send the recap as a Slack DM to the manager.
    When dry_run=True, prints the payload instead of sending.
    """
    payload = {"blocks": blocks}

    if dry_run:
        print(f"\n[DRY RUN] Would DM to: {MANAGER['name']} ({MANAGER['slack_id']})")
        print(json.dumps(payload, indent=2))
        return True

    # In production: use Slack Web API chat.postMessage
    # client.chat_postMessage(channel=MANAGER["slack_id"], blocks=blocks)
    print(f"[TODO] Send DM to {MANAGER['slack_id']} — wire up Slack client here")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Send end-of-day SE digest recap to manager.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview without sending (default)")
    parser.add_argument("--send",    action="store_true",
                        help="Actually send the DM (requires --send explicitly)")
    args = parser.parse_args()

    dry_run     = not args.send
    log         = load_action_log_today()
    recap_date  = date.today().strftime("%B %-d, %Y")

    print(f"\n{'═'*55}")
    print(f"SE RECAP — {recap_date}")
    print(f"{'═'*55}")
    print(format_recap_text(log, recap_date))

    print(f"\n{'─'*55}")
    print("SLACK BLOCK KIT PAYLOAD")
    print("─"*55)
    blocks = build_recap_blocks(log, recap_date)
    send_recap_dm(blocks, dry_run=dry_run)


if __name__ == "__main__":
    main()
