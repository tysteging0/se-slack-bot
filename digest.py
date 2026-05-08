#!/usr/bin/env python3
"""
se-slack-bot/digest.py

Core logic for the daily SE ticket digest.
Queries Salesforce, prioritizes tickets, formats message payloads.

Run standalone to preview what would be sent (no Slack connection needed yet):
    python3 digest.py --preview
    python3 digest.py --preview --owner "Michaël Vasseur"
    python3 digest.py --preview --owner "Willow Turano"
"""

import argparse
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from sf_client import run_soql


# ── Config ────────────────────────────────────────────────────────────────────

OWNERS = {
    "Michaël Vasseur": {
        "slack_id": "UPUFD3AKH",
        "first_name": "Michaël",
    },
    "Willow Turano": {
        "slack_id": "UNWKXTDDL",
        "first_name": "Willow",
    },
}

# ── Priority scoring ──────────────────────────────────────────────────────────
#
# Three-layer model:
#   1. SF Priority__c  — primary signal, set on ticket; reflects requester urgency
#   2. Opp stage       — context modifier; late-stage deals amplify urgency
#   3. Age + MRR       — amplifiers; old tickets and big deals surface faster
#
# Display thresholds (emoji on card):
#   🔴  ≥ 190  Critical / High
#   🟡  100–189  Medium
#   🟢  < 100  Standard

# 1. SF ticket Priority__c field → base score
SF_TICKET_PRIORITY = {
    "Critical": 100,
    "High":      75,
    "Medium":    50,
    "Low":       25,
}

# Chatter skip window — tickets with recent chatter are held out of the digest
# for this many days. Keeps the SE from being nagged about something they just
# documented. Set to 0 to disable.
CHATTER_SKIP_DAYS = 2   # Hold ticket out of digest for this many days after chatter
ONE_TAP_DAYS      = 7   # Tickets with chatter 3–7 days old get the one-tap confirm card

# ── Session state (already-answered-today) ────────────────────────────────────
# Tracks which ticket IDs have been responded to in the current calendar day.
# Stored as a JSON file so it persists across re-sends on the same day.
# Cleared automatically when a new day starts.
# On Render.com the persistent disk is mounted at /data; fall back to local dir for dev
_DATA_DIR    = Path(os.environ.get("SESSION_DIR", Path(__file__).parent))
SESSION_FILE = _DATA_DIR / ".session_state.json"


def _load_session() -> dict:
    """Load the session state file, returning an empty dict if missing/corrupt."""
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_session(state: dict) -> None:
    SESSION_FILE.write_text(json.dumps(state, indent=2))


def load_reviewed_today() -> set:
    """Return the set of ticket IDs already answered today."""
    today = date.today().isoformat()
    entry = _load_session().get(today, {})
    # Support both old list format and new dict format
    if isinstance(entry, list):
        return set(entry)
    return set(entry.get("reviewed", []))


def mark_reviewed_today(ticket_id: str, se_name: str = "",
                         action: str = "", note: str = "",
                         ticket_meta: dict = None) -> None:
    """
    Record that a ticket was answered in this session.
    Stores enough context for the end-of-day managerial recap.

    action: 'still_active' | 'close' | 'dm_send' | 'skip'
    ticket_meta: dict with account, opp_stage, opp_amount keys
    """
    today = date.today().isoformat()
    state = _load_session()
    # Keep today + yesterday — nudge reads yesterday's session next morning
    cutoff = (date.today() - timedelta(days=1)).isoformat()
    state = {k: v for k, v in state.items() if k >= cutoff}

    entry = state.get(today, {"reviewed": [], "log": []})
    if isinstance(entry, list):   # migrate old format
        entry = {"reviewed": entry, "log": []}

    reviewed = set(entry.get("reviewed", []))
    reviewed.add(ticket_id)
    entry["reviewed"] = sorted(reviewed)

    # Append to action log for recap
    meta = ticket_meta or {}
    entry.setdefault("log", []).append({
        "se":         se_name,
        "ticket_id":  ticket_id,
        "account":    meta.get("account", ""),
        "opp_stage":  meta.get("opp_stage", ""),
        "opp_amount": meta.get("opp_amount", 0),
        "action":     action,
        "note":       note,
        "time":       datetime.now().strftime("%-I:%M %p"),
    })

    state[today] = entry
    _save_session(state)


def load_action_log_today() -> list:
    """Return the full action log for today — used by recap.py."""
    today = date.today().isoformat()
    entry = _load_session().get(today, {})
    if isinstance(entry, list):
        return []
    return entry.get("log", [])


def save_digest_tickets(owner_name: str, ticket_ids: list) -> None:
    """
    Persist the full list of ticket IDs sent in today's digest.
    Used by the next-morning nudge to know what was outstanding.
    """
    today = date.today().isoformat()
    state = _load_session()
    cutoff = (date.today() - timedelta(days=1)).isoformat()
    state = {k: v for k, v in state.items() if k >= cutoff}

    entry = state.get(today, {"reviewed": [], "log": [], "digest_tickets": {}})
    if isinstance(entry, list):
        entry = {"reviewed": entry, "log": [], "digest_tickets": {}}
    entry.setdefault("digest_tickets", {})[owner_name] = ticket_ids
    state[today] = entry
    _save_session(state)


def load_nudge_data(target_date: date) -> dict:
    """
    Return {owner_name: n_remaining} for the given date.
    Compares the tickets that were sent in the digest against the reviewed set
    to find what the SE left unanswered.

    Called at 9am MT the NEXT morning — pass yesterday's date.
    """
    date_str = target_date.isoformat()
    state    = _load_session()
    entry    = state.get(date_str, {})
    if isinstance(entry, list):
        return {}

    reviewed       = set(entry.get("reviewed", []))
    digest_tickets = entry.get("digest_tickets", {})

    result = {}
    for owner_name, ticket_ids in digest_tickets.items():
        remaining = [tid for tid in ticket_ids if tid not in reviewed]
        if remaining:
            result[owner_name] = len(remaining)
    return result


# ── Staleness indicator ───────────────────────────────────────────────────────

def staleness_indicator(days: int) -> str:
    """
    Returns a plain-text staleness tag appended to the days-open line.
    Escalates in weight as the ticket ages.

      0–6 days  : (nothing)
      7–13 days : ⏰  Aging
      14–29 days: ⚠️  Stale
      30+ days  : 🚨  *Long overdue*   ← bolded in Slack mrkdwn
    """
    if days < 7:
        return ""
    if days < 14:
        return "  ·  ⏰ Aging"
    if days < 30:
        return "  ·  ⚠️ *Stale*"
    return "  ·  🚨 *Long overdue*"

# 2. Opp stage → context modifier (secondary weight)
OPP_STAGE_WEIGHT = {
    "Commit":         50,
    "Qualified":      40,
    "Discovery":      35,
    "Prospecting":    28,
    "MQL":            22,
    "Pending PR":     18,
    "Implementation": 10,
    "No Opp":          5,
}

# ── Salesforce ────────────────────────────────────────────────────────────────

def fetch_open_tickets(owner_name: str) -> list:
    """
    Fetch all open SE tickets for a given owner, including the most
    recent chatter post date on each ticket (used for skip logic).
    Returns raw Salesforce records.
    """
    if owner_name not in OWNERS:
        raise ValueError(f"Unknown owner: {owner_name!r}")

    # Rolling 18-month floor — no upper bound, works for any future ticket
    date_floor = (date.today() - timedelta(days=548)).strftime("%Y-%m-%dT00:00:00Z")
    safe_name  = owner_name.replace("'", "\\'")

    query = f"""
        SELECT
            Id,
            Name,
            Status__c,
            Priority__c,
            RecordType.Name,
            CreatedDate,
            Account__r.Name,
            Opportunity__c,
            Opportunity__r.Name,
            Opportunity__r.StageName,
            Opportunity__r.Amount,
            Opportunity__r.Owner.Name,
            Opportunity__r.Owner.Email,
            Opportunity__r.Anchor_Pay_Date__c,
            (SELECT Id, CreatedDate, Body FROM Feeds
             WHERE Type = 'TextPost'
             ORDER BY CreatedDate DESC
             LIMIT 1)
        FROM Ticket__c
        WHERE RecordType.Name = 'Solution Engineer Request'
        AND Owner.Name = '{safe_name}'
        AND Status__c IN ('New', 'In Progress')
        AND CreatedDate >= {date_floor}
    """
    return run_soql(query)


# ── Ticket Processing ─────────────────────────────────────────────────────────

def days_open(created_date_str: str) -> int:
    """Calculate how many days a ticket has been open."""
    # Salesforce returns ISO format: 2026-04-20T00:00:00.000+0000
    try:
        dt = datetime.fromisoformat(created_date_str.replace("Z", "+00:00")[:19])
        return (date.today() - dt.date()).days
    except Exception:
        return 0


def _parse_sf_date(date_str: str) -> Optional[date]:
    """Parse a Salesforce ISO datetime string to a Python date."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")[:19]).date()
    except Exception:
        return None


def _parse_feeds(feeds_result: Optional[dict]) -> tuple:
    """
    Extract (last_chatter_date, last_chatter_body, feed_item_id) from the Feeds subquery.
    Returns (None, "", None) if no posts exist.
    """
    if not feeds_result:
        return None, "", None
    records = feeds_result.get("records") or []
    if not records:
        return None, "", None
    r = records[0]
    return (
        _parse_sf_date(r.get("CreatedDate", "")),
        (r.get("Body") or "").strip(),
        r.get("Id"),
    )


def should_include_today(ticket: dict, reviewed_today: set) -> bool:
    """
    Return True if this ticket should appear in today's digest.

    Excluded when ANY of:
      1. Already answered in today's session (re-send protection)
      2. Has chatter posted since assignment within CHATTER_SKIP_DAYS days
         (SE just documented something — don't nag them)

    Always included when:
      - No chatter ever exists on the ticket (silence = needs attention)
      - All chatter predates ticket creation (legacy posts don't count)
    """
    # 1. Already answered today
    if ticket.get("id") in reviewed_today:
        return False

    # 2. Recent chatter skip
    if CHATTER_SKIP_DAYS == 0:
        return True

    lcd = ticket.get("last_chatter_date")
    if not lcd:
        return True

    ticket_created = _parse_sf_date(ticket.get("created_date", ""))
    if ticket_created and lcd < ticket_created:
        return True

    return (date.today() - lcd).days > CHATTER_SKIP_DAYS


def _age_score(days: int) -> int:
    """
    Tiered age scoring — urgency accelerates as tickets age, caps at 95.
      0–7 days  : 2 pts/day  (max 14)
      8–21 days : 3 pts/day  (max 56)
      22–40 days: 2 pts/day  (max 92)
      40+ days  : hard cap at 95
    """
    if days <= 7:  return days * 2
    if days <= 21: return 14 + (days - 7) * 3
    if days <= 40: return 56 + (days - 21) * 2
    return 95


def _mrr_score(amount: float) -> int:
    """
    Log-scaled MRR — differentiates meaningfully across $1k–$500k+.
      $1k  →  30 pts
      $10k →  40 pts
      $100k → 50 pts  (cap)
    """
    if not amount or amount <= 0:
        return 0
    return min(int(math.log10(amount) * 10), 50)


def priority_score(ticket: dict) -> int:
    """
    Three-layer priority score. Higher = more urgent = shown first.

    Layer 1 — SF Priority__c (primary):  0–100
    Layer 2 — Opp stage (context):       5–50
    Layer 3 — Age + MRR (amplifiers):    0–145

    Max possible: 295
    Display thresholds: 🔴 ≥ 190  |  🟡 ≥ 100  |  🟢 < 100
    """
    sf_pri  = SF_TICKET_PRIORITY.get(ticket.get("sf_priority") or "", 0)
    stage   = OPP_STAGE_WEIGHT.get(ticket.get("opp_stage") or "No Opp", 10)
    age     = _age_score(ticket.get("days_open", 0))
    mrr     = _mrr_score(ticket.get("opp_amount") or 0)
    premium = 50 if ticket.get("is_premium") else 0
    return sf_pri + stage + age + mrr + premium


def process_tickets(raw_records: list) -> list:
    """
    Transform raw Salesforce records into clean ticket dicts,
    apply skip filters (answered today + recent chatter),
    then sort by priority score descending.
    """
    reviewed_today = load_reviewed_today()
    tickets  = []
    skipped  = []

    # First pass — parse records
    parsed_records = []
    for r in raw_records:
        opp  = r.get("Opportunity__r") or {}
        acct = r.get("Account__r") or {}
        created = r.get("CreatedDate", "")
        record_type = (r.get("RecordType") or {}).get("Name") or ""
        d_open = days_open(created)
        lcd, lcb, feed_item_id = _parse_feeds(r.get("Feeds"))
        parsed_records.append((r, opp, acct, created, record_type, d_open, lcd, lcb, feed_item_id))

    for r, opp, acct, created, record_type, d_open, lcd, lcb, feed_item_id in parsed_records:

        # One-tap confirm: ticket resurfaced after skip window but chatter is
        # still recent enough to show as context (CHATTER_SKIP_DAYS < age ≤ ONE_TAP_DAYS)
        ticket_created_date = _parse_sf_date(created)
        days_since_chatter  = (date.today() - lcd).days if lcd else None
        use_one_tap = (
            lcd is not None
            and ticket_created_date is not None
            and lcd >= ticket_created_date
            and days_since_chatter is not None
            and CHATTER_SKIP_DAYS < days_since_chatter <= ONE_TAP_DAYS
        )

        opp_owner = opp.get("Owner") or {}
        ticket = {
            "id":                 r.get("Name"),
            "sf_id":              r.get("Id"),           # 18-char SF record ID for writes
            "status":             r.get("Status__c"),
            "sf_priority":        r.get("Priority__c") or "",
            "created_date":       created,
            "days_open":          d_open,
            "staleness":          staleness_indicator(d_open),
            "account":            acct.get("Name") or "No Account",
            "opp_id":             r.get("Opportunity__c"),
            "opp_name":           opp.get("Name") or "No Opp Attached",
            "opp_stage":          opp.get("StageName") or "No Opp",
            "opp_amount":         opp.get("Amount") or 0,
            "opp_owner":          opp_owner.get("Name") or "Unknown",
            "opp_owner_email":    opp_owner.get("Email") or "",  # used for Slack DM lookup
            "anchor_pay_date":    opp.get("Anchor_Pay_Date__c") or None,
            "last_chatter_date":  lcd,
            "last_chatter_note":  lcb,
            "last_chatter_reply": "",
            "days_since_chatter": days_since_chatter,
            "use_one_tap":        use_one_tap,
            # Premium flag — update to match your actual record type or field
            "is_premium":         "premium" in record_type.lower() or "reseller" in record_type.lower(),
        }
        ticket["priority"] = priority_score(ticket)

        if should_include_today(ticket, reviewed_today):
            tickets.append(ticket)
        else:
            skipped.append(ticket)

    if skipped:
        already   = [t["id"] for t in skipped if t["id"] in reviewed_today]
        chattered = [t["id"] for t in skipped if t["id"] not in reviewed_today]
        if already:
            print(f"  ✅ {len(already)} already answered today: {', '.join(already)}")
        if chattered:
            print(f"  ↩  {len(chattered)} skipped — recent chatter: {', '.join(chattered)}")

    return sorted(tickets, key=lambda t: -t["priority"])


# ── Message Formatting ────────────────────────────────────────────────────────

def format_mrr(amount: float) -> str:
    if not amount:
        return "MRR TBD"
    return f"${amount:,.0f}"


def format_opening_message(owner_name: str, tickets: list) -> str:
    """
    Plain text preview of the opening digest message.
    (In production this becomes a Slack Block Kit payload.)
    """
    first_name = OWNERS[owner_name]["first_name"]
    count = len(tickets)

    lines = [
        f"Good morning {first_name} 👋  You have {count} open ticket{'s' if count != 1 else ''} to review.",
        "━" * 40,
    ]

    for i, t in enumerate(tickets, 1):
        p = t["priority"]
        p_label = "🔴 High Priority" if p >= 190 else "🟡 Medium" if p >= 100 else "🟢 Standard"
        sf_pri  = t.get("sf_priority") or "—"
        anchor  = t.get("anchor_pay_date") or "—"
        lines += [
            f"{i} of {count}  ·  {p_label}  ·  SF Priority: {sf_pri}  ·  Score: {p}",
            "",
            f"📋 {t['id']}",
            f"🏢 {t['account']}",
            f"💼 {t['opp_name']} ({t['opp_stage']})",
            f"📅 Open {t['days_open']} day{'s' if t['days_open'] != 1 else ''}{t.get('staleness','')}  ·  ⚓ APD: {anchor}  ·  💰 {format_mrr(t['opp_amount'])}",
            "",
            "Have you connected with the requester on this?",
            "[ ✅ Yes ]  [ ❌ Not Yet ]",
            "━" * 40,
        ]

    return "\n".join(lines)


def format_still_active_prompt(ticket: dict) -> str:
    """Prompt shown after SE clicks Still Active."""
    return (
        f"What are the next steps for {ticket['id']}?\n"
        f"(This will be posted to the ticket's chatter)\n\n"
        f"┌─────────────────────────────────────────┐\n"
        f"│                                         │\n"
        f"│  Type next steps here...                │\n"
        f"│                                         │\n"
        f"└─────────────────────────────────────────┘\n"
        f"                              [ Submit ✅ ]"
    )


def format_close_ticket_prompt(ticket: dict) -> str:
    """Prompt shown after SE clicks Close Ticket."""
    return (
        f"How was {ticket['id']} resolved?\n"
        f"(This will be posted to the ticket's chatter)\n\n"
        f"┌─────────────────────────────────────────┐\n"
        f"│                                         │\n"
        f"│  Describe the resolution here...        │\n"
        f"│                                         │\n"
        f"└─────────────────────────────────────────┘\n"
        f"                              [ Submit ✅ ]"
    )


def format_dm_offer(ticket: dict) -> str:
    """Prompt shown after SE clicks Not Yet."""
    return (
        f"Got it. Want me to ping {ticket['opp_owner']} now to set up time?\n\n"
        f"[ 📨 Send them a DM ]  [ ⏭️ Skip for now ]"
    )


def format_dm_message(se_name: str, ticket: dict) -> str:
    """
    The actual DM that gets sent to the ticket requester.
    (In production this fires via Slack API.)
    """
    first_name = OWNERS.get(se_name, {}).get("first_name", se_name)
    return (
        f"Hey {ticket['opp_owner'].split()[0]}! {first_name} wanted to connect on "
        f"{ticket['id']} for {ticket['account']}. "
        f"Are you free to sync this week?"
    )


def format_chatter_update(se_name: str, note: str, note_type: str) -> str:
    """
    Chatter post format for SE updates.
    note_type: 'update' or 'resolution'
    """
    emoji = "🔄" if note_type == "update" else "✅"
    label = "SE Update" if note_type == "update" else "SE Resolution"
    today = date.today().strftime("%B %-d, %Y")
    return (
        f"{emoji} {label} — {today}\n"
        f"{se_name} via SE Tickets Bot\n\n"
        f"{note}"
    )


# ── Branching Logic ───────────────────────────────────────────────────────────

def simulate_branch(ticket: dict, se_name: str):
    """
    Walk through all possible paths for a single ticket.
    Prints what would happen at each decision point.
    """
    print(f"\n{'═'*50}")
    print(f"TICKET: {ticket['id']}  |  {ticket['account']}")
    print(f"{'═'*50}")

    print("\n[QUESTION 1] Have you connected with the requester?")
    print("  → YES path:")
    print("    [QUESTION 2] What's the current status?")

    print("\n    → STILL ACTIVE path:")
    print("      [REQUIRED] Next steps note (free text)")
    print("      → Salesforce: chatter post added")
    print(f"         {format_chatter_update(se_name, 'Demo scheduled for Friday. Following up on pricing after.', 'update')}")
    print("      → Ticket stays open, moves to next in digest")

    print("\n    → CLOSE TICKET path:")
    print("      [REQUIRED] Resolution note (free text)")
    print("      → Salesforce: chatter post added")
    print(f"         {format_chatter_update(se_name, 'Ran full demo. AE closed the deal same week.', 'resolution')}")
    print("      → Salesforce: ticket status set to Closed")
    print("      → Moves to next in digest")

    print("\n  → NOT YET path:")
    print(f"    {format_dm_offer(ticket)}")
    print("\n    → SEND DM path:")
    print(f"      DM to {ticket['opp_owner']}:")
    print(f"      \"{format_dm_message(se_name, ticket)}\"")
    print("      → Moves to next in digest")
    print("\n    → SKIP path:")
    print("      → No action, moves to next in digest")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="Preview digest output")
    parser.add_argument("--owner", type=str, help="Filter to one SE")
    parser.add_argument("--branches", action="store_true", help="Show full branch logic")
    args = parser.parse_args()

    owners_to_run = (
        {args.owner: OWNERS[args.owner]}
        if args.owner and args.owner in OWNERS
        else OWNERS
    )

    for owner_name, config in owners_to_run.items():
        print(f"\n{'━'*50}")
        print(f"Fetching tickets for {owner_name}...")
        raw = fetch_open_tickets(owner_name)
        tickets = process_tickets(raw)
        print(f"Found {len(tickets)} open tickets.")

        if args.preview:
            print("\n" + format_opening_message(owner_name, tickets))

        if args.branches and tickets:
            print(f"\n--- BRANCH LOGIC PREVIEW ({owner_name}) ---")
            simulate_branch(tickets[0], owner_name)

        if not args.preview and not args.branches:
            print(f"\nTickets (priority order):")
            for t in tickets:
                print(
                    f"  [{t['priority']:>3}] {t['id']} | "
                    f"{t['opp_stage']:<15} | "
                    f"{t['days_open']:>2}d open | "
                    f"{t['account']}"
                )


if __name__ == "__main__":
    main()
