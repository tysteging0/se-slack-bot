#!/usr/bin/env python3
"""
se-slack-bot/dashboard_refresh.py

Pulls SE ticket data from Salesforce, injects it into the HTML template,
and writes the result to the persistent disk so /dashboard can serve it.

No sf CLI required — uses OAuth via sf_client.py.
"""

import json
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

from sf_client import run_soql

# ── Config ────────────────────────────────────────────────────────────────────

# Template lives in the repo (read-only source of truth)
TEMPLATE_FILE = Path(__file__).parent / "se-ticket-trends.html"

# Output goes to persistent disk so /dashboard can serve the latest version
SESSION_DIR = Path(os.environ.get("SESSION_DIR", "/data"))
OUTPUT_FILE = SESSION_DIR / "se-ticket-trends.html"

PLACEHOLDER = "const PRELOADED = null; // INJECTED_BY_REFRESH_SCRIPT"

FY_START = "2025-05-01T00:00:00Z"
FY_END   = "2026-05-01T00:00:00Z"

OWNERS    = ["Michaël Vasseur", "Willow Turano"]
FQ_ORDER  = ["FQ1 FY2026", "FQ2 FY2026", "FQ3 FY2026", "FQ4 FY2026"]
ROLE_ORDER = ["Gusto Pro", "Mid-Market", "Core Sales", "Biz Dev", "Other"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def classify_role(role_name: str) -> str:
    if not role_name:
        return "Other"
    r = role_name.lower()
    if "_bd_" in r:                                    return "Biz Dev"
    if "_mm_" in r:                                    return "Mid-Market"
    if "_sb_" in r or "small_biz" in r:                return "Core Sales"
    if "partner" in r or "_sam_" in r or "_am_" in r or "_ae_" in r:
                                                       return "Gusto Pro"
    return "Other"


def fiscal_quarter(d: date) -> str:
    y, m = d.year, d.month
    if y == 2025 and m in (5, 6, 7):                             return "FQ1 FY2026"
    if y == 2025 and m in (8, 9, 10):                            return "FQ2 FY2026"
    if (y == 2025 and m in (11, 12)) or (y == 2026 and m == 1): return "FQ3 FY2026"
    if y == 2026 and m in (2, 3, 4):                             return "FQ4 FY2026"
    return "Other"


def opp_category(stage) -> str:
    if not stage:               return "No Opp Attached"
    if stage == "Closed Won":   return "Closed Won"
    if stage == "Closed Lost":  return "Closed Lost"
    return "Open / In Progress"


def parse_sf_date(s: str) -> date:
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s).date()


# ── Main refresh ──────────────────────────────────────────────────────────────

def run_refresh() -> dict:
    """
    Pull data from Salesforce, inject into se-ticket-trends.html,
    and publish to share-some-html.

    Returns a summary dict: {tickets, won, mrr, version, refreshed_at}
    Raises on fatal errors.
    """
    print("SE Dashboard Refresh — starting")

    # ── 1. Main FY2026 tickets ────────────────────────────────────────────────
    soql = (
        "SELECT Name, Owner.Name, Status__c, CreatedDate, "
        "Assigned_to_User_At__c, Time_to_Resolution__c, "
        "Opportunity__c, "
        "Opportunity__r.StageName, Opportunity__r.Amount, "
        "Opportunity__r.Owner.UserRole.Name "
        "FROM Ticket__c "
        "WHERE RecordType.Name = 'Solution Engineer Request' "
        "AND Owner.Name IN ('Michaël Vasseur', 'Willow Turano') "
        f"AND Assigned_to_User_At__c >= {FY_START} "
        f"AND Assigned_to_User_At__c < {FY_END} "
        "ORDER BY Assigned_to_User_At__c ASC"
    )
    records = run_soql(soql)
    print(f"  Main tickets: {len(records)}")

    seen_opp_ids: set = set()
    tickets = []
    for r in records:
        assigned = parse_sf_date(r["Assigned_to_User_At__c"])
        opp      = r.get("Opportunity__r") or {}
        opp_id   = r.get("Opportunity__c")
        stage    = opp.get("StageName") or None

        if opp_id and opp_id not in seen_opp_ids:
            amount = float(opp.get("Amount") or 0)
            seen_opp_ids.add(opp_id)
        else:
            amount = 0.0

        role_raw = None
        opp_owner = opp.get("Owner") or {}
        if opp_owner:
            user_role = opp_owner.get("UserRole") or {}
            role_raw  = user_role.get("Name")

        res_raw = r.get("Time_to_Resolution__c")
        res_hrs = float(res_raw) if res_raw is not None else None
        status  = r.get("Status__c", "")
        closed  = status in ("Resolved", "Closed")
        cat     = opp_category(stage)
        if not closed and cat in ("Closed Won", "Closed Lost"):
            cat = "Open / In Progress"

        tickets.append({
            "owner":    r["Owner"]["Name"],
            "created":  assigned,
            "month":    date(assigned.year, assigned.month, 1).isoformat(),
            "fq":       fiscal_quarter(assigned),
            "stage":    stage,
            "category": cat,
            "amount":   amount,
            "role_seg": classify_role(role_raw),
            "res_hrs":  res_hrs,
            "closed":   closed,
        })

    # ── 2. Monthly aggregation ────────────────────────────────────────────────
    monthly_map: dict = defaultdict(lambda: {
        "TOTAL_TICKETS": 0, "NO_OPP": 0, "WITH_OPP": 0, "WON": 0, "LOST": 0
    })
    for t in tickets:
        key = (t["owner"], t["month"])
        m   = monthly_map[key]
        m["TOTAL_TICKETS"] += 1
        if t["closed"]:
            if t["stage"] is None: m["NO_OPP"] += 1
            else:                   m["WITH_OPP"] += 1
            if t["category"] == "Closed Won":   m["WON"]  += 1
            elif t["category"] == "Closed Lost": m["LOST"] += 1

    monthly = []
    for (owner, month), m in sorted(monthly_map.items(), key=lambda x: (x[0][1], x[0][0])):
        wo  = m["WITH_OPP"]
        won = m["WON"]
        monthly.append({
            "OWNER":         owner,
            "MONTH":         month,
            "TOTAL_TICKETS": m["TOTAL_TICKETS"],
            "NO_OPP":        m["NO_OPP"],
            "WITH_OPP":      wo,
            "WON":           won,
            "LOST":          m["LOST"],
            "WIN_RATE_PCT":  round(won / wo * 100, 1) if wo else 0,
            "LOST_RATE_PCT": round(m["LOST"] / wo * 100, 1) if wo else 0,
        })

    # ── 3. Quarterly aggregation ──────────────────────────────────────────────
    qmap: dict = defaultdict(lambda: {
        "TOTAL_TICKETS": 0, "WITH_OPP": 0, "OPEN_WITH_OPP": 0,
        "OPEN_NO_OPP": 0, "CLOSED_NO_OPP": 0,
        "WON": 0, "LOST": 0, "hrs": []
    })
    for t in tickets:
        key = (t["owner"], t["fq"])
        q   = qmap[key]
        q["TOTAL_TICKETS"] += 1
        if t["stage"] is None:
            if t["closed"]: q["CLOSED_NO_OPP"] += 1
            else:           q["OPEN_NO_OPP"]   += 1
        elif t["closed"]:   q["WITH_OPP"]       += 1
        else:               q["OPEN_WITH_OPP"]  += 1
        if t["closed"]:
            if t["category"] == "Closed Won":    q["WON"]  += 1
            elif t["category"] == "Closed Lost": q["LOST"] += 1
        if t["res_hrs"] is not None:
            q["hrs"].append(t["res_hrs"])

    quarterly = []
    for fq in FQ_ORDER:
        for owner in OWNERS:
            key = (owner, fq)
            if key not in qmap:
                continue
            q   = qmap[key]
            wo  = q["WITH_OPP"]
            won = q["WON"]
            hrs = q["hrs"]
            quarterly.append({
                "OWNER":                 owner,
                "FISCAL_QUARTER":        fq,
                "TOTAL_TICKETS":         q["TOTAL_TICKETS"],
                "WITH_OPP":              wo,
                "OPEN_WITH_OPP":         q["OPEN_WITH_OPP"],
                "OPEN_NO_OPP":           q["OPEN_NO_OPP"],
                "CLOSED_NO_OPP":         q["CLOSED_NO_OPP"],
                "WON":                   won,
                "LOST":                  q["LOST"],
                "WIN_RATE_PCT":          round(won / wo * 100, 1) if wo else 0,
                "MEDIAN_RESOLUTION_HRS": round(statistics.median(hrs), 3) if hrs else None,
            })

    # ── 4. Opp breakdown + role segments (all + per FQ) ──────────────────────
    def build_opp_break(ticket_list):
        opp_map: dict = defaultdict(int)
        for t in ticket_list:
            stage = t["stage"] or "No Opp Attached"
            opp_map[(t["owner"], stage, t["category"])] += 1
        return sorted(
            [{"OWNER": o, "OPP_STAGE": s, "OPP_CATEGORY": c, "TICKET_COUNT": n}
             for (o, s, c), n in opp_map.items()],
            key=lambda x: (x["OWNER"], x["OPP_CATEGORY"], x["OPP_STAGE"])
        )

    def build_role_segs(ticket_list):
        rmap: dict = defaultdict(lambda: {"TOTAL_TICKETS": 0, "WON": 0, "LOST": 0, "OPEN": 0, "MRR": 0.0})
        for t in ticket_list:
            key = (t["owner"], t["role_seg"])
            r   = rmap[key]
            r["TOTAL_TICKETS"] += 1
            if t["closed"]:
                if t["category"] == "Closed Won":    r["WON"]  += 1; r["MRR"] += t["amount"]
                elif t["category"] == "Closed Lost": r["LOST"] += 1
                else:                                r["OPEN"] += 1
            else:
                r["OPEN"] += 1
        result = []
        for owner in OWNERS:
            for seg in ROLE_ORDER:
                key = (owner, seg)
                if key not in rmap:
                    continue
                r     = rmap[key]
                total = r["TOTAL_TICKETS"]
                won   = r["WON"]
                result.append({
                    "OWNER": owner, "ROLE_SEGMENT": seg,
                    "TOTAL_TICKETS": total, "WON": won,
                    "LOST": r["LOST"], "OPEN": r["OPEN"],
                    "WIN_RATE_PCT": round(won / total * 100, 1) if total else 0,
                    "MRR_CLOSED":  round(r["MRR"]),
                })
        return result

    oppBreak     = build_opp_break(tickets)
    roleSegs     = build_role_segs(tickets)
    oppBreakByFQ = {fq: build_opp_break([t for t in tickets if t["fq"] == fq]) for fq in FQ_ORDER}
    roleSegsByFQ = {fq: build_role_segs([t for t in tickets if t["fq"] == fq]) for fq in FQ_ORDER}

    # ── 5. Premium pilot tickets ──────────────────────────────────────────────
    premium_soql = (
        "SELECT Name, Owner.Name, Status__c, CreatedDate, "
        "Opportunity__c, Opportunity__r.Name, "
        "Opportunity__r.StageName, Opportunity__r.Amount "
        "FROM Ticket__c "
        "WHERE RecordType.Name = 'Solution Engineer Request' "
        "AND Owner.Name IN ('Michaël Vasseur', 'Willow Turano') "
        "AND Status__c IN ('New', 'In Progress') "
        "AND Other_Integration_Type_Details__c = 'Premium Pilot' "
        "ORDER BY CreatedDate DESC"
    )
    prem_records = run_soql(premium_soql)
    print(f"  Premium tickets: {len(prem_records)}")

    seen_prem_opps: set = set()
    premium = []
    for r in prem_records:
        opp    = r.get("Opportunity__r") or {}
        opp_id = r.get("Opportunity__c")
        stage  = opp.get("StageName") or "No Opp"
        raw_amt = float(opp.get("Amount") or 0)
        amt = 0.0
        if opp_id and opp_id not in seen_prem_opps:
            amt = raw_amt
            seen_prem_opps.add(opp_id)
        created_date = parse_sf_date(r["CreatedDate"])
        premium.append({
            "TICKET":    r["Name"],
            "OWNER":     r["Owner"]["Name"],
            "STATUS":    r["Status__c"],
            "OPP_NAME":  opp.get("Name") or "—",
            "OPP_STAGE": stage,
            "AMOUNT":    raw_amt,
            "CREATED_FQ": fiscal_quarter(created_date),
        })

    # ── 6. Reseller tickets ───────────────────────────────────────────────────
    reseller_soql = (
        "SELECT Name, Owner.Name, Status__c, CreatedDate, "
        "Account__r.Name, "
        "Opportunity__c, Opportunity__r.Name, Opportunity__r.StageName, "
        "Opportunity__r.RecordType.Name, "
        "Opportunity__r.Total_Clients__c, "
        "Opportunity__r.Clients_Migrated__c, "
        "Opportunity__r.Clients_to_Migrate__c, "
        "Opportunity__r.Forecasted_MRR__c, "
        "Opportunity__r.Opportunity_MRR__c "
        "FROM Ticket__c "
        "WHERE RecordType.Name = 'Solution Engineer Request' "
        "AND Owner.Name IN ('Michaël Vasseur', 'Willow Turano') "
        "AND Account__r.Type = 'Reseller' "
        f"AND CreatedDate >= {FY_START} "
        f"AND CreatedDate < {FY_END} "
        "ORDER BY Account__r.Name ASC"
    )
    reseller_records = run_soql(reseller_soql)
    print(f"  Reseller tickets: {len(reseller_records)}")

    seen_res_opps: set = set()
    seen_res_opps_by_fq: dict = defaultdict(set)

    def _make_res_entry():
        return {
            "owners": set(), "tickets": 0, "no_opp": 0, "company_opp": 0,
            "total_clients": 0.0, "clients_migrated": 0.0,
            "clients_to_migrate": 0.0, "clients_lost": 0.0,
            "forecasted_mrr": 0.0, "opportunity_mrr": 0.0,
        }

    res_map: dict = defaultdict(_make_res_entry)
    res_map_by_fq: dict = {fq: defaultdict(_make_res_entry) for fq in FQ_ORDER}

    for r in reseller_records:
        acct   = (r.get("Account__r") or {}).get("Name", "Unknown")
        owner  = r["Owner"]["Name"]
        opp    = r.get("Opportunity__r") or {}
        opp_id = r.get("Opportunity__c")
        stage  = opp.get("StageName")
        fq     = fiscal_quarter(parse_sf_date(r.get("CreatedDate", "")))

        d = res_map[acct]
        d["owners"].add(owner)
        d["tickets"] += 1
        if fq in res_map_by_fq:
            dfq = res_map_by_fq[fq][acct]
            dfq["owners"].add(owner)
            dfq["tickets"] += 1

        if not opp_id:
            d["no_opp"] += 1
            if fq in res_map_by_fq:
                res_map_by_fq[fq][acct]["no_opp"] += 1
            continue

        opp_rt = ((opp.get("RecordType") or {}).get("Name") or "")
        if opp_rt and opp_rt != "Reseller Opportunity":
            d["company_opp"] += 1
            if fq in res_map_by_fq:
                res_map_by_fq[fq][acct]["company_opp"] += 1

        if opp_id not in seen_res_opps:
            seen_res_opps.add(opp_id)
            tc  = float(opp.get("Total_Clients__c")    or 0)
            mc  = float(opp.get("Clients_Migrated__c") or 0)
            tmc = float(opp.get("Clients_to_Migrate__c") or 0)
            fm  = float(opp.get("Forecasted_MRR__c")   or 0)
            om  = float(opp.get("Opportunity_MRR__c")  or 0)
            d["total_clients"]      += tc
            d["clients_migrated"]   += mc
            d["clients_to_migrate"] += tmc
            d["forecasted_mrr"]     += fm
            d["opportunity_mrr"]    += om
            if stage == "Closed Lost":
                d["clients_lost"] += tc

        if fq in res_map_by_fq and opp_id not in seen_res_opps_by_fq[fq]:
            seen_res_opps_by_fq[fq].add(opp_id)
            tc  = float(opp.get("Total_Clients__c")    or 0)
            mc  = float(opp.get("Clients_Migrated__c") or 0)
            tmc = float(opp.get("Clients_to_Migrate__c") or 0)
            fm  = float(opp.get("Forecasted_MRR__c")   or 0)
            om  = float(opp.get("Opportunity_MRR__c")  or 0)
            dfq = res_map_by_fq[fq][acct]
            dfq["total_clients"]      += tc
            dfq["clients_migrated"]   += mc
            dfq["clients_to_migrate"] += tmc
            dfq["forecasted_mrr"]     += fm
            dfq["opportunity_mrr"]    += om
            if stage == "Closed Lost":
                dfq["clients_lost"] += tc

    def _build_reseller_list(rm):
        result = []
        for acct, d in sorted(rm.items(), key=lambda x: (-x[1]["tickets"], x[0])):
            owners_list = sorted(d["owners"])
            result.append({
                "PARTNER":            acct,
                "OWNER":              "Both" if len(owners_list) > 1 else owners_list[0],
                "TICKETS":            d["tickets"],
                "NO_OPP":             d["no_opp"],
                "COMPANY_OPP":        d["company_opp"],
                "TOTAL_CLIENTS":      int(d["total_clients"]),
                "CLIENTS_MIGRATED":   int(d["clients_migrated"]),
                "CLIENTS_TO_MIGRATE": int(d["clients_to_migrate"]),
                "CLIENTS_LOST":       int(d["clients_lost"]),
                "FORECASTED_MRR":     round(d["forecasted_mrr"], 2),
                "OPPORTUNITY_MRR":    round(d["opportunity_mrr"], 2),
            })
        return result

    reseller      = _build_reseller_list(res_map)
    reseller_by_fq = {fq: _build_reseller_list(res_map_by_fq[fq]) for fq in FQ_ORDER}

    # ── 7. Build payload ──────────────────────────────────────────────────────
    refreshed_at = datetime.now().strftime("%B %-d, %Y at %-I:%M %p")
    data_payload = {
        "monthly":      monthly,
        "quarterly":    quarterly,
        "oppBreak":     oppBreak,
        "roleSegs":     roleSegs,
        "oppBreakByFQ": oppBreakByFQ,
        "roleSegsByFQ": roleSegsByFQ,
        "premium":      premium,
        "reseller":     reseller,
        "resellerByFQ": reseller_by_fq,
        "refreshedAt":  refreshed_at,
    }

    injection = (
        f"const PRELOADED = {json.dumps(data_payload, default=str)};"
        " // INJECTED_BY_REFRESH_SCRIPT"
    )

    # ── 8. Read template, inject data, write to persistent disk ──────────────
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"HTML template not found: {TEMPLATE_FILE}")

    html = TEMPLATE_FILE.read_text(encoding="utf-8")
    html = re.sub(
        r"const PRELOADED = .*?; // INJECTED_BY_REFRESH_SCRIPT",
        PLACEHOLDER,
        html,
        flags=re.DOTALL,
    )
    if PLACEHOLDER not in html:
        raise RuntimeError("Could not locate PRELOADED placeholder in HTML.")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html.replace(PLACEHOLDER, injection), encoding="utf-8")
    print(f"  Written → {OUTPUT_FILE}")

    total = len(tickets)
    won   = sum(1 for t in tickets if t["category"] == "Closed Won")
    mrr   = sum(t["amount"] for t in tickets if t["category"] == "Closed Won")

    print(f"  Done — {total} tickets, {won} won, ${mrr:,.0f} MRR")
    return {"tickets": total, "won": won, "mrr": mrr, "refreshed_at": refreshed_at}
