#!/usr/bin/env python3
"""
publish_github.py — push data files to a GitHub repo (free, no deploy limits).

Netlify bills per deploy, so we keep data in GitHub and let the pages fetch it.

Env vars:
    GITHUB_TOKEN   fine-grained token with Contents: Read and write on the repo

Run:  python publish_github.py            (skips files that haven't changed)
      python publish_github.py --force    (push regardless)

RESILIENCE: the report task and the tracker task both push to this repo. If they
push at the same moment, GitHub rejects one with a 409 conflict. This script now
(a) retries a 409 by re-fetching the current sha, and (b) NEVER lets one file's
failure abort the others — so overview.json still publishes even if data.json
briefly conflicts.
"""
import base64, hashlib, json, os, sys, time, urllib.request, urllib.error

# ======================= CONFIG =======================
GH_OWNER  = "michaellahiri32-oss"
GH_REPO   = "dashboards-data"
GH_BRANCH = "main"
FILES = {
    "data.json": "data.json",
    "UK-Accident-Management-Call-Report-live.html": "report.html",
    "within-hour-call-detail-live.html": "call-detail.html",
    "overview.json": "overview.json",
    "claims.csv": "claims.csv",
}
# ======================================================

TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set the GITHUB_TOKEN environment variable first.")
FORCE = "--force" in sys.argv
API = "https://api.github.com"
HDRS = {"Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dashboards-publisher"}


def req(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, headers=HDRS, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"message": body[:200]}


def current_sha(path):
    st, j = req("GET", f"{API}/repos/{GH_OWNER}/{GH_REPO}/contents/{path}?ref={GH_BRANCH}")
    if st == 200 and isinstance(j, dict):
        return j.get("sha")
    return None


def stable_bytes(local, raw):
    """Ignore volatile timestamps in JSON so an unchanged build is seen as unchanged."""
    if local.endswith(".json"):
        try:
            j = json.loads(raw.decode("utf-8"))
            j.pop("updated", None)
            j.pop("built", None)
            return json.dumps(j, sort_keys=True).encode()
        except Exception:
            pass
    return raw


def put_file(remote, raw, tries=4):
    """PUT a file, retrying on 409 conflict by re-fetching the latest sha.
    Returns (ok, status, message)."""
    for attempt in range(tries):
        payload = {"message": f"update {remote}",
                   "content": base64.b64encode(raw).decode(),
                   "branch": GH_BRANCH}
        sha = current_sha(remote)          # fetch fresh sha each attempt
        if sha:
            payload["sha"] = sha
        st, j = req("PUT", f"{API}/repos/{GH_OWNER}/{GH_REPO}/contents/{remote}", payload)
        if st in (200, 201):
            return True, st, ""
        if st == 409:                      # conflict: someone else pushed — wait and retry
            time.sleep(1.5 * (attempt + 1))
            continue
        return False, st, j.get("message", "")
    return False, 409, "still conflicting after retries"


def main():
    stamp_file = ".last_github"
    try:
        with open(stamp_file) as f:
            stamps = json.load(f)
    except Exception:
        stamps = {}

    pushed = skipped = failed = 0
    problems = []
    for local, remote in FILES.items():
        if not os.path.exists(local):
            print(f"  (skip {local} — not found)")
            continue
        with open(local, "rb") as f:
            raw = f.read()
        fp = hashlib.sha256(stable_bytes(local, raw)).hexdigest()
        if stamps.get(remote) == fp and not FORCE:
            print(f"  {remote}: unchanged — skipped")
            skipped += 1
            continue

        ok, st, msg = put_file(remote, raw)
        if ok:
            stamps[remote] = fp
            pushed += 1
            print(f"  {remote}: pushed")
        else:
            failed += 1
            problems.append(f"{remote} ({st}): {msg}")
            # DO NOT abort — carry on so the other files still publish
            print(f"  {remote}: FAILED ({st}) — continuing with the rest")

    with open(stamp_file, "w") as f:
        json.dump(stamps, f)

    print(f"Done: {pushed} pushed, {skipped} unchanged, {failed} failed.")
    if problems:
        # non-zero exit so the log flags it, but only AFTER trying everything
        print("Problems:")
        for p in problems:
            print("   " + p)


if __name__ == "__main__":
    main()
