#!/usr/bin/env python3
"""
se-slack-bot/sf_client.py

Salesforce API client.
Authenticates by exchanging the refresh token from SFDX_AUTH_URL for a live
access token — no sf CLI, no username/password, works with SSO.
"""

import os
import requests as _requests

from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceError

SFDX_AUTH_URL = os.environ.get("SFDX_AUTH_URL", "")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _parse_sfdx_auth_url(auth_url: str) -> dict:
    """
    Parse force://clientId:clientSecret:refreshToken@instanceHost
    Returns dict with client_id, client_secret, refresh_token, instance_url.
    """
    url      = auth_url.replace("force://", "")
    at_idx   = url.rfind("@")
    creds    = url[:at_idx]
    host     = url[at_idx + 1:]
    parts    = creds.split(":", 2)
    return {
        "client_id":     parts[0],
        "client_secret": parts[1] if len(parts) > 1 else "",
        "refresh_token": parts[2] if len(parts) > 2 else "",
        "instance_url":  f"https://{host}",
    }


def _get_access_token(parsed: dict) -> str:
    """Exchange the refresh token for a fresh access token."""
    data = {
        "grant_type":    "refresh_token",
        "client_id":     parsed["client_id"],
        "refresh_token": parsed["refresh_token"],
    }
    if parsed.get("client_secret"):
        data["client_secret"] = parsed["client_secret"]

    resp = _requests.post(
        f"{parsed['instance_url']}/services/oauth2/token",
        data=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_client() -> Salesforce:
    """Returns an authenticated Salesforce client. Called fresh each request."""
    parsed       = _parse_sfdx_auth_url(SFDX_AUTH_URL)
    access_token = _get_access_token(parsed)
    return Salesforce(
        instance_url=parsed["instance_url"],
        session_id=access_token,
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
