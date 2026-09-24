#!/usr/bin/env python3
"""
build_detail_api.py — feed the "Within-the-Hour Call Detail" report from the
Connex API instead of a manual CSV import.

Pulls /interaction for the campaign, resolves agent names, and injects the call
rows as the report's own baked snapshot (<script id="im-baked">), so the page
opens with the data already loaded.

Times are shown in NSW (Australia/Sydney) time, matching the dialler's clock and
your existing CSV imports. The 07:30 UK run time is set in Task Scheduler and is
independent of this.

Env vars (same as the other builds): CXM_CLIENT_ID, CXM_SECRET, CXM_TOKEN, CXM_ENDPOINT
Run:  python build_detail_api.py --report "within-hour-call-detail.html"
      python build_detail_api.py --report "..." --days 30
"""
import argparse, json, os, re, sys, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# This report follows the AGENTS, not a campaign: they switch campaigns mid-shift,
# so filtering by campaign would miss half their calls. Partial, case-insensitive
# match on the Connex display name.
AGENTS = ["luke roche", "nasreddine", "nasridine", "naseridine"]   # spelling variants covered
CUTOFF_DATE = ""                # "" = no cutoff; set "YYYY-MM-DD" to ignore earlier calls
NSW = ZoneInfo("Australia/Sydney")   # the report displays NSW clock time
PAGE_SIZE = 100
MAX_PAGES = 800
DEFAULT_DAYS = 45               # how much history to include


def env(n):
    v = os.environ.get(n)
    if not v: sys.exit(f"Missing environment variable {n}.")
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

def parse_nsw(ts):
    if not ts: return None
    try:
        s = ts.replace("Z", "+00:00")
        m = re.search(r"([+-]\d{2})$", s)
        if m: s += ":00"
        return datetime.fromisoformat(s).astimezone(NSW)
    except Exception:
        return None


def stamp_html(built_at):
    """Small 'last updated' badge, bottom-right. Turns amber if the daily 07:30
    build hasn't run in over ~26h, so a missed run is visible rather than silent."""
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
        'var h=Math.floor(m/60);if(h<24)return h+"h "+(m%%60)+"m ago";'
        'return Math.floor(h/24)+"d "+(h%%24)+"h ago";}'
        'function draw(){var el=document.getElementById("__stamp");if(!el){'
        'el=document.createElement("div");el.id="__stamp";document.body.appendChild(el);}'
        'var d=new Date(BUILT),hrs=(Date.now()-d.getTime())/3600000;'
        'el.className=hrs>26?"stale":"";'
        'el.innerHTML="Updated <b>"+ago()+"</b> &middot; "+d.toLocaleString("en-GB",'
        '{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});}'
        'if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",draw);'
        'else draw();setInterval(draw,60000);})();</script>' % built_at
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--out")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = ap.parse_args()

    with open(args.report, encoding="utf-8") as f:
        template = f.read()

    E = env("CXM_ENDPOINT").rstrip("/")
    H = make_headers(get_token(E, env("CXM_CLIENT_ID"), env("CXM_SECRET")), env("CXM_TOKEN"))

    users = {}
    for u in paged(E, H, "/user", {}):
        users[u["id"]] = u.get("display_name") or u.get("username") or u["id"]

    # which user ids are the agents we want?
    wanted = {uid: nm for uid, nm in users.items()
              if any(a in nm.casefold() for a in AGENTS)}
    if not wanted:
        sys.exit("No agents matched " + str(AGENTS) + ".\nNames available: "
                 + ", ".join(sorted(users.values())))
    print("Matched agents: " + ", ".join(sorted(wanted.values())))

    now = datetime.now(NSW)
    start = (now - timedelta(days=args.days)).strftime("%Y-%m-%d")
    if CUTOFF_DATE:
        start = max(start, CUTOFF_DATE)

    rows = []
    for uid, name in wanted.items():
        for it in paged(E, H, "/interaction",
                        {"filter[user_id]": uid, "sort": "-start_time"}):
            dt = parse_nsw(it.get("start_time"))
            if not dt: continue
            day = dt.strftime("%Y-%m-%d")
            if day < start: break
            dur = it.get("duration_in_secs")
            rows.append({"a": name,
                         "d": (it.get("direction") or "").lower(),
                         "day": day, "hour": dt.hour, "min": dt.minute, "sec": dt.second,
                         "len": int(dur) if isinstance(dur, (int, float)) else None})

    rows.sort(key=lambda r: (r["day"], r["hour"], r["min"], r["sec"]))
    payload = {
        "meta": {"fileName": "Connex API (live)",
                 "importedAt": now.replace(microsecond=0).isoformat(),
                 "count": len(rows)},
        "data": rows,
        "sel": None,
    }
    # same escaping the report's own save uses: keeps it valid JSON and unable to
    # break out of the <script> tag
    js = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    tag = '<script id="im-baked" type="application/json">' + js + '</script>'

    html = re.sub(r'<script id="im-baked"[\s\S]*?</script>\s*', "", template, count=1)
    html = re.sub(r'<style id="__stampcss">[\s\S]*?</script>\s*', "", html, count=1)
    badge = stamp_html(now.replace(microsecond=0).isoformat())
    html = re.sub(r"</body>", tag + "\n" + badge + "\n</body>", html, count=1, flags=re.I)

    out = args.out or re.sub(r"\.html?$", "", args.report) + "-live.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    days = sorted({r["day"] for r in rows})
    print(f"Built {out}: {len(rows)} calls, {len(set(r['a'] for r in rows))} agents, "
          f"{len(days)} days ({days[0] if days else '-'} to {days[-1] if days else '-'}, NSW time).")


if __name__ == "__main__":
    main()
