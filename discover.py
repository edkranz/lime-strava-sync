#!/usr/bin/env python3
"""
Endpoint discovery: reuse the saved token from probe-output/01-login.json and
try a batch of candidate trip-history paths. Reports HTTP status for each so we
can find the current one (200) vs gone (404) vs auth issue (401/403).

No new SMS needed — reads the existing token.
"""
import json
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

BASE = "https://web-production.lime.bike"
OUT = Path(__file__).parent / "probe-output"

login = json.load(open(OUT / "01-login.json"))
TOKEN = login["token"]
UID = login["user"]["id"]

HEADERS = {
    "User-Agent": "Lime/3.149.0 (iPhone; iOS 17.5; Scale/3.00)",
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "Platform": "iOS",
    "App-Version": "3.149.0",
    "Authorization": f"Bearer {TOKEN}",
}

# (method, path). {uid} filled in below.
CANDIDATES = [
    ("GET", "/api/rider/v1/trips"),
    ("GET", "/api/rider/v2/trips"),
    ("GET", "/api/rider/v1/trips?page%5Blimit%5D=5"),
    ("GET", "/api/rider/v2/trips?page%5Blimit%5D=5"),
    ("GET", "/api/rider/v1/rides"),
    ("GET", "/api/rider/v2/rides"),
    ("GET", "/api/rider/v1/views/ride_history?page_limit=5"),   # known-404 baseline
    ("GET", "/api/rider/v2/views/ride_history?page_limit=5"),
    ("GET", "/api/rider/v1/views/trip_history"),
    ("GET", "/api/rider/v2/views/trip_history"),
    ("GET", "/api/rider/v1/views/trips"),
    ("GET", "/api/rider/v2/views/trips"),
    ("GET", "/api/rider/v1/views/user_transactions"),
    ("GET", "/api/rider/v2/views/user_transactions"),
    ("GET", "/api/rider/v1/user_transactions"),
    ("GET", "/api/rider/v1/views/profile"),        # sanity: a v1 view that should work
    ("GET", "/api/rider/v2/views/profile"),
    ("GET", f"/api/rider/v1/users/{UID}/trips"),
    ("GET", "/api/rider/v1/views/trip_history_list"),
    ("GET", "/api/rider/v2/views/history"),
]


def call(method, path):
    r = urllib.request.Request(BASE + path, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return None, str(e)


print(f"user id: {UID}\n")
winners = []
for method, path in CANDIDATES:
    status, body = call(method, path)
    tag = ""
    if status == 200:
        tag = "  <<< 200 OK"
        winners.append((path, body))
    print(f"{str(status):>5}  {method} {path}{tag}")

for path, body in winners:
    safe = path.replace("/", "_").replace("?", "_").replace("%", "")[:60]
    p = OUT / f"discover{safe}.json"
    try:
        p.write_text(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        p.write_text(body)
    # show top-level shape
    try:
        obj = json.loads(body)
        keys = list(obj.keys()) if isinstance(obj, dict) else f"[list len {len(obj)}]"
        print(f"\n200 {path}\n  top-level: {keys}\n  saved -> {p}")
    except Exception:
        print(f"\n200 {path} (non-JSON), saved -> {p}")
