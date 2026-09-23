#!/usr/bin/env python3
"""
Option-1 experiment: log the sync in as its OWN device (stable X-Device-Token)
and see whether that session can coexist with the phone app.

Run this, enter the SMS code. It saves the token + device id to
probe-output/device-login.json. Then follow the steps it prints.
"""
import json, os, sys, urllib.request, urllib.error, urllib.parse, uuid
from pathlib import Path

BASE = "https://web-production.lime.bike"
OUT = Path(__file__).parent / "probe-output"
DEV_FILE = OUT / "sync-device-token.txt"

# A stable device identity for "the sync device" — reused across logins so Lime
# sees the same device each time (the whole point of the experiment).
OUT.mkdir(exist_ok=True)
if DEV_FILE.exists():
    device_token = DEV_FILE.read_text().strip()
else:
    device_token = str(uuid.uuid4())
    DEV_FILE.write_text(device_token)

HEADERS = {
    "User-Agent": "Lime/3.149.0 (iPhone; iOS 17.5; Scale/3.00)",
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "Platform": "iOS",
    "App-Version": "3.149.0",
    "X-Device-Token": device_token,
    "X-Suuid": device_token,          # some Lime builds use this for device id
    "Device-Id": device_token,
}


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(HEADERS)
    if data:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    phone = os.environ.get("LIME_PHONE") or input("Lime phone (+61...): ").strip()
    print(f"\nUsing device token: {device_token}")
    print("[1] Requesting SMS code...")
    s, _ = req("GET", "/api/rider/v1/login?" + urllib.parse.urlencode({"phone": phone}))
    print(f"    HTTP {s}")
    code = input("[2] Enter SMS code: ").strip()
    s, body = req("POST", "/api/rider/v1/login", {"login_code": code, "phone": phone})
    print(f"    HTTP {s}")
    try:
        d = json.loads(body)
        token = d.get("token")
    except json.JSONDecodeError:
        token = None
    if not token:
        print("    login failed:", body[:300]); sys.exit(1)
    (OUT / "device-login.json").write_text(json.dumps({"token": token, "device_token": device_token}))
    # verify
    r = urllib.request.Request(BASE + "/api/rider/v1/views/user_transactions",
                               headers={**HEADERS, "Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(r, timeout=30)
        print("    token works ✅  (saved to probe-output/device-login.json)")
    except urllib.error.HTTPError as e:
        print(f"    token verify failed: HTTP {e.code}")
        sys.exit(1)
    print("\nNEXT:")
    print("  1. Open the Lime app on your phone and log back in (view your ride history).")
    print("  2. Tell Claude — it will re-test this token to see if it survived.")


if __name__ == "__main__":
    main()
