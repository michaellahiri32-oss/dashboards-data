#!/usr/bin/env python3
"""
build.py — build data.json for the leads tracker from a Connex calls CSV.

Discover which disposition is a lead:
    python build.py --csv calls.csv --list-outcomes
Then build (LEAD_OUTCOMES is already set to Hire & Repair):
    python build.py --csv calls.csv
    python build.py --csv calls.csv --day 2026-07-08

Export MONTH-TO-DATE so Today and Month both build from one file. CSV timestamps
are Australian-stamped (+10/+11); each row's offset is converted to UK per-row.
(The API version, build_api.py, gets UK times directly and shares the logic below.)

>>> Only the CONFIG block needs editing. <<<
"""

import argparse, csv, json, re, sys
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

# ============================ CONFIG ============================

TITLE = "UK Accident Management — Leads Tracker"
UK = ZoneInfo("Europe/London")

# Which disposition(s) count as a LEAD. Matching ignores case/spacing/punctuation,
# so "Hire & Repair Uk" also matches "Hire & Repair - UK" / "HIRE & REPAIR UK".
LEAD_CODES    = set()
LEAD_OUTCOMES = {"Hire & Repair Uk"}
# If BOTH are empty, every call is counted (with a warning) so you can still test.

# Column names in the Connex calls export (same for CSV and API responses).
COL_AGENT   = "agent_name"
COL_TIME    = "date"
COL_REF     = "contact_id"        # shown in the lead list; swap to "lead_reference" if preferred
COL_CODE    = "outcome_code"
COL_OUTCOME = "outcome"
COL_FIRST   = "first_name"
COL_LAST    = "last_name"
COL_CAMPAIGN= "campaign_name"

ONLY_CAMPAIGN  = ""       # restrict to one campaign_name, or "" for all rows in the file
EXCLUDE_AGENTS = set()    # usernames/agents to keep off the board
ROSTER = []               # always-show these names even on zero; [] = just who's in the file

# ===============================================================

_LEAD_CODES_N    = {re.sub(r"[^a-z0-9]+", "", x.lower()) for x in LEAD_CODES}
_LEAD_OUTCOMES_N = {re.sub(r"[^a-z0-9]+", "", x.lower()) for x in LEAD_OUTCOMES}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def is_lead(row):
    if not _LEAD_CODES_N and not _LEAD_OUTCOMES_N:
        return True
    if _LEAD_CODES_N and _norm(row.get(COL_CODE)) in _LEAD_CODES_N:
        return True
    if _LEAD_OUTCOMES_N and _norm(row.get(COL_OUTCOME)) in _LEAD_OUTCOMES_N:
        return True
    return False


def parse_dt(raw):
    """Parse a timestamp to UK time. Handles a bare '+10' offset and naive UK times."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r'([+-]\d{2})(?::?(\d{2}))?$', raw)
    if m and m.group(2) is None:
        raw = raw[:m.start()] + m.group(1) + ':00'
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UK)
    return dt.astimezone(UK)


def aggregate(rows, report_day, now_uk):
    """Shared core: turn call rows into the data.json dict. Used by CSV and API builds."""
    month_key = report_day.strftime("%Y-%m")
    agents = {}

    def ensure(name):
        if name not in agents:
            agents[name] = {"name": name, "id": slugify(name),
                            "leadsToday": 0, "leadsMonth": 0, "leads": []}
        return agents[name]

    for name in ROSTER:
        ensure(name)

    skipped = 0
    for row in rows:
        if ONLY_CAMPAIGN and (row.get(COL_CAMPAIGN) or "").strip() != ONLY_CAMPAIGN:
            continue
        name = (row.get(COL_AGENT) or "").strip()
        if not name or name in EXCLUDE_AGENTS:
            skipped += 1; continue
        dt = parse_dt(row.get(COL_TIME))
        if dt is None:
            skipped += 1; continue
        if not is_lead(row):
            ensure(name)
            continue
        a = ensure(name)
        d = dt.date()
        if d.strftime("%Y-%m") == month_key:
            a["leadsMonth"] += 1
        if d == report_day:
            a["leadsToday"] += 1
            cust = " ".join(x for x in [(row.get(COL_FIRST) or "").strip(),
                                        (row.get(COL_LAST) or "").strip()] if x)
            lead = {"ref": (row.get(COL_REF) or "").strip(), "time": dt.strftime("%H:%M")}
            if cust: lead["name"] = cust
            st = (row.get(COL_OUTCOME) or "").strip()
            if st: lead["status"] = st
            a["leads"].append(lead)

    for a in agents.values():
        a["leads"].sort(key=lambda l: l["time"])

    data = {
        "title": TITLE,
        "updated": now_uk.replace(microsecond=0).isoformat(),
        "period": {"day": report_day.isoformat(), "month": month_key},
        "agents": sorted(agents.values(), key=lambda a: (-a["leadsMonth"], a["name"])),
    }
    return data, skipped


def open_csv(path):
    try:
        f = open(path, newline="", encoding="utf-8-sig")
    except FileNotFoundError:
        sys.exit(f"CSV not found: {path}")
    sample = f.read(4096); f.seek(0)
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    return f, csv.DictReader(f, delimiter=delim)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--day")
    ap.add_argument("--out", default="data.json")
    ap.add_argument("--list-outcomes", action="store_true")
    args = ap.parse_args()

    f, reader = open_csv(args.csv)

    if args.list_outcomes:
        c = Counter()
        with f:
            for row in reader:
                c[(row.get(COL_CODE, "").strip(), row.get(COL_OUTCOME, "").strip())] += 1
        print(f"{'count':>7}  code / outcome")
        for (code, outc), n in c.most_common():
            print(f"{n:>7}  {code or '(blank)'}  /  {outc or '(blank)'}")
        return

    now_uk = datetime.now(UK)
    report_day = datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else now_uk.date()

    if not _LEAD_CODES_N and not _LEAD_OUTCOMES_N:
        print("WARNING: no lead filter set — counting every call.", file=sys.stderr)

    with f:
        if COL_AGENT not in (reader.fieldnames or []) or COL_TIME not in (reader.fieldnames or []):
            sys.exit("Missing columns. Headers: " + ", ".join(reader.fieldnames or []))
        data, skipped = aggregate(reader, report_day, now_uk)

    with open(args.out, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)

    tt = sum(a["leadsToday"] for a in data["agents"])
    tm = sum(a["leadsMonth"] for a in data["agents"])
    print(f"Wrote {args.out}: {tt} leads today, {tm} this month "
          f"({report_day}, UK). {len(data['agents'])} agents, {skipped} skipped.")


if __name__ == "__main__":
    main()
