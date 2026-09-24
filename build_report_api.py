#!/usr/bin/env python3
"""
build_report_api.py — feed the UK Accident Management *Call Report* from the
Connex API instead of a manual CSV paste.

It pulls /interaction for the campaign, resolves outcome + agent names, looks up
the caller's phone (only for Hire & Repair + Transferred calls, cached), then
runs the SAME logic as the report's ingestCSV: 9-June cutoff, test-call skip,
force-HR, H&R/transfer sales, same-day dedup, manual claims. It builds the report's
aggregates and injects them as window.__SNAPSHOT__ into a copy of the report HTML —
your manual spend/overtime/rosters are untouched (they still load from the browser).

Env vars (same as build_api.py): CXM_CLIENT_ID, CXM_SECRET, CXM_TOKEN, CXM_ENDPOINT
Run:  python build_report_api.py --report "UK-Accident-Management-Call-Report.html"
"""
import argparse, hashlib, json, os, re, sys, threading, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

# ===================== CONFIG =====================
CAMPAIGN_ID   = "5bf2bf42-222f-40e5-95d5-82ada3807ffb"     # UK Accident Management
ABANDON_ID    = "100aeb42-7c9a-443c-8aa9-d850f14fd1d1"     # "Inbound Abandon" (not in /selectable)
CUTOFF_DATE   = "2026-06-09"        # ignore dialler-testing before this (UK date)
UK            = ZoneInfo("Europe/London")
PAGE_SIZE     = 100
MAX_PAGES     = 600
PHONE_WORKERS = 10
CACHE_FILE    = "customer_phone_cache.json"

# ---- optional passcode gate on the published report ----
# Set a passcode to require it before the report is shown. Leave "" for no gate.
# Only a one-way hash of this is published — the passcode itself never leaves this file.
REPORT_PASSCODE = ""            # e.g. "gorilla2026"  (change to your chosen code)
_GATE_SALT = "ukam-report::v1"  # do not change once in use (would invalidate saved unlocks)

# Auto-refresh INSIDE the report. Leave at 0: the report is served inside the
# loader page (report_loader.html), and a reload from within the iframe only
# re-renders the same srcdoc HTML — it never re-fetches from GitHub. The LOADER
# does the real refresh (its REFRESH_MIN), so an inner one is useless and fights it.
REPORT_REFRESH_MIN = 0

# Daily Google Ads spend, read from the published CSV of your spend Sheet (the one
# the Google Ads Script writes to). Leave "" to keep spend manual. When set, the
# spend from the sheet feeds the report's cost-per-claim automatically each build.
SPEND_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSEIOq_0n-xt5ownb0G6rkMtcmeprrypsboXZ51HIwLnC8ml1qfkds5AMH6idkojgi1_nTHL-tmIkoD/pub?gid=0&single=true&output=csv"

# Roster patterns — used by the Dropped-call section's "Inc. weekends" toggle to
# decide which days are "non-rostered" (excluded by default). Must match the shift
# pattern in the holiday tracker. hours[0]=Sun .. [6]=Sat; a day is "rostered" if >0.
# OPERATING HOURS — used by the Dropped-call section to count abandons only while
# the line is open, and to grey closed hours in the heatmap.
#
# "hours" is per weekday: index 0=Sun .. 6=Sat. Each is [open, close] (24h, open<=h<close)
# or null for a closed day. Periods apply from their `from` date until the next one.
# OPEN_OVERRIDES pins specific dates (e.g. one-off weekend cover) regardless of the pattern.
#                    Sun          Mon      Tue      Wed      Thu      Fri      Sat
SHIFTS = [
    {"from": "2000-01-01", "hours": [ None,  [8,17],  [8,17],  [8,17],  [8,17],  [8,17],  None ]},   # to 30 Jun: Mon-Fri 8-5
    {"from": "2026-07-01", "hours": [ None,  [8,20],  [8,20],  [8,20],  [8,20],  [8,20],  None ]},   # 1-26 Jul: Mon-Fri 8-8
    {"from": "2026-07-27", "hours": [ [14,20], None,   None,   [14,20], [14,20], [14,20], [14,20] ]}, # from 27 Jul: Wed-Sun 2-8
]

# One-off dates that override the pattern above. "YYYY-MM-DD": [open, close].
# These are the weekends that WERE worked before the 27 Jul change.
OPEN_OVERRIDES = {
    "2026-07-04": [15, 19],   # Sat 3-7pm
    "2026-07-05": [8, 16],    # Sun 8am-4pm
    "2026-07-18": [15, 19],   # Sat 3-7pm
    "2026-07-19": [8, 16],    # Sun 8am-4pm
}

SALE_HIRE     = "Hire & Repair - UK"
SALE_TRANSFER = "Transferred - UK"
# ---- standing corrections (ported verbatim from the report) ----
SALE_TRANSFER_NUMBERS = {"447438475625", "447436471637"}
SALE_EXCLUDE_NUMBERS  = {"447423588163", "447340003433"}
FORCE_HR              = {"447432154233"}
DEDUP_EXEMPT          = {"447508635441"}
AGENT_REASSIGN        = {"447727606730": "James Bridgwood"}
# EXCLUDE_CALLS is keyed by the raw AEST dialler timestamp in the CSV. The API
# gives UTC, so those strings can't be matched here — re-key by interaction id if
# needed (ask Claude to help find the 2 ids). Left empty so nothing is dropped wrongly.
EXCLUDE_CALL_IDS      = set()
MANUAL_CLAIMS = [
    {"date": "2026-07-06", "hour": 15, "minute": 52, "agent": "James Bridgwood"},
]
# ==================================================


def env(n):
    v = os.environ.get(n)
    if not v: sys.exit(f"Missing environment variable {n}.")
    return v

def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    return ("44" + d[1:]) if d.startswith("0") else d

def is_test(o):
    return re.sub(r"[^a-z0-9]", "", (o or "").lower()) == "test"

def parse_uk(ts):
    """UTC ISO ('...Z') -> dict(date,hour,minute,ms). Handles bare +hh offsets too."""
    if not ts: return None
    try:
        s = ts.replace("Z", "+00:00")
        # bare offset like +10 -> +10:00
        m = re.search(r"([+-]\d{2})$", s)
        if m: s = s + ":00"
        dt = datetime.fromisoformat(s).astimezone(UK)
        return {"date": dt.strftime("%Y-%m-%d"), "hour": dt.hour, "minute": dt.minute,
                "ms": dt.timestamp()}
    except Exception:
        return None

# ---------------------- API ----------------------
def get_token(E, cid, sec):
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": cid, "client_secret": sec}).encode()
    req = urllib.request.Request(E + "/oauth2/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]

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
    m = {o["id"]: o.get("name", "") for o in (j.get("data") or [])}
    m[ABANDON_ID] = "Inbound Abandon"
    return m

def user_map(E, H):
    m = {}
    for u in paged(E, H, "/user", {}):
        m[u["id"]] = u.get("display_name") or u.get("username") or u["id"]
    return m

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

def fetch_phones(E, H, customer_ids, cache):
    """Look up contact_data.tel* for the given customer ids (cached, threaded)."""
    todo = [cid for cid in customer_ids if cid and cid not in cache]
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

# ------------------ report builders (ported) ------------------
def dow_of(ds):
    y, m, d = map(int, ds.split("-"))
    return (datetime(y, m, d).weekday() + 1) % 7   # Sun=0 .. Sat=6, matching the report

def build_shift_agg(ev):
    if not ev: return {"occ": {}, "hrs": {}, "range": []}
    dates = sorted({e[0] for e in ev}); minD, maxD = dates[0], dates[-1]
    occ = {i: 0 for i in range(7)}
    # count each weekday occurrence across the whole span
    from datetime import date as _d, timedelta
    y, m, d = map(int, minD.split("-")); cur = _d(y, m, d)
    y2, m2, d2 = map(int, maxD.split("-")); end = _d(y2, m2, d2)
    while cur <= end:
        occ[(cur.weekday() + 1) % 7] += 1; cur += timedelta(days=1)
    hrs = {}
    for ds, h in ev:
        wd = dow_of(ds); hrs.setdefault(wd, {}); hrs[wd][h] = hrs[wd].get(h, 0) + 1
    return {"occ": occ, "hrs": hrs, "range": [minD, maxD]}

def build_dispo(recs):
    m = {}
    for x in recs:
        o = (x["outcome"] or "").strip() or "(blank)"
        m.setdefault(x["date"], {}); m[x["date"]][o] = m[x["date"]].get(o, 0) + 1
    return m

def build_agent_instr(recs, dedup):
    m = {}
    for x in recs:
        if x["sale"] == 1 and (not dedup or not x["hrdup"]):
            ag = (x["ph"] and AGENT_REASSIGN.get(x["ph"])) or x["agent"]
            if ag:
                m.setdefault(x["date"], {}); m[x["date"]][ag] = m[x["date"]].get(ag, 0) + 1
    return m

def build_agent_calls(recs):
    m = {}
    for x in recs:
        if x["agent"]:
            m.setdefault(x["date"], {}); m[x["date"]][x["agent"]] = m[x["date"]].get(x["agent"], 0) + 1
    return m

def build_snapshot(recs, shift_ev):
    shift = build_shift_agg(shift_ev)
    dispo = build_dispo(recs)                       # raw, before force-HR
    # force-HR: earliest non-No-Answer call to each FORCE_HR number -> Hire & Repair
    fhr = {}
    for x in recs:
        if x["ph"] in FORCE_HR and x["outcome"] != "No Answer - UK":
            fhr.setdefault(x["ph"], []).append(x)
    for lst in fhr.values():
        lst.sort(key=lambda a: a["ms"]); lst[0]["outcome"] = SALE_HIRE
    # H&R sales
    for x in recs:
        if x["outcome"] == SALE_HIRE and not x["excl"]: x["sale"] = 1
    # transfer sales: earliest call per allow-listed number
    bynum = {}
    for x in recs:
        if x["outcome"] == SALE_TRANSFER and x["match"]:
            bynum.setdefault(x["match"], []).append(x)
    for lst in bynum.values():
        lst.sort(key=lambda a: a["ms"]); lst[0]["sale"] = 1
    # same-day dedup of H&R by number (except exempt)
    for x in recs: x["hrdup"] = 0
    hk = {}
    for x in recs:
        if x["sale"] == 1 and x["outcome"] == SALE_HIRE and x["ph"] and x["ph"] not in DEDUP_EXEMPT:
            hk.setdefault(x["date"] + "|" + x["ph"], []).append(x)
    for lst in hk.values():
        lst.sort(key=lambda a: a["ms"])
        for x in lst[1:]: x["hrdup"] = 1
    # manual claims
    for mc in MANUAL_CLAIMS:
        recs.append({"date": mc["date"], "hour": mc.get("hour", 12), "minute": mc.get("minute", 0),
                     "dir": "in", "outcome": SALE_HIRE, "sale": 1, "aband": 0, "hrdup": 0,
                     "ph": "", "agent": mc.get("agent", ""), "ms": 0, "match": None, "excl": False})
    agents    = build_agent_instr(recs, False)
    agents_dd = build_agent_instr(recs, True)
    calls     = build_agent_calls(recs)
    data = sorted([[x["date"], x["hour"], x["minute"], x["dir"], x["sale"], x["aband"], x["hrdup"]]
                   for x in recs], key=lambda r: (r[0], r[1], r[2]))
    return {"data": data, "shiftAgg": shift, "agents": agents, "agentsDD": agents_dd,
            "agentCalls": calls, "dispo": dispo, "dedup": True, "roster": SHIFTS, "overrides_hours": OPEN_OVERRIDES}


GATE_TEMPLATE = """<style id="__gatecss">html.__gated body{display:none!important}
#__gate{position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;background:#0d1017;color:#e8ecf2;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
#__gate .box{background:#161b24;border:1px solid #242c3a;border-radius:14px;padding:26px;width:min(90vw,340px);text-align:center}
#__gate .t{font-size:14px;color:#8a94a6;margin-bottom:16px}
#__gate input{width:100%;box-sizing:border-box;padding:11px;border-radius:9px;border:1px solid #242c3a;background:#0d1017;color:#e8ecf2;font-size:16px;text-align:center;letter-spacing:2px}
#__gate button{margin-top:12px;width:100%;padding:11px;border-radius:9px;border:0;background:#f5b544;color:#1a1205;font-weight:700;font-size:15px;cursor:pointer}
#__gate .err{color:#e5484d;font-size:13px;min-height:18px;margin-top:8px}</style>
<script>(function(){var KEY='__uk_am_report_auth_v1',HASH='__HASH__',SALT='__SALT__';
try{if(localStorage.getItem(KEY)===HASH)return;}catch(e){}
document.documentElement.classList.add('__gated');
function build(){var g=document.createElement('div');g.id='__gate';
g.innerHTML='<div class="box"><div class="t">UK Accident Management &mdash; Call Report</div><input id="__gp" type="password" placeholder="Passcode" autocomplete="off" autofocus><button id="__gb">Enter</button><div class="err" id="__ge"></div></div>';
document.documentElement.appendChild(g);
async function sha(s){var b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(SALT+s));return Array.from(new Uint8Array(b)).map(function(x){return x.toString(16).padStart(2,'0')}).join('');}
async function go(){var v=document.getElementById('__gp').value;var h=await sha(v);
if(h===HASH){try{localStorage.setItem(KEY,HASH);}catch(e){}document.documentElement.classList.remove('__gated');g.remove();}
else{document.getElementById('__ge').textContent='Incorrect passcode';document.getElementById('__gp').value='';document.getElementById('__gp').focus();}}
document.getElementById('__gb').onclick=go;document.getElementById('__gp').addEventListener('keydown',function(e){if(e.key==='Enter')go();});}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',build);else build();})();</script>"""


def gate_head(passcode):
    if not passcode:
        return ""
    h = hashlib.sha256((_GATE_SALT + passcode).encode("utf-8")).hexdigest()
    return GATE_TEMPLATE.replace("__HASH__", h).replace("__SALT__", _GATE_SALT)


def refresh_head(minutes):
    if not minutes or minutes <= 0:
        return ""
    ms = int(minutes * 60 * 1000)
    return ('<script id="__autorefresh">(function(){var MS=%d,la=Date.now();'
            "['mousemove','keydown','click','scroll','touchstart'].forEach(function(e){"
            "window.addEventListener(e,function(){la=Date.now();},{passive:true});});"
            "setInterval(function(){if(document.hidden||(Date.now()-la)>45000){location.reload();}},MS);"
            "})();</script>") % ms


def stamp_head(built_at):
    """A small 'last updated' badge, fixed to the bottom-right of the report.
    Shows the build time and counts up ('7 min ago') so a stale page is obvious."""
    return (
        '<style id="__stampcss">#__stamp{position:fixed;right:12px;bottom:12px;z-index:9999;'
        'background:#161b24;border:1px solid #242c3a;color:#8a94a6;border-radius:10px;'
        'padding:7px 11px;font:12px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;'
        'box-shadow:0 4px 14px rgba(0,0,0,.35);pointer-events:none}'
        '#__stamp b{color:#e8ecf2;font-weight:600}'
        '#__stamp.stale b{color:#f5b544}</style>'
        '<script id="__stamp_js">(function(){var BUILT="%s";'
        'function ago(){var s=(Date.now()-new Date(BUILT).getTime())/1000;'
        'if(s<60)return"just now";var m=Math.round(s/60);if(m<60)return m+" min ago";'
        'var h=Math.floor(m/60);return h+"h "+(m%%60)+"m ago";}'
        'function draw(){var el=document.getElementById("__stamp");if(!el){'
        'el=document.createElement("div");el.id="__stamp";document.body.appendChild(el);}'
        'var d=new Date(BUILT),mins=(Date.now()-d.getTime())/60000;'
        'el.className=mins>90?"stale":"";'
        'el.innerHTML="Updated <b>"+ago()+"</b> &middot; "+d.toLocaleString("en-GB",'
        '{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});}'
        'if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",draw);'
        'else draw();setInterval(draw,60000);})();</script>' % built_at
    )


ABANDON_BLOCK = r'''<style id="__abhm">
#__abandonHeat{margin-top:22px}
#__abandonHeat .hm-title{font-size:14px;font-weight:650;margin:0 0 4px}
#__abandonHeat .hm-sub{font-size:11.5px;color:var(--faint);margin:0 0 12px}
#__abandonHeat table{width:100%;table-layout:fixed;border-collapse:collapse;min-width:0}
#__abandonHeat col.c-date{width:108px}
#__abandonHeat col.c-tot{width:150px}
#__abandonHeat th{font-size:10px;color:var(--faint);font-weight:600;padding:0 0 6px;text-align:center}
#__abandonHeat th.row-h{text-align:left}
#__abandonHeat td{padding:1px;text-align:center}
#__abandonHeat td.row-h{text-align:left;font-size:12.5px;white-space:nowrap;padding-right:8px}
#__abandonHeat td.row-h .dow{color:var(--faint);margin-left:6px;font-size:11px}
#__abandonHeat .cell{height:40px;border-radius:5px;display:flex;align-items:center;justify-content:center;
  font-size:15px;font-weight:700;color:#fff}
#__abandonHeat .cell.zero{color:var(--faint);font-weight:500}
#__abandonHeat .cell.closed{background:repeating-linear-gradient(45deg,#0c1016,#0c1016 4px,#0e131b 4px,#0e131b 8px)}
#__abandonHeat td.tot{white-space:nowrap;font-size:12.5px;text-align:right;padding-right:6px;font-variant-numeric:tabular-nums}
#__abandonHeat td.tot .d{color:#ff9ea1;font-weight:700}
#__abandonHeat td.tot .p{color:var(--faint);margin-left:6px}
#__abandonHeat tr.col-tot td{font-size:12px;color:var(--muted);padding-top:8px;font-variant-numeric:tabular-nums}
#__abandonHeat tr.col-tot td.row-h{color:var(--faint);text-transform:uppercase;letter-spacing:.05em;font-size:10px}
#__abandonHeat tr.col-tot td{text-align:center}
#__abandonHeat tr.col-tot td.d-c{color:#ff9ea1;font-weight:700}
#__abandonHeat tr.col-sep td{padding-top:10px;border-top:1px solid var(--line)}
#__abandonHeat tr.col-pct td{color:var(--faint);font-size:11px;padding-bottom:2px}
#__abandonHeat .grand{text-align:right;font-size:12.5px;color:var(--muted);margin-top:10px}
#__abandonHeat .grand b{color:#ff9ea1}
#__abandonHeat .ab-months{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
#__abandonHeat .ab-months button{background:var(--card2);border:1px solid var(--line);color:var(--muted);
  font:inherit;font-size:12px;padding:5px 11px;border-radius:8px;cursor:pointer}
#__abandonHeat .ab-months button.on{background:var(--accent);color:#1a1205;border-color:var(--accent);font-weight:700}
#__abandonHeat .ab-bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 12px}
#__abandonHeat .ab-we{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);cursor:pointer;user-select:none}
#__abandonHeat .ab-we input{appearance:none;width:34px;height:19px;border-radius:19px;background:var(--card2);
  border:1px solid var(--line);position:relative;cursor:pointer;transition:background .15s}
#__abandonHeat .ab-we input:checked{background:var(--accent);border-color:var(--accent)}
#__abandonHeat .ab-we input::after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;
  border-radius:50%;background:#e8ecf2;transition:left .15s}
#__abandonHeat .ab-we input:checked::after{left:16px;background:#1a1205}
#__abandonHeat .ab-tot{margin-top:22px}
#__abandonHeat table.mt{width:100%;border-collapse:collapse}
#__abandonHeat table.mt th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--faint);
  text-align:right;padding:6px 10px;font-weight:600}
#__abandonHeat table.mt th:first-child{text-align:left}
#__abandonHeat table.mt td{padding:9px 10px;border-top:1px solid var(--line);font-size:13.5px;
  text-align:right;font-variant-numeric:tabular-nums}
#__abandonHeat table.mt td:first-child{text-align:left}
#__abandonHeat table.mt tr.mrow{cursor:pointer}
#__abandonHeat table.mt tr.mrow:hover td{background:var(--card2)}
#__abandonHeat table.mt tr.mrow td:first-child::before{content:"\25B8 ";color:var(--faint)}
#__abandonHeat table.mt tr.mrow.open td:first-child::before{content:"\25BE ";color:var(--accent)}
#__abandonHeat table.mt tr.wrow td{background:var(--bg2);font-size:12.5px;color:var(--muted);border-top:1px solid var(--bg)}
#__abandonHeat table.mt tr.wrow td:first-child{padding-left:26px}
#__abandonHeat table.mt td .d{color:#ff9ea1;font-weight:700}
#__abandonHeat table.mt tr.grandrow td{border-top:2px solid var(--line);font-weight:700;color:var(--text)}
</style>
<script id="__abandon_js">
(function(){
  var AB_MONTH = 'all';           // this section's own month filter (independent of the report)
  var AB_OPEN = {};               // which month rows are expanded in the totals table
  var AB_WE = false;              // include non-rostered days? default off (Mon-Fri-style)
  function shiftFor(ymd){
    var roster = (typeof DATA_ROSTER!=='undefined' && DATA_ROSTER) ? DATA_ROSTER : null;
    if(!roster) return null;
    var pat = roster[0];
    for(var i=0;i<roster.length;i++){ if(ymd >= roster[i].from) pat = roster[i]; }
    return pat;
  }
  function windowFor(ymd){
    // a specific-date override wins over the weekly pattern
    if(DATA_HRS_OVR && DATA_HRS_OVR[ymd]) return DATA_HRS_OVR[ymd];
    var p = shiftFor(ymd);
    if(!p || !p.hours) return null;                        // no roster -> treat as open
    var d = new Date(ymd + 'T12:00:00'); if(isNaN(d)) return null;
    return p.hours[d.getDay()] || null;                    // [open,close] or null (closed)
  }
  function openHour(ymd, hr){
    var w = windowFor(ymd);
    if(w === null) return (DATA_ROSTER ? false : true);    // closed day (or open if no roster at all)
    return hr >= w[0] && hr < w[1];
  }
  function rostered(ymd){
    if(!DATA_ROSTER){ var d=new Date(ymd+'T12:00:00'); var wd=isNaN(d)?1:d.getDay(); return wd!==0&&wd!==6; }
    return windowFor(ymd) !== null;      // a working day = has an open window (pattern or override)
  }
  function keepDay(ymd){
    if(AB_WE) return true;                 // toggle on = include all days
    var d=new Date(ymd+'T12:00:00'); if(isNaN(d)) return true;
    var wd=d.getDay();
    return wd!==0 && wd!==6;               // off = Monday-Friday only
  }
  function abHeat(v,max){
    if(!v) return '#0e151d';
    var t=Math.pow(v/max,0.7);
    // dark -> amber -> red
    var stops=[[24,28,36],[168,121,31],[229,72,77]];
    var seg=t<.5?0:1, lt=t<.5?t*2:(t-.5)*2, a=stops[seg], b=stops[seg+1];
    return 'rgb('+Math.round(a[0]+(b[0]-a[0])*lt)+','+Math.round(a[1]+(b[1]-a[1])*lt)+','+Math.round(a[2]+(b[2]-a[2])*lt)+')';
  }
  var DATA_ROSTER = (window.__SNAPSHOT__ && window.__SNAPSHOT__.roster) || null;
  var DATA_HRS_OVR = (window.__SNAPSHOT__ && window.__SNAPSHOT__.overrides_hours) || {};
  function abData(){                       // inbound calls, filtered by month + rostered-day rule
    var base = (typeof DATA!=='undefined') ? DATA : [];
    return base.filter(function(r){
      return r[3]==='in' && keepDay(r[0]) && (AB_WE || openHour(r[0], r[1]))
             && (AB_MONTH==='all' || r[0].slice(0,7)===AB_MONTH);
    });
  }
  function abHourRange(dates){
    // when weekends/closed hours are OFF, span only the open window; else fall back
    // to the report's core range so the grid still looks sensible.
    if(AB_WE || typeof DATA_ROSTER==='undefined' || !DATA_ROSTER){
      var r=coreHourRange(); return {mn:r.mn, mx:r.mx};
    }
    var lo=99, hi=-1;
    dates.forEach(function(d){ var w=windowFor(d); if(w){ if(w[0]<lo)lo=w[0]; if(w[1]-1>hi)hi=w[1]-1; } });
    if(hi<0){ var r2=coreHourRange(); return {mn:r2.mn, mx:r2.mx}; }
    return {mn:lo, mx:hi};
  }
  function render(){
    var host=document.getElementById('__abandonHeat');
    if(!host || typeof coreHourRange!=='function') return;
    renderMonths();
    var rows=abData();
    var _dates=[].concat(rows.map(function(r){return r[0];})).filter(function(v,i,a){return a.indexOf(v)===i;}).sort();
    var rng=abHourRange(_dates), mn=rng.mn, mx=rng.mx;
    var dates=[].concat(rows.map(function(r){return r[0];}))
                .filter(function(v,i,a){return a.indexOf(v)===i;}).sort();
    var callG={}, abG={}, colCall={}, colAb={}, rowCall={}, rowAb={}, maxAb=0, tCall=0, tAb=0;
    dates.forEach(function(d){callG[d]={};abG[d]={};});
    rows.forEach(function(r){
      if(r[1]<mn||r[1]>mx) return;
      callG[r[0]][r[1]]=(callG[r[0]][r[1]]||0)+1;
      colCall[r[1]]=(colCall[r[1]]||0)+1; rowCall[r[0]]=(rowCall[r[0]]||0)+1; tCall++;
      if(r[5]===1){ abG[r[0]][r[1]]=(abG[r[0]][r[1]]||0)+1;
        colAb[r[1]]=(colAb[r[1]]||0)+1; rowAb[r[0]]=(rowAb[r[0]]||0)+1; tAb++; }
    });
    dates.forEach(function(d){for(var h=mn;h<=mx;h++){var v=abG[d][h]||0; if(v>maxAb)maxAb=v;}});
    maxAb=maxAb||1;
    var pct=function(a,c){return c>0?(Math.round(a/c*1000)/10)+'%':'–';};

    var h='<div class="hm-title">Inbound Abandoned — by hour</div>'
      +'<div class="hm-sub">Cell shows calls dropped that hour (redder = more). Daily total is calls / dropped / abandon rate.</div>'
      +'<table><colgroup><col class="c-date">';
    for(var hr=mn;hr<=mx;hr++) h+='<col>';
    h+='<col class="c-tot"></colgroup><thead><tr><th class="row-h">Date</th>';
    for(var hr=mn;hr<=mx;hr++) h+='<th>'+pad(hr)+'</th>';
    h+='<th style="text-align:right">Calls / Dropped / %</th></tr></thead><tbody>';
    dates.forEach(function(d){
      h+='<tr><td class="row-h"><b>'+fmtD(d)+'</b><span class="dow">'+DOW[dowOf(d)]+'</span></td>';
      for(var hr=mn;hr<=mx;hr++){
        if(!AB_WE && !openHour(d,hr)){ h+='<td><div class="cell closed" title="'+fmtD(d)+' '+pad(hr)+':00 — line closed"></div></td>'; continue; }
        var a=abG[d][hr]||0, c=callG[d][hr]||0;
        var tip=fmtD(d)+' '+pad(hr)+':00 — '+a+' dropped of '+c+' call'+(c!==1?'s':'');
        h+='<td><div class="cell'+(a?'':' zero')+'" style="background:'+abHeat(a,maxAb)+'" title="'+tip+'">'+(a||'·')+'</div></td>';
      }
      h+='<td class="tot">'+(rowCall[d]||0)+' / <span class="d">'+(rowAb[d]||0)+'</span> <span class="p">&middot; '+pct(rowAb[d]||0,rowCall[d]||0)+'</span></td></tr>';
    });
    h+='<tr class="col-tot col-sep"><td class="row-h">Calls / hour</td>';
    for(var hr=mn;hr<=mx;hr++) h+='<td>'+(colCall[hr]||0)+'</td>';
    h+='<td style="text-align:right">'+tCall+'</td></tr>';
    h+='<tr class="col-tot"><td class="row-h">Dropped / hour</td>';
    for(var hr=mn;hr<=mx;hr++) h+='<td class="d-c">'+(colAb[hr]||0)+'</td>';
    h+='<td style="text-align:right"><b style="color:#ff9ea1">'+tAb+'</b></td></tr>';
    h+='<tr class="col-tot col-pct"><td class="row-h">Abandon %</td>';
    for(var hr=mn;hr<=mx;hr++) h+='<td>'+pct(colAb[hr]||0, colCall[hr]||0)+'</td>';
    h+='<td style="text-align:right">'+pct(tAb,tCall)+'</td></tr>';
    h+='</tbody></table>';
    var grid=document.getElementById('__abGrid');
    if(grid) grid.innerHTML=h;
    renderTotals();
  }

  function renderMonths(){
    var bar=document.getElementById('__abMonths'); if(!bar) return;
    var months = (typeof monthsPresent==='function') ? monthsPresent() : [];
    if(AB_MONTH!=='all' && months.indexOf(AB_MONTH)<0) AB_MONTH='all';
    var html='<button data-m="all" class="'+(AB_MONTH==='all'?'on':'')+'">All months</button>';
    months.forEach(function(m){
      var lab=MONTHS[+m.slice(5,7)-1]+" '"+m.slice(2,4);
      html+='<button data-m="'+m+'" class="'+(AB_MONTH===m?'on':'')+'">'+lab+'</button>';
    });
    bar.innerHTML=html;
    bar.querySelectorAll('button').forEach(function(b){
      b.onclick=function(){ AB_MONTH=b.dataset.m; render(); };
    });
    var we=document.getElementById('__abWe');
    if(we){
      we.checked=AB_WE;
      we.onchange=function(){ AB_WE=we.checked; render(); };
    }
  }

  // Monthly totals, each expandable into its weeks (Mon-Sun).
  function renderTotals(){
    var host=document.getElementById('__abTotals'); if(!host) return;
    var base=(typeof DATA!=='undefined'?DATA:[]).filter(function(r){
      return r[3]==='in' && keepDay(r[0]) && (AB_WE || openHour(r[0], r[1]));
    });
    var pct=function(a,c){return c>0?(Math.round(a/c*1000)/10)+'%':'\u2013';};
    var months=(typeof monthsPresent==='function')?monthsPresent():[];

    function tally(filter){var c=0,a=0;base.forEach(function(r){if(filter(r)){c++;if(r[5]===1)a++;}});return {c:c,a:a};}

    var h='<div class="hm-title">Monthly totals</div>'
      +'<div class="hm-sub">Click a month for its weekly breakdown. Calls / dropped / abandon rate.</div>'
      +'<table class="mt"><thead><tr><th>Period</th><th>Calls</th><th>Dropped</th><th>Rate</th></tr></thead><tbody>';
    var gC=0,gA=0;
    months.forEach(function(m){
      var t=tally(function(r){return r[0].slice(0,7)===m;});
      gC+=t.c; gA+=t.a;
      var open=!!AB_OPEN[m];
      h+='<tr class="mrow'+(open?' open':'')+'" data-m="'+m+'"><td>'+MONTHS[+m.slice(5,7)-1]+" '"+m.slice(2,4)
        +'</td><td>'+t.c+'</td><td class="d">'+t.a+'</td><td>'+pct(t.a,t.c)+'</td></tr>';
      if(open){
        // weeks (Mondays) that fall in this month's data
        var mons=[].concat(base.filter(function(r){return r[0].slice(0,7)===m;})
                    .map(function(r){return mondayOf(r[0]);}))
                  .filter(function(v,i,a){return a.indexOf(v)===i;}).sort();
        mons.forEach(function(mon){
          var end=addDays(mon,6);
          var w=tally(function(r){return r[0]>=mon && r[0]<=end;});
          h+='<tr class="wrow"><td>'+fmtD(mon)+' \u2013 '+fmtD(end)+'</td><td>'+w.c
            +'</td><td class="d">'+w.a+'</td><td>'+pct(w.a,w.c)+'</td></tr>';
        });
      }
    });
    h+='<tr class="grandrow"><td>All months</td><td>'+gC+'</td><td class="d">'+gA+'</td><td>'+pct(gA,gC)+'</td></tr>';
    h+='</tbody></table>';
    host.innerHTML=h;
    host.querySelectorAll('tr.mrow').forEach(function(row){
      row.onclick=function(){var m=row.dataset.m; AB_OPEN[m]=!AB_OPEN[m]; renderTotals();};
    });
  }
  function ensure(){
    var ov=document.getElementById('tab-overview');
    if(!ov) return false;
    if(!document.getElementById('__abandonHeat')){
      var sec=document.createElement('section');
      sec.id='__abandonHeat'; sec.className='panel';
      sec.innerHTML='<div class="ab-bar">'
                  +   '<div class="ab-months" id="__abMonths" style="margin:0"></div>'
                  +   '<label class="ab-we"><input type="checkbox" id="__abWe">Inc. weekends '
                  +     '<span style="color:var(--faint)">(non-rostered days)</span></label>'
                  +   '</div>'
                  + '<div id="__abGrid"></div>'
                  + '<div class="ab-tot" id="__abTotals"></div>';
      ov.appendChild(sec);
    }
    try{ render(); }catch(e){
      document.getElementById('__abandonHeat').innerHTML=
        '<div class="hm-title">Inbound Abandoned — by hour</div>'+
        '<div class="hm-sub" style="color:#ff9ea1">Couldn\'t render: '+e+'</div>';
    }
    return true;
  }
  // Wrap renderAll IF it exists yet; otherwise keep polling until the report is ready.
  function hook(){
    if(typeof window.renderAll==='function' && !window.__abHooked){
      window.__abHooked=true;
      var orig=window.renderAll;
      window.renderAll=function(){ var r=orig.apply(this,arguments); try{ensure();}catch(e){} return r; };
      return true;
    }
    return false;
  }
  function boot(){
    hook();            // attach for future re-renders (tab/month/dir changes)
    ensure();          // and render right now
  }
  // The report defines renderAll during its own load; poll briefly until it's there.
  var tries=0;
  (function waitReady(){
    var haveFns = (typeof viewData==='function' && typeof coreHourRange==='function'
                   && document.getElementById('tab-overview'));
    if(haveFns){ boot(); }
    else if(tries++ < 60){ setTimeout(waitReady,100); }   // up to ~6s
  })();
})();
</script>'''


def abandon_block():
    return ABANDON_BLOCK


def inject(report_html, snapshot, gate_html="", refresh_html="", stamp_html=""):
    tag = "<script>window.__SNAPSHOT__=" + json.dumps(snapshot, separators=(",", ":")) + ";</script>"
    html = re.sub(r"<script>window\.__SNAPSHOT__=[\s\S]*?</script>\s*", "", report_html, flags=re.I)
    # strip any previously-injected gate / refresh / stamp so re-builds don't stack them
    html = re.sub(r'<style id="__gatecss">[\s\S]*?</script>\s*', "", html, count=1)
    html = re.sub(r'<script id="__autorefresh">[\s\S]*?</script>\s*', "", html, count=1)
    html = re.sub(r'<style id="__stampcss">[\s\S]*?</script>\s*', "", html, count=1)
    html = re.sub(r'<style id="__abhm">[\s\S]*?<script id="__abandon_js">[\s\S]*?</script>\s*', "", html, count=1)
    head_add = tag
    if gate_html:    head_add += "\n" + gate_html
    if refresh_html: head_add += "\n" + refresh_html
    if stamp_html:   head_add += "\n" + stamp_html
    head_add += "\n" + abandon_block()
    html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + "\n" + head_add, html, count=1)
    return html


def fetch_spend(url):
    """Read a published date,spend CSV -> {date: float}. Returns None on failure."""
    import csv
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            text = r.read().decode("utf-8-sig", "replace")
    except Exception as e:
        print(f"  (spend fetch failed: {e} — keeping manual spend)")
        return None
    out = {}
    for parts in csv.reader(text.splitlines()):
        if len(parts) < 2:
            continue
        d, raw = parts[0].strip(), parts[1].strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):   # skips the header row
            continue
        try:
            out[d] = round(float(re.sub(r"[£,\s]", "", raw)), 2)
        except ValueError:
            continue
    return out or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="path to the Call Report HTML (template)")
    ap.add_argument("--out", help="output HTML (default: <report>-live.html)")
    args = ap.parse_args()

    with open(args.report, encoding="utf-8") as f:
        template = f.read()

    E = env("CXM_ENDPOINT").rstrip("/")
    H = make_headers(get_token(E, env("CXM_CLIENT_ID"), env("CXM_SECRET")), env("CXM_TOKEN"))
    outcomes = outcome_map(E, H)
    users = user_map(E, H)

    # pull interactions newest-first, stop at the cutoff date
    raw = []
    for it in paged(E, H, "/interaction", {"filter[campaign_id]": CAMPAIGN_ID, "sort": "-start_time"}):
        uk = parse_uk(it.get("start_time"))
        if not uk: continue
        if uk["date"] < CUTOFF_DATE: break
        outcome = outcomes.get(it.get("outcome_id"), "")
        if is_test(outcome): continue
        raw.append((it, uk, outcome))

    # phone lookups only where corrections need them (H&R + Transferred)
    cache = load_cache()
    need = {it.get("customer_id") for (it, uk, o) in raw if o in (SALE_HIRE, SALE_TRANSFER)}
    fetch_phones(E, H, need, cache); save_cache(cache)

    recs, shift_ev = [], []
    for it, uk, outcome in raw:
        shift_ev.append([uk["date"], uk["hour"]])
        tels = cache.get(it.get("customer_id"), []) if outcome in (SALE_HIRE, SALE_TRANSFER) else []
        match = next((t for t in tels if t in SALE_TRANSFER_NUMBERS), None)
        excl = any(t in SALE_EXCLUDE_NUMBERS for t in tels) or (it.get("id") in EXCLUDE_CALL_IDS)
        recs.append({"date": uk["date"], "hour": uk["hour"], "minute": uk["minute"],
                     "dir": "in" if it.get("direction") == "inbound" else "out",
                     "outcome": outcome, "match": match, "excl": excl, "ms": uk["ms"],
                     "sale": 0, "aband": 1 if outcome.lower().replace(" ", "") == "inboundabandon" else 0,
                     "ph": (tels[0] if tels else ""), "agent": users.get(it.get("user_id"), "")})

    snap = build_snapshot(recs, shift_ev)

    if SPEND_CSV_URL:
        spend = fetch_spend(SPEND_CSV_URL)
        if spend:
            snap["overrides"] = spend
            print(f"  spend: {len(spend)} days from sheet "
                  f"(latest {max(spend)} = £{spend[max(spend)]:,.2f}).")

    out = args.out or re.sub(r"\.html?$", "", args.report) + "-live.html"
    built_at = datetime.now(UK).replace(microsecond=0).isoformat()
    with open(out, "w", encoding="utf-8") as f:
        f.write(inject(template, snap, gate_head(REPORT_PASSCODE),
                       refresh_head(REPORT_REFRESH_MIN), stamp_head(built_at)))

    tc = sum(sum(v.values()) for v in snap["agentCalls"].values())
    claims = sum(sum(v.values()) for v in snap["agentsDD"].values())
    print(f"Built {out}: {len(snap['data'])} call rows, {claims} claims (deduped), "
          f"{tc} agent-calls, phones cached: {len(cache)}.")


if __name__ == "__main__":
    main()
