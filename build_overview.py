#!/usr/bin/env python3
"""
build_overview.py — tiny daily overview for management.

Produces overview.json with TODAY's:
  - inbound calls
  - instructs (= the report's deduped Hire & Repair claims)
  - total cost (today's Google Ads spend)
  - cost per instruct
…plus a per-section "last updated" timestamp, so managers can see how fresh
each number is (Connex builds on our schedule; Google Ads only refreshes hourly).

It pulls from the SAME Connex API and the SAME spend CSV as the main report, and
applies the SAME correction/dedup rules, so the figures reconcile with the report.

Publish overview.json to GitHub (like data.json); the page reads it from there.

  python build_overview.py            # today (UK)
  python build_overview.py --day 2026-07-22
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Python 3.9+ needed (zoneinfo).")

UK = ZoneInfo("Europe/London")

# ===================== CONFIG (keep in step with build_report_api.py) =====================
CAMPAIGN_ID   = "5bf2bf42-222f-40e5-95d5-82ada3807ffb"     # UK Accident Management
PAGE_SIZE     = 100
MAX_PAGES     = 600
CACHE_FILE    = "customer_phone_cache.json"                 # shared with the report

SPEND_CSV_URL = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vSEIOq_0n-xt5ownb0"
                 "G6rkMtcmeprrypsboXZ51HIwLnC8ml1qfkds5AMH6idkojgi1_nTHL-tmIkoD/pub"
                 "?gid=0&single=true&output=csv")

SALE_HIRE            = "Hire & Repair - UK"
SALE_TRANSFER        = "Transferred - UK"
SALE_TRANSFER_NUMBERS = {"447438475625", "447436471637"}
SALE_EXCLUDE_NUMBERS  = {"447423588163", "447340003433"}
FORCE_HR              = {"447432154233"}
DEDUP_EXEMPT          = {"447508635441"}
AGENT_REASSIGN        = {"447727606730": "James Bridgwood"}
# =========================================================================================


def env(n):
    v = os.environ.get(n)
    if not v: sys.exit(f"Missing env var {n}")
    return v

def get_token(E, cid, sec):
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
        "client_id": cid, "client_secret": sec}).encode()
    req = urllib.request.Request(E + "/oauth2/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]

def make_headers(tok, cxm):
    ct = cxm.strip(); xa = ct if ct.lower().startswith("basic ") else "Basic " + ct
    return {"Authorization": "Bearer " + tok, "X-Authorization": xa, "X-Timezone": "Europe/London"}

def api_get(E, H, path, params=None):
    url = E + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=90) as r:
        return json.load(r)

def paged(E, H, path, base):
    page = 1
    while page <= MAX_PAGES:
        p = dict(base); p["page[number]"] = str(page); p["page[size]"] = str(PAGE_SIZE)
        j = api_get(E, H, path, p)
        data = j.get("data") or []
        for d in data: yield d
        last = (j.get("meta") or {}).get("page", {}).get("last-page", page)
        if page >= last or not data: break
        page += 1

def parse_uk(ts):
    if not ts: return None
    try:
        s = ts.replace("Z", "+00:00")
        m = re.search(r"([+-]\d{2})$", s)
        if m: s = s + ":00"
        return datetime.fromisoformat(s).astimezone(UK)
    except Exception:
        return None

def norm_phone(s):
    return re.sub(r"\D", "", s or "")

def outcome_map(E, H):
    j = api_get(E, H, f"/campaign/{CAMPAIGN_ID}/outcome/selectable")
    return {o["id"]: o.get("name", "") for o in (j.get("data") or [])}

def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}

def fetch_spend(url):
    """Return ({date: spend}, stamp) from the published CSV; ({}, None) on failure.
    Retries once, because the sheet occasionally drops the connection transiently."""
    text = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                text = r.read().decode("utf-8-sig", "replace")
            break
        except Exception as e:
            if attempt == 0:
                time.sleep(3)          # transient blip — wait and retry once
                continue
            print(f"  (spend fetch failed: {e})")
            return {}, None            # NOTE: two values, matching the success path
    out = {}
    stamp = None
    stamp_col = None
    rows = [[p.strip() for p in line.split(",")] for line in text.splitlines()]
    for parts in rows:
        # Layout A: a "spend_updated" label with the time in the NEXT cell, same row.
        for i, cell in enumerate(parts):
            if cell.lower() == "spend_updated":
                if i + 1 < len(parts) and parts[i + 1]:
                    stamp = parts[i + 1]        # value sits beside the label
                stamp_col = i                    # or it's a header for this column
        if len(parts) < 2:
            continue
        d, raw = parts[0], parts[1]
        if not re.match(r"\d{4}-\d{2}-\d{2}", d):
            continue
        try:
            out[d] = round(float(re.sub(r"[£,\s]", "", raw)), 2)
        except Exception:
            pass
    # Layout B: "spend_updated" was a header column — grab the first non-empty value
    # in that column from the data rows.
    if stamp is None and stamp_col is not None:
        for parts in rows:
            if len(parts) > stamp_col:
                v = parts[stamp_col]
                if v and v.lower() != "spend_updated" and re.search(r"\d{4}-\d{2}-\d{2}T", v):
                    stamp = v
                    break
    # Layout C (last resort): any ISO timestamp anywhere in the sheet.
    if stamp is None:
        for parts in rows:
            for cell in parts:
                if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", cell):
                    stamp = cell
                    break
            if stamp:
                break
    return out, stamp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="YYYY-MM-DD (default: today UK)")
    ap.add_argument("--out", default="overview.json")
    ap.add_argument("--debug", action="store_true",
                    help="print scan diagnostics (how many records seen, date spread)")
    args = ap.parse_args()

    now_uk = datetime.now(UK)
    day = datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else now_uk.date()
    day_str = day.isoformat()

    E = env("CXM_ENDPOINT").rstrip("/")
    H = make_headers(get_token(E, env("CXM_CLIENT_ID"), env("CXM_SECRET")), env("CXM_TOKEN"))
    outcomes = outcome_map(E, H)
    cache = load_cache()

    # ---- pull today's interactions ----
    # The API doesn't accept a start_time range filter, and the feed isn't reliably
    # newest-first, so we scan pages and filter in code. We keep going until we've
    # seen plenty of OLDER records (proving we're well past today), rather than
    # stopping on the first older one.
    calls = 0
    recs = []
    seen = 0
    older = 0
    date_counts = {}
    for it in paged(E, H, "/interaction", {"filter[campaign_id]": CAMPAIGN_ID, "sort": "-start_time"}):
        dt = parse_uk(it.get("start_time"))
        if dt is None:
            continue
        seen += 1
        d = dt.date()
        if args.debug:
            dk = d.isoformat()
            date_counts[dk] = date_counts.get(dk, 0) + 1
        if d < day:
            older += 1
            if older >= 500:               # seen a big block of older data -> done
                break
            continue
        if d > day:
            continue
        outcome = outcomes.get(it.get("outcome_id"), "")
        if it.get("direction") == "inbound":
            calls += 1
        tels = cache.get(it.get("customer_id"), [])
        ph = next((t for t in tels if t), "")
        recs.append({
            "ms": dt.timestamp(),
            "outcome": outcome,
            "ph": ph,
            "match": next((t for t in tels if t in SALE_TRANSFER_NUMBERS), None),
            "excl": any(t in SALE_EXCLUDE_NUMBERS for t in tels),
            "sale": 0, "hrdup": 0,
        })

    # ---- apply the SAME instruct logic as the report ----
    # force-HR
    for x in recs:
        if x["ph"] in FORCE_HR and x["outcome"] != "No Answer - UK":
            x["outcome"] = SALE_HIRE
    # H&R sales
    for x in recs:
        if x["outcome"] == SALE_HIRE and not x["excl"]:
            x["sale"] = 1
    # transferred sales (matched numbers)
    for x in recs:
        if x["outcome"] == SALE_TRANSFER and x["match"]:
            x["sale"] = 1
    # dedup same-day H&R by phone (keep earliest)
    hk = {}
    for x in recs:
        if x["sale"] == 1 and x["outcome"] == SALE_HIRE and x["ph"] and x["ph"] not in DEDUP_EXEMPT:
            hk.setdefault(x["ph"], []).append(x)
    for lst in hk.values():
        lst.sort(key=lambda a: a["ms"])
        for x in lst[1:]: x["hrdup"] = 1

    instructs = sum(1 for x in recs if x["sale"] == 1 and not x["hrdup"])

    if args.debug:
        print(f"\n  [debug] scanned {seen} interactions total")
        print(f"  [debug] records kept for {day_str}: {len(recs)}")
        top = sorted(date_counts.items(), reverse=True)[:6]
        print("  [debug] interactions per date seen (newest first):")
        for dk, n in top:
            mark = "  <-- today" if dk == day_str else ""
            print(f"           {dk}: {n}{mark}")
        raw_hr = sum(1 for x in recs if x["outcome"] == SALE_HIRE)
        print(f"  [debug] raw H&R today (pre-dedup): {raw_hr}, deduped instructs: {instructs}")
        cached = sum(1 for x in recs if x["ph"])
        print(f"  [debug] records with a cached phone number: {cached}/{len(recs)}")
        print()

    # ---- spend ----
    spend_map, spend_stamp = fetch_spend(SPEND_CSV_URL)
    spend = spend_map.get(day_str, 0.0)
    cpi = round(spend / instructs, 2) if instructs else None

    now_iso = now_uk.replace(microsecond=0).isoformat()
    out = {
        "day": day_str,
        "calls": calls,
        "instructs": instructs,
        "spend": round(spend, 2),
        "cpi": cpi,
        # per-section freshness. Connex reflects THIS build; spend reflects the CSV's latest date.
        "updated": {
            "connex": now_iso,               # calls + instructs are as fresh as this run
            "spend_asof": now_iso,           # when we last READ the sheet
            "spend_updated": spend_stamp,    # when the Ads script last WROTE it (if stamped)
            "spend_latest_day": (max(spend_map) if spend_map else None),
        },
        "built": now_iso,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"{day_str}: calls {calls} · instructs {instructs} · "
          f"spend £{spend:,.2f} · CPI {('£%.2f' % cpi) if cpi else '—'}")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
