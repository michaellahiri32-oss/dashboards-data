#!/usr/bin/env python3
"""
build_contact.py — Outbound contact-rate tracker across selected campaigns.

For each campaign in CAMPAIGNS, over a rolling window (default 14 days), it pulls
outbound calls from the Connex API and classifies each by its disposition:

  - No Answer*          -> non-contact
  - Answer(ing) Machine* -> non-contact
  - any other real outcome -> contacted
  - blank / no outcome, or a "-Test-" outcome -> excluded (not a real attempt)

It aggregates per campaign per day and writes contact.json:
  contact rate = contacted / dispositioned, where dispositioned = total minus
  the excluded (blank/test) calls.

Read-only against Connex. Writes contact.json only. Publishing is a separate step.

  python build_contact.py                 # last 14 days
  python build_contact.py --days 30       # longer window
  python build_contact.py --days 3        # quick test run
"""
import argparse, json, os, re, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Python 3.9+ needed (zoneinfo).")

UK = ZoneInfo("Europe/London")

# ---- campaigns to track (id : [group, name]) ------------------------------------
# Trim/extend freely; the dashboard has a picker so extra ones here are fine.
CAMPAIGNS = {
    # UK
    "5bf2bf42-222f-40e5-95d5-82ada3807ffb": ["UK", "UK_Accident Management"],
    "a454f03d-12a5-4f66-9aaf-a62b6f5fa20f": ["UK", "UK_Accident Management Validation"],
    # WA
    "81f8ff21-d4af-4d80-9920-626b2c3b79f2": ["WA", "Marketing WA Opps"],
    "f9125632-a9c5-42cc-b624-e1b19dda3e76": ["WA", "Marketing WA Specialized"],
    "7916b5a1-d9e0-485c-a55c-85937da4c467": ["WA", "WA Marketing - Keep Driving"],
    # NSW
    "fcc9b7f5-7a5a-4824-a75e-fe84e2526229": ["NSW", "NSW_CrashClaim"],
    "bb86532e-135e-4eef-99cd-4995bca76d23": ["NSW", "NSW_FaultDrivers"],
    "8bb3cbbb-111a-4001-9d2e-a5a9ffc9623e": ["NSW", "NSW_InstantMarketing"],
    "60976b6a-549d-4f71-951e-312d9f302f50": ["NSW", "NSW_InstantMarketing12"],
    # Law firms
    "984f0ec6-662e-4e67-9bfd-129a0541d0ef": ["LAW", "WA Law Group"],
    "c80fee22-4ba4-4f63-947f-9ad6b5d7e231": ["LAW", "Firths Lawyers"],
}

PAGE_SIZE = 100
MAX_PAGES = 600          # safety cap per campaign (~60k calls)

# ---- Connex plumbing (same pattern as the other builders) -----------------------
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
    return {"Authorization": "Bearer " + tok, "X-Authorization": xa,
            "Accept": "application/json", "X-Timezone": "Europe/London"}

def api_get(E, H, path, params=None):
    url = E + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=90) as r:
        return json.load(r)

def parse_uk(ts):
    if not ts: return None
    try:
        s = ts.replace("Z", "+00:00")
        m = re.search(r"([+-]\d{2})$", s)
        if m: s = s + ":00"
        return datetime.fromisoformat(s).astimezone(UK)
    except Exception:
        return None

# ---- outcome classification -----------------------------------------------------
def classify(name):
    """Return 'na' (No Answer), 'am' (Answer Machine), 'test', or 'other'."""
    n = (name or "").strip().lower()
    if not n:
        return "blank"
    if n == "-test-" or n == "test":
        return "test"
    if "no answer" in n:
        return "na"
    if "answer machine" in n or "answering machine" in n:
        return "am"
    return "other"

def build_outcome_map(E, H):
    """id -> name, from a global outcome list if available, merged with each
    campaign's selectable outcomes (which reliably include No Answer / Answer Machine)."""
    m = {}
    # try a global list first (ignore if the endpoint isn't there)
    try:
        page = 1
        while page <= 50:
            j = api_get(E, H, "/outcome", {"page[number]": page, "page[size]": 100})
            data = j.get("data") or []
            for o in data:
                if o.get("id"): m[o["id"]] = o.get("name", "")
            last = (j.get("meta") or {}).get("page", {}).get("last-page", page)
            if page >= last or not data: break
            page += 1
    except Exception:
        pass
    # merge per-campaign selectable (authoritative for each campaign's own outcomes)
    for cid in CAMPAIGNS:
        try:
            for o in (api_get(E, H, f"/campaign/{cid}/outcome/selectable").get("data") or []):
                if o.get("id") and o["id"] not in m:
                    m[o["id"]] = o.get("name", "")
        except Exception:
            pass
    return m

# ---- main -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="rolling window in days (default 14)")
    ap.add_argument("--out", default="contact.json")
    args = ap.parse_args()

    now_uk = datetime.now(UK)
    win_from = (now_uk - timedelta(days=args.days - 1)).date()   # inclusive
    win_from_s = win_from.isoformat()
    today_s = now_uk.date().isoformat()

    E = env("CXM_ENDPOINT").rstrip("/")
    H = make_headers(get_token(E, env("CXM_CLIENT_ID"), env("CXM_SECRET")), env("CXM_TOKEN"))

    print(f"Window: {win_from_s} .. {today_s}  ({args.days} days)")
    print("Resolving outcome names...")
    oc_map = build_outcome_map(E, H)
    print(f"  {len(oc_map)} outcomes known")

    # per campaign -> per date -> counts
    out = {}
    unresolved = {}
    for cid, (grp, name) in CAMPAIGNS.items():
        days = {}   # date -> [t, na, am, test, blank]
        seen = 0
        page = 1
        while page <= MAX_PAGES:
            try:
                j = api_get(E, H, "/interaction",
                            {"filter[campaign_id]": cid, "sort": "-start_time",
                             "page[number]": page, "page[size]": PAGE_SIZE})
            except Exception as e:
                print(f"  ! {name}: fetch error on page {page}: {e}")
                break
            data = j.get("data") or []
            if not data:
                break
            any_in_window = False
            for it in data:
                dt = parse_uk(it.get("start_time"))
                if dt is None:
                    continue
                d = dt.date().isoformat()
                if d < win_from_s:
                    continue
                any_in_window = True
                if it.get("direction") != "outbound":
                    continue
                oid = it.get("outcome_id")
                nm = oc_map.get(oid)
                if oid and nm is None:
                    unresolved[oid] = unresolved.get(oid, 0) + 1
                k = classify(nm)
                row = days.setdefault(d, [0, 0, 0, 0, 0])
                row[0] += 1
                if   k == "na":    row[1] += 1
                elif k == "am":    row[2] += 1
                elif k == "test":  row[3] += 1
                elif k == "blank": row[4] += 1
                seen += 1
            # sorted newest-first: once an entire page is older than the window, stop
            if not any_in_window:
                break
            page += 1
        # compact: date -> {t,d,na,am}  (d = dispositioned = total - test - blank)
        cam = {}
        for d, (t, na, am, tst, bl) in sorted(days.items()):
            disp = t - tst - bl
            cam[d] = {"t": t, "d": disp, "na": na, "am": am}
        out[cid] = cam
        tt = sum(v["t"] for v in cam.values())
        dd = sum(v["d"] for v in cam.values())
        cc = sum(v["d"] - v["na"] - v["am"] for v in cam.values())
        rate = (100.0 * cc / dd) if dd else 0.0
        print(f"  {grp:<3} {name:<38} calls {tt:>6}  dispositioned {dd:>6}  contact {rate:5.1f}%")

    payload = {
        "generated": now_uk.replace(microsecond=0).isoformat(),
        "window": {"from": win_from_s, "to": today_s, "days": args.days},
        "campaigns": [{"id": cid, "group": g, "name": n} for cid, (g, n) in CAMPAIGNS.items()],
        "data": out,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"\nWrote {args.out}")
    if unresolved:
        top = sorted(unresolved.items(), key=lambda x: -x[1])[:8]
        print(f"  note: {len(unresolved)} outcome id(s) had no name (counted as a real outcome). "
              f"top: {top}")

if __name__ == "__main__":
    main()
