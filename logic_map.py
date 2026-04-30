#!/usr/bin/env python3
"""
se-slack-bot/logic_map.py

Step 3: Full branching logic map for the SE ticket digest.

Every possible button click, every decision point, every Salesforce
action, and every next message — defined in one place before any live
wiring happens.

Run to print the full tree:
    python3 logic_map.py
    python3 logic_map.py --ticket Ticket-00864090
    python3 logic_map.py --path yes_active
    python3 logic_map.py --path yes_close
    python3 logic_map.py --path no_dm
    python3 logic_map.py --path no_skip
"""

import argparse
from dataclasses import dataclass, field
from typing import Optional
from digest import OWNERS  # reuse SE config


# ── Node types ────────────────────────────────────────────────────────────────

@dataclass
class SFWrite:
    """A Salesforce action that fires at this node."""
    description: str          # Human-readable label
    object: str               # Salesforce object (Ticket__c, FeedItem)
    operation: str            # update | insert
    field_updates: dict       # { field_api_name: value_or_description }


@dataclass
class Node:
    """
    One state in the conversation.

    id          — unique key used in action_ids and routing
    label       — short human description
    trigger     — what the SE did to arrive here (button label or modal submit)
    message_fn  — name of the blocks.py function that renders this state
    sf_writes   — list of Salesforce writes that fire when entering this node
    next_nodes  — ids of nodes reachable from here (buttons / modal submit)
    terminal    — True if this is a final state (no more choices)
    requires_input — True if a free-text modal is required before advancing
    """
    id:             str
    label:          str
    trigger:        str
    message_fn:     str
    sf_writes:      list[SFWrite]        = field(default_factory=list)
    next_nodes:     list[str]            = field(default_factory=list)
    terminal:       bool                 = False
    requires_input: bool                 = False


# ── Salesforce write specs ────────────────────────────────────────────────────
#
# Defined here for reference; full canonical specs live in sf_writes.py (Step 4).

def sf_chatter_post(note_type: str) -> SFWrite:
    emoji = "🔄" if note_type == "update" else "✅"
    label = "SE Update" if note_type == "update" else "SE Resolution"
    return SFWrite(
        description=f"Post {label} to ticket chatter",
        object="FeedItem",
        operation="insert",
        field_updates={
            "ParentId":  "Ticket__c.Id",
            "Body":      f"{emoji} {label} — <today's date>\n<SE name> via SE Tickets Bot\n\n<free-text note>",
            "Type":      "TextPost",
        }
    )

def sf_close_ticket() -> SFWrite:
    return SFWrite(
        description="Set ticket Status__c to Closed",
        object="Ticket__c",
        operation="update",
        field_updates={
            "Status__c": "Closed",
        }
    )

def sf_dm_logged() -> SFWrite:
    """Optional: log that a DM was sent so we don't re-offer it tomorrow."""
    return SFWrite(
        description="Log DM-sent flag on ticket (optional field)",
        object="Ticket__c",
        operation="update",
        field_updates={
            "SE_DM_Sent__c": True,   # custom checkbox — add if desired
        }
    )


# ── Decision tree ─────────────────────────────────────────────────────────────

NODES: dict[str, Node] = {

    # ── Entry point ───────────────────────────────────────────────────────────
    "opening": Node(
        id="opening",
        label="Opening ticket card  [standard — no recent chatter]",
        trigger="Bot sends morning digest  |  ticket.use_one_tap == False",
        message_fn="block_opening(first_name, total, index, ticket)",
        sf_writes=[],
        next_nodes=["connected_yes", "connected_no"],
    ),

    "one_tap_confirm": Node(
        id="one_tap_confirm",
        label="One-tap card  [ticket has chatter 3–7 days old]",
        trigger="Bot sends morning digest  |  ticket.use_one_tap == True",
        message_fn="block_one_tap_confirm(first_name, total, index, ticket)",
        sf_writes=[],
        next_nodes=["one_tap_active", "one_tap_update", "one_tap_close"],
    ),

    # ── One-tap branch ────────────────────────────────────────────────────────
    "one_tap_active": Node(
        id="one_tap_active",
        label="SE confirmed still active — no note required",
        trigger="Button: ✅ Confirm Active  (action_id: one_tap_active__{ticket_id})",
        message_fn="block_one_tap_confirmed(ticket)",
        sf_writes=[],          # No new chatter post — existing note is sufficient
        next_nodes=["advance"],
        terminal=False,
    ),

    "one_tap_update": Node(
        id="one_tap_update",
        label="SE wants to add a new update — opens Still Active modal",
        trigger="Button: 📝 Add Update  (action_id: one_tap_update__{ticket_id})",
        message_fn="block_still_active_modal(ticket)   ← opens as modal",
        sf_writes=[],
        next_nodes=["submit_active"],
        requires_input=True,
    ),

    "one_tap_close": Node(
        id="one_tap_close",
        label="SE closing from one-tap card — opens Close Ticket modal",
        trigger="Button: 🔒 Close Ticket  (action_id: one_tap_close__{ticket_id})",
        message_fn="block_close_ticket_modal(ticket)   ← opens as modal",
        sf_writes=[],
        next_nodes=["submit_close"],
        requires_input=True,
    ),

    # ── Branch A: SE has connected ────────────────────────────────────────────
    "connected_yes": Node(
        id="connected_yes",
        label="SE clicked Yes — connected with requester",
        trigger="Button: ✅ Yes  (action_id: connected_yes__{ticket_id})",
        message_fn="block_connected_yes(ticket)",
        sf_writes=[],
        next_nodes=["status_active", "status_close"],
    ),

    # Branch A1: Still Active
    "status_active": Node(
        id="status_active",
        label="SE clicked Still Active — modal opens for next steps note",
        trigger="Button: 🔄 Still Active  (action_id: status_active__{ticket_id})",
        message_fn="block_still_active_modal(ticket)   ← opens as modal",
        sf_writes=[],
        next_nodes=["submit_active"],
        requires_input=True,
    ),

    "submit_active": Node(
        id="submit_active",
        label="SE submitted Still Active note",
        trigger="Modal submit: submit_update__{ticket_id}  (min 10 chars enforced by Slack)",
        message_fn="block_update_confirmed(ticket, note)",
        sf_writes=[
            sf_chatter_post("update"),
        ],
        next_nodes=["advance"],
        terminal=False,   # advances to next ticket
    ),

    # Branch A2: Close Ticket
    "status_close": Node(
        id="status_close",
        label="SE clicked Close Ticket — modal opens for resolution note",
        trigger="Button: 🔒 Close Ticket  (action_id: status_close__{ticket_id})",
        message_fn="block_close_ticket_modal(ticket)   ← opens as modal",
        sf_writes=[],
        next_nodes=["submit_close"],
        requires_input=True,
    ),

    "submit_close": Node(
        id="submit_close",
        label="SE submitted resolution note",
        trigger="Modal submit: submit_close__{ticket_id}  (min 10 chars enforced by Slack)",
        message_fn="block_close_confirmed(ticket, note)",
        sf_writes=[
            sf_chatter_post("resolution"),
            sf_close_ticket(),
        ],
        next_nodes=["advance"],
        terminal=False,   # advances to next ticket
    ),

    # ── Branch B: SE has NOT connected ───────────────────────────────────────
    "connected_no": Node(
        id="connected_no",
        label="SE clicked Not Yet — offer to DM opp owner",
        trigger="Button: ❌ Not Yet  (action_id: connected_no__{ticket_id})",
        message_fn="block_not_yet(ticket)",
        sf_writes=[],
        next_nodes=["dm_send", "dm_skip"],
    ),

    # Branch B1: Send DM
    "dm_send": Node(
        id="dm_send",
        label="Bot sends DM to opp owner, shows confirmation",
        trigger="Button: 📨 Send them a DM  (action_id: dm_send__{ticket_id})",
        message_fn="block_dm_sent(ticket, se_first_name)",
        sf_writes=[
            sf_dm_logged(),   # optional; remove if SE_DM_Sent__c field not added
        ],
        next_nodes=["advance"],
        terminal=False,
    ),

    # Branch B2: Skip
    "dm_skip": Node(
        id="dm_skip",
        label="SE skipped DM — no action, advance to next ticket",
        trigger="Button: ⏭️ Skip for now  (action_id: dm_skip__{ticket_id})",
        message_fn="(no new message — advance silently)",
        sf_writes=[],
        next_nodes=["advance"],
        terminal=False,
    ),

    # ── Advance / Complete ────────────────────────────────────────────────────
    "advance": Node(
        id="advance",
        label="Move to next ticket in the digest",
        trigger="(internal — no button click)",
        message_fn="block_opening(first_name, total, index+1, next_ticket)  OR  block_digest_complete()",
        sf_writes=[],
        next_nodes=["opening", "complete"],   # opening if tickets remain, else complete
        terminal=False,
    ),

    "complete": Node(
        id="complete",
        label="All tickets reviewed — digest complete",
        trigger="(last ticket resolved)",
        message_fn="block_digest_complete(first_name, total)",
        sf_writes=[],
        next_nodes=[],
        terminal=True,
    ),
}


# ── Path shortcuts for --path flag ────────────────────────────────────────────

PATHS = {
    "yes_active": ["opening", "connected_yes", "status_active", "submit_active", "advance", "complete"],
    "yes_close":  ["opening", "connected_yes", "status_close",  "submit_close",  "advance", "complete"],
    "no_dm":      ["opening", "connected_no",  "dm_send",                         "advance", "complete"],
    "no_skip":    ["opening", "connected_no",  "dm_skip",                          "advance", "complete"],
}


# ── Rendering ────────────────────────────────────────────────────────────────

def render_node(node: Node, indent: int = 0) -> str:
    pad = "  " * indent
    lines = [
        f"{pad}┌─ [{node.id.upper()}]  {node.label}",
        f"{pad}│  Trigger   : {node.trigger}",
        f"{pad}│  Message   : {node.message_fn}",
    ]

    if node.requires_input:
        lines.append(f"{pad}│  ⚠️  REQUIRES FREE-TEXT INPUT (modal, min 10 chars)")

    if node.sf_writes:
        lines.append(f"{pad}│  SF Writes :")
        for w in node.sf_writes:
            lines.append(f"{pad}│    • [{w.object} / {w.operation}]  {w.description}")
            for fname, fval in w.field_updates.items():
                lines.append(f"{pad}│        {fname}: {fval}")
    else:
        lines.append(f"{pad}│  SF Writes : (none)")

    if node.terminal:
        lines.append(f"{pad}│  ✅ TERMINAL — digest complete")
    elif node.next_nodes:
        lines.append(f"{pad}│  Next      : {' | '.join(node.next_nodes)}")

    lines.append(f"{pad}└{'─'*60}")
    return "\n".join(lines)


def render_path(path_name: str) -> str:
    node_ids = PATHS.get(path_name)
    if not node_ids:
        return f"Unknown path: {path_name}. Options: {', '.join(PATHS)}"

    lines = [
        f"\n{'═'*62}",
        f"PATH: {path_name.upper()}",
        f"{'═'*62}",
    ]
    for i, nid in enumerate(node_ids):
        node = NODES.get(nid)
        if node:
            lines.append(render_node(node, indent=i))
        else:
            lines.append(f"  [unknown node: {nid}]")
    return "\n".join(lines)


def render_full_tree() -> str:
    lines = [
        "\n" + "═"*62,
        "SE TICKET DIGEST — FULL BRANCHING LOGIC MAP",
        "═"*62,
        "",
        "CONVERSATION PATHS",
        "──────────────────",
        "  A.  Yes → Still Active  → submit note → next ticket",
        "  B.  Yes → Close Ticket  → submit note → next ticket",
        "  C.  No  → Send DM       → confirm     → next ticket",
        "  D.  No  → Skip          → (silent)    → next ticket",
        "",
        "ALL NODES",
        "──────────────────",
    ]
    for node in NODES.values():
        lines.append(render_node(node, indent=0))
        lines.append("")

    lines += [
        "ONE-TAP ROUTING",
        "──────────────────",
        "  ticket.use_one_tap == True  → block_one_tap_confirm  (chatter 3–7d old)",
        "  ticket.use_one_tap == False → block_opening           (standard flow)",
        "",
        "SALESFORCE WRITES SUMMARY",
        "──────────────────────────",
        "  Path A (Yes → Still Active):",
        "    1. FeedItem INSERT on Ticket__c  ← SE Update note",
        "    (ticket stays open)",
        "",
        "  Path B (Yes → Close Ticket):",
        "    1. FeedItem INSERT on Ticket__c  ← SE Resolution note",
        "    2. Ticket__c UPDATE  Status__c → Closed",
        "",
        "  Path C (No → Send DM):",
        "    1. Slack API: chat.postMessage to opp owner's DM channel",
        "    2. Ticket__c UPDATE  SE_DM_Sent__c → True  (optional)",
        "",
        "  Path D (No → Skip):",
        "    (no Salesforce writes)",
        "",
        "ACTION_ID ROUTING TABLE",
        "────────────────────────",
        "  connected_yes__{ticket_id}   → node: connected_yes",
        "  connected_no__{ticket_id}    → node: connected_no",
        "  status_active__{ticket_id}   → node: status_active  (opens modal)",
        "  status_close__{ticket_id}    → node: status_close   (opens modal)",
        "  submit_update__{ticket_id}   → node: submit_active  (modal callback)",
        "  submit_close__{ticket_id}    → node: submit_close   (modal callback)",
        "  dm_send__{ticket_id}         → node: dm_send",
        "  dm_skip__{ticket_id}         → node: dm_skip",
    ]
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Print the SE digest branching logic map."
    )
    parser.add_argument("--ticket", type=str, help="Ticket ID to display in examples")
    parser.add_argument("--path",   type=str, help=f"Show one path: {', '.join(PATHS)}")
    args = parser.parse_args()

    if args.path:
        print(render_path(args.path))
    else:
        print(render_full_tree())

    # If a ticket ID was given, show what the action_ids would look like
    if args.ticket:
        tid = args.ticket
        print(f"\n{'━'*62}")
        print(f"ACTION IDs FOR: {tid}")
        print("━"*62)
        action_ids = [
            f"connected_yes__{tid}",
            f"connected_no__{tid}",
            f"status_active__{tid}",
            f"status_close__{tid}",
            f"submit_update__{tid}",
            f"submit_close__{tid}",
            f"dm_send__{tid}",
            f"dm_skip__{tid}",
            f"one_tap_active__{tid}",
            f"one_tap_update__{tid}",
            f"one_tap_close__{tid}",
        ]
        for a in action_ids:
            print(f"  {a}")


if __name__ == "__main__":
    main()
