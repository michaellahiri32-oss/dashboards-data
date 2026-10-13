#!/usr/bin/env python3
"""publish_contact.py — push contact.json to the dashboards-data repo (409-resilient).
Env: GITHUB_TOKEN (fine-grained, Contents: Read and write on dashboards-data)."""
import base64, json, os, sys, time, urllib.request, urllib.error

GH_OWNER, GH_REPO, GH_BRANCH = "michaellahiri32-oss", "dashboards-data", "main"
LOCAL = REMOTE = "contact.json"
TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set the GITHUB_TOKEN environment variable first.")
API = "https://api.github.com"
HDRS = {"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "contact-publisher"}

def req(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, headers=HDRS, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception: return e.code, {}

def cur_sha():
    st, j = req("GET", f"{API}/repos/{GH_OWNER}/{GH_REPO}/contents/{REMOTE}?ref={GH_BRANCH}")
    return j.get("sha") if st == 200 and isinstance(j, dict) else None

def main():
    if not os.path.exists(LOCAL):
        sys.exit(f"{LOCAL} not found — run build_contact.py first.")
    raw = open(LOCAL, "rb").read()
    for attempt in range(4):
        payload = {"message": "update contact.json",
                   "content": base64.b64encode(raw).decode(), "branch": GH_BRANCH}
        sha = cur_sha()
        if sha: payload["sha"] = sha
        st, j = req("PUT", f"{API}/repos/{GH_OWNER}/{GH_REPO}/contents/{REMOTE}", payload)
        if st in (200, 201):
            print("contact.json: published"); return
        if st == 409:
            time.sleep(1.5 * (attempt + 1)); continue
        sys.exit(f"publish failed ({st}): {j.get('message','')}")
    sys.exit("publish failed: still conflicting after retries")

if __name__ == "__main__":
    main()
