#!/usr/bin/env python3
"""
se-slack-bot/sf_client.py

Salesforce API client for production use.
Replaces the sf CLI calls in digest.py and sf_writes.py with
proper server-side auth using simple-salesforce.

Auth: Username + Password + Security Token
      (swap for JWT Bearer once Connected App is set up)

Set these environment variables on your server:
    SF_USERNAME        your Salesforce username
    SF_PASSWORD        your Salesforce password
    SF_SECURITY_TOKEN  your Salesforce security token
                       (Settings → Reset My Security Token)
    SF_DOMAIN          "login" for production, "test" for sandbox
"""

import os
import json
from functools import lru_cache
from simple_salesforce import Salesforce, SalesforceLogin
from simple_salesforce.exceptions import SalesforceError


# ── Auth ──────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_client() -> Salesforce:
    """
    Returns an authenticated Salesforce client.
    Cached so we reuse the session across requests.
    Call get_client.cache_clear() if you need to force re-auth.
    """
    return Salesforce(
        username=os.environ["SF_USERNAME"],
        password=os.environ["SF_PASSWORD"],
        security_token=os.environ["SF_SECURITY_TOKEN"],
        domain=os.environ.get("SF_DOMAIN", "login"),
    )


# ── Query ─────────────────────────────────────────────────────────────────────

def run_soql(query: str) -> list:
    """
    Execute a SOQL query and return records.
    Drop-in replacement for digest.py's run_soql().
    """
    try:
        sf = get_client()
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
    """
    Update a single Salesforce record.
    Returns True on success, False on failure.
    """
    try:
        sf   = get_client()
        obj  = getattr(sf, sobject)
        obj.update(record_id, fields)
        return True
    except SalesforceError as e:
        print(f"[SF ERROR] Update {sobject} {record_id} failed: {e}")
        return False


def insert_record(sobject: str, fields: dict) -> str | None:
    """
    Insert a new Salesforce record.
    Returns the new record Id on success, None on failure.
    """
    try:
        sf  = get_client()
        obj = getattr(sf, sobject)
        r   = obj.create(fields)
        return r.get("id")
    except SalesforceError as e:
        print(f"[SF ERROR] Insert {sobject} failed: {e}")
        return None


def insert_chatter_post(parent_id: str, body: str) -> bool:
    """
    Post a chatter message (FeedItem) to a record.
    """
    record_id = insert_record("FeedItem", {
        "ParentId": parent_id,
        "Body":     body,
        "Type":     "TextPost",
    })
    return record_id is not None
