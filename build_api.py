#!/usr/bin/env python3
"""
build_api.py — build the agent leads tracker (data.json) from the Connex API.

IMPORTANT: leads here use the SAME rules as the managerial Call Report, so an
agent's commission matches what the client is actually billed for:
  * Hire & Repair outcomes count as a lead
  * ...EXCEPT calls from SALE_EXCLUDE_NUMBERS
  * ...and same-day repeat H&R to the same number counts ONCE (dedup)
  * Transferred calls to SALE_TRANSFER_NUMBERS count as a lead (earliest only)
  * FORCE_HR numbers are reclassified to Hire & Repair
  * AGENT_REASSIGN credits certain numbers to a named agent
Keep these lists in step with build_report_api.py.

Env: CXM_CLIENT_ID, CXM_SECRET, CXM_TOKEN, CXM_ENDPOINT
Run: python build_api.py            (today)
     python build_api.py --day 2026-07-13
"""
import argparse, json, os, re, sys, threading, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import build   # shared: parse_dt(), UK, slugify(), TITLE

# ============================ CONFIG ============================
CAMPAIGN_ID   = "5bf2bf42-222f-40e5-95d5-82ada3807ffb"   # UK Accident Management
INBOUND_VALUE = "inbound"
EXCLUDE_AGENTS = ["nadir", "unknown"]      # hidden from all tables
PAGE_SIZE, MAX_PAGES = 100, 400
PHONE_WORKERS = 10
CACHE_FILE = "customer_phone_cache.json"   # shared with build_report_api.py

SALE_HIRE     = "Hire & Repair - UK"
SALE_WIND     = "Windscreen - Auto W/Screen - UK"   # windscreen claim (also pays £1)
SALE_PIOPP    = "PI Opp - UK"                        # personal-injury opportunity (pays £2)
SALE_TRANSFER = "Transferred - UK"
# PI opps only count from this date onwards (the scheme started here); they are NOT
# leads and never enter the conversion figures.
PIOPP_FROM    = "2026-10-19"
# ---- these MUST mirror build_report_api.py ----
SALE_TRANSFER_NUMBERS = {"447438475625", "447436471637"}
SALE_EXCLUDE_NUMBERS  = {"447423588163", "447340003433"}
FORCE_HR              = {"447432154233"}
DEDUP_EXEMPT          = {"447508635441"}
AGENT_REASSIGN        = {"447727606730": "James Bridgwood", "447870407370": "Mel Abbott", "447793292655": "Daniel Gee", "447758789895": "James Bridgwood", "447386335582": "Gemma Stone"}
# manually-added H&R claims (mirror build_report_api.py MANUAL_CLAIMS): each injects one H&R lead credited to the named agent
MANUAL_CLAIMS         = [
    {"date": "2026-10-31", "agent": "Mel Abbott"},   # +447391539070, logged as H&R
]
# ===============================================================


def _env(n):
    v = os.environ.get(n)
    if not v: sys.exit(f"Missing environment variable {n}.")
    return v

def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    return ("44" + d[1:]) if d.startswith("0") else d

def get_token(E, cid, sec):
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": cid, "client_secret": sec}).encode()
    req = urllib.request.Request(E + "/oauth2/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Login failed ({e.code}). Check CXM_CLIENT_ID / CXM_SECRET.")

def make_headers(tok, cxm):
    ct = cxm.strip(); xa = ct if ct.lower().startswith("basic ") else "Basic " + ct
    return {"Authorization": "Bearer " + tok, "X-Authorization": xa,
            "Accept": "application/json", "X-Timezone": "Europe/London"}

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
        for rec in data: yield rec
        last = (j.get("meta") or {}).get("page", {}).get("last-page", page)
        if page >= last or not data: break
        page += 1

def outcome_map(E, H):
    j = api_get(E, H, f"/campaign/{CAMPAIGN_ID}/outcome/selectable")
    return {o["id"]: o.get("name", "") for o in (j.get("data") or [])}

def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}

def save_cache(c):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f: json.dump(c, f)
    except Exception:
        pass

def fetch_phones(E, H, ids, cache):
    todo = [c for c in ids if c and c not in cache]
    lock = threading.Lock()
    def one(cid):
        try:
            j = api_get(E, H, f"/customer/{cid}")
            cd = (j.get("data", j) or {}).get("contact_data") or {}
            tels = [norm_phone(cd.get(k)) for k in ("tel1", "tel2", "tel3") if cd.get(k)]
        except Exception:
            tels = []
        with lock:
            cache[cid] = tels
    if todo:
        with ThreadPoolExecutor(max_workers=PHONE_WORKERS) as ex:
            list(ex.map(one, todo))
    return cache


# How far back to pull for the month filter. 9 June is when the campaign's real
# data starts (before that is dialler testing), so June is a partial month.
HISTORY_START = "2026-06-09"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()

    E = _env("CXM_ENDPOINT").rstrip("/")
    H = make_headers(get_token(E, _env("CXM_CLIENT_ID"), _env("CXM_SECRET")), _env("CXM_TOKEN"))

    outcomes = outcome_map(E, H)
    users = {}
    for u in paged(E, H, "/user", {}):
        users[u["id"]] = u.get("display_name") or u.get("username") or u["id"]

    now_uk = datetime.now(build.UK)
    report_day = datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else now_uk.date()
    month_start = report_day.replace(day=1)
    hist_start = datetime.strptime(HISTORY_START, "%Y-%m-%d").date()
    pi_from = datetime.strptime(PIOPP_FROM, "%Y-%m-%d").date()

    # ---- pull interactions back to HISTORY_START (so past months are available) ----
    raw = []
    for it in paged(E, H, "/interaction", {"filter[campaign_id]": CAMPAIGN_ID, "sort": "-start_time"}):
        dt = build.parse_dt(it.get("start_time"))
        if dt is None: continue
        if dt.date() < hist_start: break
        raw.append((it, dt, outcomes.get(it.get("outcome_id"), "")))

    # ---- phone numbers (only for the outcomes the rules touch) ----
    cache = load_cache()
    need = {it.get("customer_id") for (it, dt, o) in raw if o in (SALE_HIRE, SALE_TRANSFER, SALE_WIND)}
    fetch_phones(E, H, need, cache)
    save_cache(cache)

    # ---- shape records, then apply the report's rules ----
    recs = []
    for it, dt, outcome in raw:
        tels = cache.get(it.get("customer_id"), []) if outcome in (SALE_HIRE, SALE_TRANSFER, SALE_WIND) else []
        recs.append({
            "dt": dt, "date": dt.date(), "ms": dt.timestamp(),
            "outcome": outcome,
            "inbound": it.get("direction") == INBOUND_VALUE,
            "ph": tels[0] if tels else "",
            "match": next((t for t in tels if t in SALE_TRANSFER_NUMBERS), None),
            "excl": any(t in SALE_EXCLUDE_NUMBERS for t in tels),
            "agent": users.get(it.get("user_id"), ""),
            "lead": 0, "dup": 0, "wind": 0, "wdup": 0, "pi": 0,
        })

    # force-HR: earliest non-No-Answer call to each FORCE_HR number becomes H&R
    fhr = {}
    for x in recs:
        if x["ph"] in FORCE_HR and x["outcome"] != "No Answer - UK":
            fhr.setdefault(x["ph"], []).append(x)
    for lst in fhr.values():
        lst.sort(key=lambda a: a["ms"]); lst[0]["outcome"] = SALE_HIRE

    # H&R = lead, unless the number is excluded
    for x in recs:
        if x["outcome"] == SALE_HIRE and not x["excl"]:
            x["lead"] = 1
    # allow-listed transfers = lead (earliest per number only)
    bynum = {}
    for x in recs:
        if x["outcome"] == SALE_TRANSFER and x["match"]:
            bynum.setdefault(x["match"], []).append(x)
    for lst in bynum.values():
        lst.sort(key=lambda a: a["ms"]); lst[0]["lead"] = 1
    # same-day dedup of H&R by number (except exempt numbers)
    hk = {}
    for x in recs:
        if x["lead"] and x["outcome"] == SALE_HIRE and x["ph"] and x["ph"] not in DEDUP_EXEMPT:
            hk.setdefault((x["date"], x["ph"]), []).append(x)
    for lst in hk.values():
        lst.sort(key=lambda a: a["ms"])
        for x in lst[1:]:
            x["dup"] = 1

    # windscreen = its own claim (pays £1), same-day dedup by number like H&R
    for x in recs:
        if x["outcome"] == SALE_WIND and not x["excl"]:
            x["wind"] = 1
    wk = {}
    for x in recs:
        if x["wind"] and x["ph"] and x["ph"] not in DEDUP_EXEMPT:
            wk.setdefault((x["date"], x["ph"]), []).append(x)
    for lst in wk.values():
        lst.sort(key=lambda a: a["ms"])
        for x in lst[1:]:
            x["wdup"] = 1

    # PI opps: each "PI Opp - UK" from PIOPP_FROM onwards pays £2. Not a lead, not a
    # windscreen, and never counted in conversion (which only uses leads ÷ inbound calls).
    for x in recs:
        if x["outcome"] == SALE_PIOPP and x["date"] >= pi_from:
            x["pi"] = 1

    # manual claims: inject one H&R lead per entry, credited to the named agent (mirrors the report)
    for mc in MANUAL_CLAIMS:
        y, m, d = map(int, mc["date"].split("-"))
        recs.append({"dt": None, "date": datetime(y, m, d).date(), "ms": 0,
                     "outcome": SALE_HIRE, "inbound": True,
                     "ph": "", "match": None, "excl": False,
                     "agent": mc.get("agent", ""),
                     "lead": 1, "dup": 0, "wind": 0, "wdup": 0, "pi": 0})

    # ---- tally per agent ----
    agents = {}
    def ensure(name):
        aid = build.slugify(name)
        if aid not in agents:
            agents[aid] = {"name": name, "id": aid,
                           "leadsToday": 0, "leadsMonth": 0, "callsToday": 0, "callsMonth": 0,
                           "windToday": 0, "windMonth": 0,
                           "piToday": 0, "piMonth": 0,
                           "byMonth": {}, "byDay": {}}
        return agents[aid]

    for x in recs:
        name = (x["ph"] and AGENT_REASSIGN.get(x["ph"])) or x["agent"] or "Unknown"
        if any(e in name.casefold() for e in EXCLUDE_AGENTS):
            continue
        counts_lead = x["lead"] and not x["dup"]
        counts_wind = x["wind"] and not x["wdup"]
        counts_pi   = x["pi"]
        if not (x["inbound"] or counts_lead or counts_wind or counts_pi):
            continue
        a = ensure(name)
        mk = x["date"].strftime("%Y-%m")
        bm = a["byMonth"].setdefault(mk, {"leads": 0, "calls": 0, "wind": 0, "pi": 0,
                                          "weLeads": 0, "weCalls": 0, "weWind": 0, "wePi": 0})
        cur_month = (mk == report_day.strftime("%Y-%m"))
        is_we = x["date"].weekday() >= 5          # 5 = Saturday, 6 = Sunday
        if x["inbound"]:
            bm["calls"] += 1
            if is_we: bm["weCalls"] += 1
            if cur_month: a["callsMonth"] += 1
            if x["date"] == report_day: a["callsToday"] += 1
        if counts_lead:
            bm["leads"] += 1
            if is_we: bm["weLeads"] += 1
            if cur_month: a["leadsMonth"] += 1
            if x["date"] == report_day: a["leadsToday"] += 1
        if counts_wind:
            bm["wind"] += 1
            if is_we: bm["weWind"] += 1
            if cur_month: a["windMonth"] += 1
            if x["date"] == report_day: a["windToday"] += 1
        if counts_pi:
            bm["pi"] += 1
            if is_we: bm["wePi"] += 1
            if cur_month: a["piMonth"] += 1
            if x["date"] == report_day: a["piToday"] += 1
        if cur_month:
            dk = x["date"].isoformat()
            bd = a["byDay"].setdefault(dk, {"leads": 0, "calls": 0, "wind": 0, "pi": 0})
            if x["inbound"]:  bd["calls"] += 1
            if counts_lead:   bd["leads"] += 1
            if counts_wind:   bd["wind"]  += 1
            if counts_pi:     bd["pi"]    += 1

    data = {
        "title": build.TITLE,
        "updated": now_uk.replace(microsecond=0).isoformat(),
        "period": {"day": report_day.isoformat(), "month": report_day.strftime("%Y-%m")},
        "months": sorted({m for a in agents.values() for m in a["byMonth"]}),
        "agents": sorted(agents.values(), key=lambda a: (-a["leadsMonth"], a["name"])),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    lt = sum(a["leadsToday"] for a in data["agents"]); lm = sum(a["leadsMonth"] for a in data["agents"])
    ct = sum(a["callsToday"] for a in data["agents"]); cm = sum(a["callsMonth"] for a in data["agents"])
    dropped = sum(1 for x in recs if x["dup"]) + sum(1 for x in recs if x["outcome"] == SALE_HIRE and x["excl"])
    print(f"Wrote {args.out}: leads {lt} today / {lm} month | inbound calls {ct} today / {cm} month "
          f"| {len(data['agents'])} agents. Corrections removed {dropped} raw H&R "
          f"(excluded numbers + same-day duplicates).")


if __name__ == "__main__":
    main()
