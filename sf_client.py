#!/usr/bin/env python3
"""
se-slack-bot/sf_client.py

Salesforce API client.
Authenticates using the sf CLI stored credentials (works with SSO).
The SFDX_AUTH_URL env var is used on Render to restore the session;
locally it falls back to the already-authenticated sf CLI.
"""

import json
import os
import subprocess

from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceError

SF_ORG = "gusto-prod"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_credentials() -> dict:
    """
    Get a live access token + instance URL from the sf CLI.
    The CLI handles token refresh automatically — no password needed.
    """
    result = subprocess.run(
        ["sf", "org", "display", "--target-org", SF_ORG, "--json"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    r    = data["result"]
    return {
        "instance_url": r["instanceUrl"],
        "access_token": r["accessToken"],
    }


def get_client() -> Salesforce:
    """
    Returns an authenticated Salesforce client using the sf CLI session.
    Called fresh on each request — sf CLI refreshes the token as needed.
    """
    creds = _get_credentials()
    return Salesforce(
        instance_url=creds["instance_url"],
        session_id=creds["access_token"],
    )


# ── Query ─────────────────────────────────────────────────────────────────────

def run_soql(query: str) -> list:
    """Execute a SOQL query and return records."""
    try:
        sf     = get_client()
        result = sf.query_all(query)
        return result.get("records", [])
    except SalesforceError as e:
        print(f"[SF ERROR] SOQL failed: {e}")
        return []
    except Exception as e:
        print(f"[SF ERROR] Unexpected: {e}")
        return []


# ── Record writes ─────────────────────────────────────────────────────────────

def update_record(sobject: str, record_id: str, fields: dict) -> bool:
    """Update a single Salesforce record. Returns True on success."""
    try:
        sf  = get_client()
        obj = getattr(sf, sobject)
        obj.update(record_id, fields)
        return True
    except SalesforceError as e:
        print(f"[SF ERROR] Update {sobject} {record_id} failed: {e}")
        return False


def insert_record(sobject: str, fields: dict) -> str | None:
    """Insert a new Salesforce record. Returns the new Id on success."""
    try:
        sf  = get_client()
        obj = getattr(sf, sobject)
        r   = obj.create(fields)
        return r.get("id")
    except SalesforceError as e:
        print(f"[SF ERROR] Insert {sobject} failed: {e}")
        return None


def insert_chatter_post(parent_id: str, body: str) -> bool:
    """Post a chatter message (FeedItem) to a record."""
    record_id = insert_record("FeedItem", {
        "ParentId": parent_id,
        "Body":     body,
        "Type":     "TextPost",
    })
    return record_id is not None
