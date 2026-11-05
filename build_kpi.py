"""
build_kpi.py - monthly KPI figures for the Gorilla KPI Report (writes kpi.json).

Reads the Call Report snapshot (instructs / inbound / abandons per month) and the
spend sheet, and writes kpi.json for the KPI page to pull. Run AFTER build_report_api.py
(it needs the freshly-built snapshot).  Env: same as the other builds.
"""
import json, os, collections, datetime
import build_report_api as bra

SNAP = ["UK-Accident-Management-Call-Report-live.html", "report.html", "UK-Accident-Management-Call-Report.html"]
MN = ["", "Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

# Full monthly Google Ads spend for months the spend sheet doesn't cover (keyed in by hand).
MANUAL_SPEND = {"2026-06": 78172, "2026-07": 247597, "2026-08": 230840}

# --- operating hours (mirrors build_report_api) - used for the ON-SHIFT drop rate ---
def _dow(ds):  # Sun=0 .. Sat=6, matching the report
    y, m, d = map(int, ds.split("-")); return (datetime.date(y, m, d).weekday() + 1) % 7
def _open_window(ds):
    if ds in bra.OPEN_OVERRIDES: return bra.OPEN_OVERRIDES[ds]
    pick = None
    for sh in bra.SHIFTS:
        if sh["from"] <= ds: pick = sh
    return pick["hours"][_dow(ds)] if pick else None
def on_shift(ds, hour):
    w = _open_window(ds)
    return bool(w) and w[0] <= hour < w[1]

def snapshot():
    f = next((c for c in SNAP if os.path.exists(c)), None)
    if not f:
        raise SystemExit("No Call Report snapshot found - run build_report_api.py first.")
    html = open(f, encoding="utf-8", errors="replace").read()
    i = html.index("window.__SNAPSHOT__=") + len("window.__SNAPSHOT__=")
    return json.JSONDecoder().raw_decode(html, i)[0], f

def main():
    snap, f = snapshot()
    data = snap["data"]   # [date, hour, minute, dir, sale, aband, hrdup]
    mon = collections.defaultdict(lambda: {"instructs":0, "inbound":0, "abandons":0, "inbound_shift":0})
    for r in data:
        ds = r[0]; hr = r[1]; m = ds[:7]
        if r[4] == 1 and r[6] == 0: mon[m]["instructs"] += 1       # leads: counted any time
        if r[3] == "in" and 8 <= hr < 20: mon[m]["inbound_shift"] += 1   # inbound within line hours 8am-8pm
        if on_shift(ds, hr):                                        # drop rate: on-shift only
            if r[3] == "in": mon[m]["inbound"]  += 1
            if r[5] == 1:    mon[m]["abandons"] += 1

    spend_daily = bra.fetch_spend(bra.SPEND_CSV_URL) or {}
    spend_mon = collections.defaultdict(float)
    for d, v in spend_daily.items(): spend_mon[d[:7]] += v
    earliest = min(spend_daily) if spend_daily else None
    this_m = datetime.date.today().strftime("%Y-%m")

    def month_spend(m):
        if m in MANUAL_SPEND: return float(MANUAL_SPEND[m])
        if m not in spend_mon: return None
        # a month only partly covered by the sheet (and already finished) isn't reliable for CPL
        if earliest and m == earliest[:7] and earliest[8:10] != "01" and m != this_m:
            return None
        return round(spend_mon[m], 2)

    start = bra.CUTOFF_DATE[:7]
    allm = sorted(m for m in set(list(mon) + [this_m]) if m >= start)
    months = []
    for m in allm:
        y, mm = m.split("-")
        x = mon.get(m, {"instructs":0, "inbound":0, "abandons":0, "inbound_shift":0})
        months.append({"m": m, "label": f"{MN[int(mm)]} {y[2:]}",
                       "instructs": x["instructs"], "inbound": x["inbound"],
                       "inbound_shift": x["inbound_shift"],
                       "abandons": x["abandons"], "spend": month_spend(m)})

    out = {"generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "range": {"from": bra.CUTOFF_DATE, "to": max(r[0] for r in data)},
           "months": months}
    with open("kpi.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    tot = sum(x["instructs"] for x in months)
    print(f"Wrote kpi.json from {f}: {len(months)} months, {tot} instructs total.")
    for x in months:
        dr  = (x["abandons"]/x["inbound"]*100) if x["inbound"] else 0
        cpl = (x["spend"]/x["instructs"]) if (x["spend"] and x["instructs"]) else None
        sp  = ("GBP%.0f" % x["spend"]) if x["spend"] else "   -"
        cp  = ("GBP%.2f" % cpl) if cpl else "  -"
        print(f"  {x['label']:<7} instr {x['instructs']:>4}  drop {dr:>4.1f}%  spend {sp:>9}  CPL {cp}")

if __name__ == "__main__":
    main()
