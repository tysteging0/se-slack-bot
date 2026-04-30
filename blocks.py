#!/usr/bin/env python3
"""
se-slack-bot/blocks.py

Slack Block Kit message templates for the SE ticket digest.
Every state of the conversation is defined here as a function
that returns a Block Kit payload (list of blocks).

Preview any template by running:
    python3 blocks.py --template opening --owner "Michaël Vasseur"
    python3 blocks.py --template connected
    python3 blocks.py --template not_yet
    python3 blocks.py --template still_active
    python3 blocks.py --template close_ticket
    python3 blocks.py --template dm_sent
    python3 blocks.py --template all

Paste the output into https://app.slack.com/block-kit-builder to preview visually.
"""

import argparse
import json
from datetime import date
from digest import staleness_indicator


# ── Helpers ───────────────────────────────────────────────────────────────────

def divider():
    return {"type": "divider"}


def section(text: str):
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text}
    }


def header(text: str):
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text, "emoji": True}
    }


def context(text: str):
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}]
    }


def buttons(*btn_defs):
    """
    btn_defs: list of (text, action_id, style)
    style: "primary" | "danger" | None
    """
    elements = []
    for text, action_id, style in btn_defs:
        btn = {
            "type": "button",
            "text": {"type": "plain_text", "text": text, "emoji": True},
            "action_id": action_id,
        }
        if style:
            btn["style"] = style
        elements.append(btn)
    return {"type": "actions", "elements": elements}


def priority_emoji(priority: int) -> str:
    if priority >= 190:
        return "🔴"
    if priority >= 100:
        return "🟡"
    return "🟢"


def format_mrr(amount: float) -> str:
    if not amount:
        return "MRR TBD"
    return f"${amount:,.0f}"


# ── Sample ticket for previewing ──────────────────────────────────────────────

SAMPLE_TICKET = {
    "id":              "Ticket-00864090",
    "account":         "Chris Bailey Inc",
    "opp_name":        "Chris Bailey Inc - New Business",
    "opp_stage":       "Commit",
    "opp_amount":      8400,
    "opp_owner":       "Mackenzie Matthews",
    "days_open":       9,
    "sf_priority":     "High",
    "anchor_pay_date": "2026-06-01",
    "is_premium":      False,
    "priority":        198,   # High(75) + Commit(50) + 9days(14+2*3=20) + mrr(~39) = 184 → rounded
}

SAMPLE_OWNER  = "Michaël Vasseur"
SAMPLE_FIRST  = "Michaël"
SAMPLE_TOTAL  = 8
SAMPLE_INDEX  = 1


# ── Templates ─────────────────────────────────────────────────────────────────

def block_opening(first_name: str, total: int, index: int, ticket: dict) -> list:
    """
    The first message the SE sees each morning for each ticket.
    Shows ticket details + the connected / not yet question.
    """
    priority = ticket.get("priority", 0)
    p_label  = "High Priority" if priority >= 190 else "Medium" if priority >= 100 else "Standard"
    sf_pri   = ticket.get("sf_priority") or "—"
    anchor   = ticket.get("anchor_pay_date") or "—"
    stale    = ticket.get("staleness") or staleness_indicator(ticket.get("days_open", 0))

    return [
        header(f"Good morning {first_name} 👋"),
        section(f"You have *{total}* open ticket{'s' if total != 1 else ''} to review today."),
        divider(),

        # Ticket card
        section(
            f"{priority_emoji(priority)}  *{index} of {total}*  ·  {p_label}  ·  SF Priority: {sf_pri}\n\n"
            f"*📋 {ticket['id']}*\n"
            f"🏢  {ticket['account']}\n"
            f"💼  {ticket['opp_name']}  ·  _{ticket['opp_stage']}_\n"
            f"📅  Open {ticket['days_open']} day{'s' if ticket['days_open'] != 1 else ''}"
            f"{stale}  "
            f"·  ⚓  APD: {anchor}  "
            f"·  💰  {format_mrr(ticket.get('opp_amount', 0))}"
        ),
        divider(),

        section("Have you connected with the requester on this?"),
        buttons(
            ("✅  Yes", f"connected_yes__{ticket['id']}", "primary"),
            ("❌  Not Yet", f"connected_no__{ticket['id']}", None),
        ),
    ]


def block_one_tap_confirm(first_name: str, total: int, index: int, ticket: dict) -> list:
    """
    Streamlined card for tickets that already have recent chatter.
    Shows the last update as context — no note required to confirm active.

    Three choices:
      ✅ Confirm Active  — one tap, no typing, marks done and advances
      📝 Add Update      — opens the standard Still Active modal
      🔒 Close Ticket   — opens the resolution modal
    """
    priority = ticket.get("priority", 0)
    p_label  = "High Priority" if priority >= 190 else "Medium" if priority >= 100 else "Standard"
    sf_pri   = ticket.get("sf_priority") or "—"
    anchor   = ticket.get("anchor_pay_date") or "—"
    stale    = ticket.get("staleness") or staleness_indicator(ticket.get("days_open", 0))
    note     = ticket.get("last_chatter_note") or ""
    days_ago = ticket.get("days_since_chatter")
    ago_str  = f"{days_ago} day{'s' if days_ago != 1 else ''} ago" if days_ago else "recently"

    # Truncate long notes for display
    preview = (note[:140] + "…") if len(note) > 140 else note

    return [
        divider(),
        section(
            f"{priority_emoji(priority)}  *{index} of {total}*  ·  {p_label}  ·  SF Priority: {sf_pri}\n\n"
            f"*📋 {ticket['id']}*\n"
            f"🏢  {ticket['account']}\n"
            f"💼  {ticket['opp_name']}  ·  _{ticket['opp_stage']}_\n"
            f"📅  Open {ticket['days_open']} day{'s' if ticket['days_open'] != 1 else ''}"
            f"{stale}  ·  ⚓  APD: {anchor}  ·  💰  {format_mrr(ticket.get('opp_amount', 0))}"
        ),
        divider(),
        section(
            f"🗒️  *Last update ({ago_str}):*\n"
            f"> {preview}"
        ),
        section("Still active on this one?"),
        buttons(
            ("✅  Confirm Active",  f"one_tap_active__{ticket['id']}",  "primary"),
            ("📝  Add Update",      f"one_tap_update__{ticket['id']}",  None),
            ("🔒  Close Ticket",   f"one_tap_close__{ticket['id']}",   "danger"),
        ),
    ]


def block_one_tap_confirmed(ticket: dict) -> list:
    """Shown after SE taps Confirm Active — no note needed."""
    return [
        section(
            f"✅  *{ticket['id']}* confirmed active — Salesforce is up to date."
        ),
        context("Moving to your next ticket..."),
    ]


def block_connected_yes(ticket: dict) -> list:
    """
    Shown after SE clicks Yes — connected with requester.
    Asks for current status.
    """
    return [
        section(f"Got it — you've connected on *{ticket['id']}*. ✅"),
        divider(),
        section("What's the current status?"),
        buttons(
            ("🔒  Close Ticket",  f"status_close__{ticket['id']}",  "danger"),
            ("🔄  Still Active",  f"status_active__{ticket['id']}", None),
        ),
    ]


def block_still_active_modal(ticket: dict) -> dict:
    """
    Modal that opens when SE clicks Still Active.
    Requires a next steps note before submitting.
    Returns a Slack modal payload (not a block list).
    """
    return {
        "type": "modal",
        "callback_id": f"submit_update__{ticket['id']}",
        "title": {
            "type": "plain_text",
            "text": "Next Steps",
            "emoji": True
        },
        "submit": {"type": "plain_text", "text": "Submit ✅"},
        "close":  {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            section(
                f"*{ticket['id']}*  ·  {ticket['account']}\n"
                f"_{ticket['opp_stage']}_  ·  Open {ticket['days_open']} days"
            ),
            divider(),
            {
                "type":    "input",
                "block_id": "next_steps_input",
                "label": {
                    "type":  "plain_text",
                    "text":  "What are the next steps?",
                    "emoji": True
                },
                "hint": {
                    "type": "plain_text",
                    "text": "This will be posted to the ticket's chatter in Salesforce."
                },
                "element": {
                    "type":            "plain_text_input",
                    "action_id":       "next_steps_text",
                    "multiline":       True,
                    "min_length":      10,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. Demo scheduled for Friday. Following up on pricing after the call..."
                    }
                }
            }
        ]
    }


def block_close_ticket_modal(ticket: dict) -> dict:
    """
    Modal that opens when SE clicks Close Ticket.
    Requires a resolution note before submitting.
    Returns a Slack modal payload.
    """
    return {
        "type": "modal",
        "callback_id": f"submit_close__{ticket['id']}",
        "title": {
            "type": "plain_text",
            "text": "Resolution Note",
            "emoji": True
        },
        "submit": {"type": "plain_text", "text": "Close Ticket ✅"},
        "close":  {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            section(
                f"*{ticket['id']}*  ·  {ticket['account']}\n"
                f"_{ticket['opp_stage']}_  ·  Open {ticket['days_open']} days"
            ),
            divider(),
            {
                "type":    "input",
                "block_id": "resolution_input",
                "label": {
                    "type":  "plain_text",
                    "text":  "How was this resolved?",
                    "emoji": True
                },
                "hint": {
                    "type": "plain_text",
                    "text": "This will be posted to the ticket's chatter and the ticket will be closed in Salesforce."
                },
                "element": {
                    "type":      "plain_text_input",
                    "action_id": "resolution_text",
                    "multiline": True,
                    "min_length": 10,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. Ran full product demo. AE closed the deal the same week. Customer loved payroll automation..."
                    }
                }
            }
        ]
    }


def block_not_yet(ticket: dict) -> list:
    """
    Shown after SE clicks Not Yet — hasn't connected with requester.
    Offers to ping the opp owner via Slack DM.
    """
    return [
        section(f"Got it — not connected yet on *{ticket['id']}*."),
        divider(),
        section(
            f"Want me to ping *{ticket['opp_owner']}* now to set up time?"
        ),
        buttons(
            ("📨  Send them a DM", f"dm_send__{ticket['id']}", "primary"),
            ("⏭️  Skip for now",   f"dm_skip__{ticket['id']}", None),
        ),
    ]


def block_dm_sent(ticket: dict, se_first_name: str) -> list:
    """
    Confirmation shown after DM is sent to the opp owner.
    """
    opp_first = ticket["opp_owner"].split()[0]
    return [
        section(
            f"✅  DM sent to *{ticket['opp_owner']}*:\n\n"
            f"> Hey {opp_first}! {se_first_name} wanted to connect on "
            f"*{ticket['id']}* for {ticket['account']}. "
            f"Are you free to sync this week?"
        ),
        context(f"Moving to your next ticket..."),
    ]


def block_update_confirmed(ticket: dict, note: str) -> list:
    """
    Confirmation shown after SE submits a Still Active note.
    """
    today = date.today().strftime("%B %-d, %Y")
    return [
        section(
            f"✅  *{ticket['id']}* stays open — note posted to Salesforce chatter:\n\n"
            f"> 🔄 SE Update — {today}\n"
            f"> {note}"
        ),
        context("Moving to your next ticket..."),
    ]


def block_close_confirmed(ticket: dict, note: str) -> list:
    """
    Confirmation shown after SE submits a Close Ticket note.
    """
    today = date.today().strftime("%B %-d, %Y")
    return [
        section(
            f"✅  *{ticket['id']}* closed — resolution posted to Salesforce chatter:\n\n"
            f"> ✅ SE Resolution — {today}\n"
            f"> {note}"
        ),
        context("Moving to your next ticket..."),
    ]


def block_digest_complete(first_name: str, total: int) -> list:
    """
    Final message shown when all tickets have been reviewed.
    """
    return [
        divider(),
        section(
            f"🎉  All done, {first_name}! You reviewed *{total}* ticket{'s' if total != 1 else ''} today.\n"
            f"Salesforce is up to date. See you tomorrow 👋"
        ),
    ]


# ── Preview ───────────────────────────────────────────────────────────────────

TEMPLATES = {
    "opening":      lambda: {"blocks": block_opening(SAMPLE_FIRST, SAMPLE_TOTAL, SAMPLE_INDEX, SAMPLE_TICKET)},
    "connected":    lambda: {"blocks": block_connected_yes(SAMPLE_TICKET)},
    "not_yet":      lambda: {"blocks": block_not_yet(SAMPLE_TICKET)},
    "dm_sent":      lambda: {"blocks": block_dm_sent(SAMPLE_TICKET, SAMPLE_FIRST)},
    "still_active": lambda: block_still_active_modal(SAMPLE_TICKET),
    "close_ticket": lambda: block_close_ticket_modal(SAMPLE_TICKET),
    "confirmed_update": lambda: {"blocks": block_update_confirmed(SAMPLE_TICKET, "Demo scheduled for Friday. Following up on pricing after the call.")},
    "confirmed_close":  lambda: {"blocks": block_close_confirmed(SAMPLE_TICKET, "Ran full demo. AE closed the deal same week.")},
    "complete":     lambda: {"blocks": block_digest_complete(SAMPLE_FIRST, SAMPLE_TOTAL)},
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=str, default="all",
                        help=f"Template to preview: {', '.join(TEMPLATES)} or 'all'")
    args = parser.parse_args()

    to_show = list(TEMPLATES.keys()) if args.template == "all" else [args.template]

    for name in to_show:
        if name not in TEMPLATES:
            print(f"Unknown template: {name}")
            continue
        print(f"\n{'━'*60}")
        print(f"TEMPLATE: {name}")
        print(f"Paste into: https://app.slack.com/block-kit-builder")
        print("━"*60)
        print(json.dumps(TEMPLATES[name](), indent=2))


if __name__ == "__main__":
    main()
