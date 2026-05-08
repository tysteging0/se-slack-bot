#!/usr/bin/env python3
"""
se-slack-bot/sf_writes.py

Step 4: Salesforce write specs for every digest path.

Each function here is the canonical definition of exactly what gets
written to Salesforce when an SE takes an action.  Functions are
fully self-contained — they build the payload AND execute the write
via sf_client (OAuth refresh token, no sf CLI required).

All functions are safe to call in dry-run mode (pass dry_run=True)
which prints the payload without writing to Salesforce.

Run to simulate all writes:
    python3 sf_writes.py --dry-run
    python3 sf_writes.py --dry-run --ticket Ticket-00864090 --se "Michaël Vasseur"
"""

import argparse
from datetime import date
from typing import Optional

from digest import OWNERS, mark_reviewed_today, load_action_log_today
from sf_client import update_record as _sf_update, insert_chatter_post as _sf_chatter


# ── Shared helpers ────────────────────────────────────────────────────────────

def sf_update_record(sobject: str, record_id: str, fields: dict,
                     dry_run: bool = False) -> bool:
    """Update a Salesforce record. Dry-run prints payload; live calls sf_client."""
    if dry_run:
        print(f"\n[DRY RUN] update {sobject} {record_id}")
        for k, v in fields.items():
            print(f"  {k}: {v}")
        return True
    return _sf_update(sobject, record_id, fields)


def sf_insert_feeditem(parent_id: str, body: str,
                       dry_run: bool = False) -> bool:
    """Post a chatter FeedItem. Dry-run prints body; live calls sf_client."""
    if dry_run:
        print(f"\n[DRY RUN] FeedItem on {parent_id}")
        print(f"  Body:\n{_indent(body, 4)}")
        return True
    return _sf_chatter(parent_id, body)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


def build_chatter_body(se_name: str, note: str, note_type: str) -> str:
    """
    Canonical chatter post body format.
    note_type: 'update' | 'resolution'
    """
    emoji = "🔄" if note_type == "update" else "✅"
    label = "SE Update" if note_type == "update" else "SE Resolution"
    today = date.today().strftime("%B %-d, %Y")
    return (
        f"{emoji} {label} — {today}\n"
        f"{se_name} via SE Tickets Bot\n"
        f"\n"
        f"{note}"
    )


# ── Write functions (one per SF action) ───────────────────────────────────────

def write_chatter_update(ticket: dict, se_name: str, note: str,
                         dry_run: bool = False) -> bool:
    """
    PATH A — Still Active
    Posts a "SE Update" chatter note to the ticket.
    Ticket stays open. No status change.
    """
    ticket_id   = ticket["sf_id"]   # Salesforce record Id (18-char)
    body = build_chatter_body(se_name, note, "update")
    print(f"\n[WRITE] Chatter update on {ticket['id']} ({ticket_id})")
    return sf_insert_feeditem(ticket_id, body, dry_run=dry_run)


def write_chatter_resolution(ticket: dict, se_name: str, note: str,
                              dry_run: bool = False) -> bool:
    """
    PATH B — Close Ticket (step 1 of 2)
    Posts a "SE Resolution" chatter note to the ticket.
    """
    ticket_id = ticket["sf_id"]
    body = build_chatter_body(se_name, note, "resolution")
    print(f"\n[WRITE] Chatter resolution on {ticket['id']} ({ticket_id})")
    return sf_insert_feeditem(ticket_id, body, dry_run=dry_run)


def write_close_ticket(ticket: dict, dry_run: bool = False) -> bool:
    """
    PATH B — Close Ticket (step 2 of 2)
    Sets Status__c → Closed on the ticket record.
    Always runs after write_chatter_resolution.
    """
    ticket_id = ticket["sf_id"]
    print(f"\n[WRITE] Close ticket {ticket['id']} ({ticket_id})")
    return sf_update_record(
        sobject="Ticket__c",
        record_id=ticket_id,
        fields={"Status__c": "Closed"},
        dry_run=dry_run,
    )


def write_chatter_dm_note(ticket: dict, dry_run: bool = False) -> bool:
    """
    PATH C — DM sent
    Posts a chatter note confirming the DM was sent.
    This also acts as the natural chatter-skip signal — the ticket won't
    resurface in the digest for CHATTER_SKIP_DAYS days.
    """
    ticket_id = ticket["sf_id"]
    body = (
        f"📨 Slack DM sent to ticket requester to collaborate on ask.\n"
        f"SE Tickets Bot — {date.today().strftime('%B %-d, %Y')}"
    )
    print(f"\n[WRITE] DM chatter note on {ticket['id']} ({ticket_id})")
    return sf_insert_feeditem(ticket_id, body, dry_run=dry_run)


# ── Path orchestrators (called by the Slack event handler) ───────────────────

def handle_one_tap_active(ticket: dict, se_name: str,
                          dry_run: bool = False) -> dict:
    """
    One-tap confirm: SE confirmed the ticket is still active.
    No new chatter post — existing recent note is sufficient.
    Just marks the ticket reviewed today and advances.
    """
    print(f"\n[ONE-TAP] Confirmed active: {ticket['id']}")
    if dry_run:
        print(f"  [DRY RUN] No SF write — existing chatter is sufficient")
    if not dry_run:
        mark_reviewed_today(ticket["id"], se_name=se_name, action="one_tap_active",
                            ticket_meta=ticket)
    return {"success": True, "actions": {"one_tap_confirm": True}, "next": "advance"}


def handle_still_active(ticket: dict, se_name: str, note: str,
                         dry_run: bool = False) -> dict:
    """
    Full execution for Path A: Yes → Still Active → submit note.
    Marks the ticket as reviewed today on success.
    """
    results = {}
    results["chatter_update"] = write_chatter_update(ticket, se_name, note, dry_run)
    success = all(results.values())
    if success and not dry_run:
        mark_reviewed_today(ticket["id"], se_name=se_name, action="still_active",
                            note=note, ticket_meta=ticket)
    return {"success": success, "actions": results, "next": "advance"}


def handle_close_ticket(ticket: dict, se_name: str, note: str,
                         dry_run: bool = False) -> dict:
    """
    Full execution for Path B: Yes → Close Ticket → submit resolution.
    Writes chatter first, then closes. Marks reviewed today on success.
    """
    results = {}
    results["chatter_resolution"] = write_chatter_resolution(ticket, se_name, note, dry_run)
    results["close_ticket"]       = write_close_ticket(ticket, dry_run)
    success = all(results.values())
    if success and not dry_run:
        mark_reviewed_today(ticket["id"], se_name=se_name, action="close",
                            note=note, ticket_meta=ticket)
    return {"success": success, "actions": results, "next": "advance"}


def handle_dm_send(ticket: dict, se_name: str, slack_fn,
                   dry_run: bool = False) -> dict:
    """
    Full execution for Path C: No → Send DM.
    Marks reviewed today so the ticket doesn't resurface on a re-send.

    slack_fn: callable(user_id, message) → bool
    """
    results = {}
    se_first  = OWNERS.get(se_name, {}).get("first_name", se_name)
    opp_first = ticket["opp_owner"].split()[0]
    dm_body = (
        f"Hey {opp_first}! {se_first} wanted to connect on "
        f"{ticket['id']} for {ticket['account']}. "
        f"Are you free to sync this week?"
    )

    if dry_run:
        print(f"\n[DRY RUN] Slack DM to opp owner: {ticket['opp_owner']}")
        print(f"  Message: {dm_body}")
        results["slack_dm"] = True
    else:
        results["slack_dm"] = slack_fn(ticket.get("opp_owner_slack_id"), dm_body)

    results["dm_chatter"] = write_chatter_dm_note(ticket, dry_run)
    success = all(results.values())
    if success and not dry_run:
        mark_reviewed_today(ticket["id"], se_name=se_name, action="dm_send",
                            ticket_meta=ticket)
    return {"success": success, "actions": results, "next": "advance"}


def handle_dm_skip(ticket: dict, se_name: str = "", dry_run: bool = False) -> dict:
    """
    Path D: No → Skip.  No SF writes, but still marks reviewed today
    so skipped tickets don't reappear on a same-day re-send.
    """
    print(f"\n[SKIP] No action for {ticket['id']}")
    if not dry_run:
        mark_reviewed_today(ticket["id"], se_name=se_name, action="skip",
                            ticket_meta=ticket)
    return {"success": True, "actions": {}, "next": "advance"}


# ── Dry-run preview ───────────────────────────────────────────────────────────

SAMPLE_TICKET_WITH_SF_ID = {
    "id":              "Ticket-00864090",
    "sf_id":           "a0X000000EXAMPLE",   # 18-char Salesforce Id (replace when live)
    "account":         "Chris Bailey Inc",
    "opp_name":        "Chris Bailey Inc - New Business",
    "opp_stage":       "Commit",
    "opp_amount":      8400,
    "opp_owner":       "Mackenzie Matthews",
    "opp_owner_slack_id": "PLACEHOLDER_MM",  # Slack user ID for Mackenzie
    "days_open":       9,
    "priority":        115,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Simulate all writes without touching Salesforce (default: True)")
    parser.add_argument("--live",    action="store_true",
                        help="ACTUALLY write to Salesforce (requires --live flag explicitly)")
    parser.add_argument("--ticket",  type=str, help="Override sample ticket ID")
    parser.add_argument("--se",      type=str, default="Michaël Vasseur",
                        help="SE name (must be in OWNERS dict)")
    args = parser.parse_args()

    dry_run = not args.live   # default to dry-run; --live overrides

    ticket = dict(SAMPLE_TICKET_WITH_SF_ID)
    if args.ticket:
        ticket["id"] = args.ticket

    se_name = args.se
    sample_note_update     = "Demo scheduled for Friday. Following up on pricing after the call."
    sample_note_resolution = "Ran full product demo. AE closed the deal the same week. Customer loved payroll automation."

    print(f"\n{'═'*60}")
    print(f"SF WRITES DRY-RUN — SE: {se_name}  |  Ticket: {ticket['id']}")
    print(f"{'═'*60}")
    print("(Pass --live to execute for real)\n")

    print("── PATH A: Yes → Still Active ──────────────────────────────")
    r = handle_still_active(ticket, se_name, sample_note_update, dry_run=dry_run)
    print(f"  Result: {'✅ OK' if r['success'] else '❌ FAILED'}  |  next: {r['next']}")

    print("\n── PATH B: Yes → Close Ticket ──────────────────────────────")
    r = handle_close_ticket(ticket, se_name, sample_note_resolution, dry_run=dry_run)
    print(f"  Result: {'✅ OK' if r['success'] else '❌ FAILED'}  |  next: {r['next']}")

    print("\n── PATH C: No → Send DM ────────────────────────────────────")
    r = handle_dm_send(ticket, se_name, slack_fn=lambda uid, msg: True, dry_run=dry_run)
    print(f"  Result: {'✅ OK' if r['success'] else '❌ FAILED'}  |  next: {r['next']}")
    print("  (chatter note posted; ticket suppressed for 2 days by skip logic)")

    print("\n── PATH D: No → Skip ───────────────────────────────────────")
    r = handle_dm_skip(ticket)
    print(f"  Result: {'✅ OK' if r['success'] else '❌ FAILED'}  |  next: {r['next']}")

    print(f"\n{'═'*60}")
    print("CHATTER BODY PREVIEW")
    print("═"*60)
    print("\n[Update format]")
    print(build_chatter_body(se_name, sample_note_update, "update"))
    print("\n[Resolution format]")
    print(build_chatter_body(se_name, sample_note_resolution, "resolution"))


if __name__ == "__main__":
    main()
