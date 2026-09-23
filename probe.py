#!/usr/bin/env python3
"""
Lime rider-API probe (throwaway inspection tool).

Goal: log in with your own Lime account, list your recent rides, and dump the
FULL raw JSON of one trip's detail endpoints so we can find out whether Lime
returns a GPS route / polyline for completed trips.

- stdlib only (no pip install)
- interactive: it triggers the SMS code, you paste it back
- saves every raw response under ./probe-output/ for inspection

Usage:
    python3 probe.py
    # or pre-set the phone to skip the prompt:
    LIME_PHONE="+61412345678" python3 probe.py

Nothing here writes to Lime or Strava. It only reads your own account.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

BASE = "https://web-production.lime.bike"
OUT = Path(__file__).parent / "probe-output"

# Lime often rejects requests that don't look like the app. These are generic
# app-ish headers; if you get 401/403 on calls that should work, grab the real
# User-Agent / X-Suuid etc. from a proxy capture and drop them in here.
BASE_HEADERS = {
    "User-Agent": "Lime/3.100.0 (iPhone; iOS 17.5; Scale/3.00)",
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "Platform": "iOS",
    "App-Version": "3.100.0",
}

# Keys/patterns that would mean "there IS a route we can turn into GPX".
ROUTE_HINTS = re.compile(
    r"polyline|encoded|route|geojson|coordinate|coords|waypoint|"
    r"\blat\b|\blng\b|\blon\b|latitude|longitude|path|track|geometry|breadcrumb",
    re.IGNORECASE,
)


def req(method, path, *, token=None, params=None, body=None):
    """Make a request, return (status, parsed_json_or_text). Never raises on HTTP error."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = dict(BASE_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except urllib.error.URLError as e:
        return None, f"NETWORK ERROR: {e}"
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def save(name, obj):
    OUT.mkdir(exist_ok=True)
    p = OUT / name
    p.write_text(json.dumps(obj, indent=2) if not isinstance(obj, str) else obj)
    print(f"    saved -> {p}")


def find_route_hints(obj, path=""):
    """Recursively collect JSON paths whose key OR string value looks route-y."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}"
            if ROUTE_HINTS.search(str(k)):
                preview = repr(v)[:120]
                hits.append(f"{here} = {preview}")
            hits += find_route_hints(v, here)
    elif isinstance(obj, list):
        # only descend into first couple of items to keep output sane
        for i, v in enumerate(obj[:3]):
            hits += find_route_hints(v, f"{path}[{i}]")
    elif isinstance(obj, str) and len(obj) > 20 and ROUTE_HINTS.search(obj):
        hits.append(f"{path} (string value looks route-y) = {obj[:120]!r}")
    return hits


def main():
    phone = os.environ.get("LIME_PHONE") or input(
        "Lime phone in +international format (e.g. +61412345678): "
    ).strip()
    if not phone.startswith("+"):
        print("Phone must start with + and country code.")
        sys.exit(1)

    # 1) trigger the SMS code
    print("\n[1] Requesting SMS code...")
    status, resp = req("GET", "/api/rider/v1/login", params={"phone": phone})
    print(f"    HTTP {status}")
    if status not in (200, 201, 204):
        print("    Unexpected response — dumping and continuing anyway:")
        print("   ", json.dumps(resp)[:500] if not isinstance(resp, str) else resp[:500])

    # 2) submit the code
    code = input("\n[2] Enter the 6-digit code Lime just texted you: ").strip()
    status, resp = req(
        "POST", "/api/rider/v1/login", body={"login_code": code, "phone": phone}
    )
    print(f"    HTTP {status}")
    if isinstance(resp, str) or status >= 400:
        print("    Login failed. Raw response:")
        print("   ", resp if isinstance(resp, str) else json.dumps(resp)[:800])
        print("\n    If this is a headers/format issue, capture the real login")
        print("    request from a proxy and adjust BASE_HEADERS / the body shape.")
        sys.exit(1)

    # token location varies by API version; search common spots
    token = (
        resp.get("token")
        or resp.get("access_token")
        or (resp.get("data") or {}).get("token")
        or (resp.get("meta") or {}).get("token")
    )
    save("01-login.json", resp)
    if not token:
        print("    Logged in, but couldn't find the token field automatically.")
        print("    Look in probe-output/01-login.json and set it manually.")
        sys.exit(1)
    print("    Got a token.")

    # 3) ride history
    print("\n[3] Fetching ride history...")
    status, hist = req(
        "GET", "/api/rider/v1/views/ride_history", token=token,
        params={"page_limit": 10},
    )
    print(f"    HTTP {status}")
    save("02-ride_history.json", hist)

    # 4) pull trip ids out of the history (structure unknown, so scan broadly)
    trip_ids = []
    def scan_ids(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("trip_id", "transaction_id", "id") and isinstance(v, str):
                    trip_ids.append(v)
                scan_ids(v)
        elif isinstance(o, list):
            for v in o:
                scan_ids(v)
    scan_ids(hist)
    trip_ids = list(dict.fromkeys(trip_ids))  # dedupe, keep order
    print(f"    Candidate trip/transaction IDs found: {trip_ids[:10]}")

    if not trip_ids:
        print("    No IDs auto-detected. Inspect probe-output/02-ride_history.json")
        print("    and re-run pointing at a real trip_id.")
        return

    tid = trip_ids[0]
    print(f"\n[4] Dumping detail for most recent trip: {tid}")

    for label, path, params in [
        ("trip_receipt", "/api/rider/v2/views/trip_receipt", {"trip_id": tid}),
        ("trip_summary", "/api/rider/v2/views/trip_summary",
         {"transaction_id": tid, "source": "ride_history"}),
    ]:
        status, detail = req("GET", path, token=token, params=params)
        print(f"    {label}: HTTP {status}")
        save(f"03-{label}.json", detail)
        hints = find_route_hints(detail)
        if hints:
            print(f"    >>> POSSIBLE ROUTE DATA in {label}:")
            for h in hints:
                print("        ", h)
        else:
            print(f"    (no route-looking fields in {label})")

    print("\nDone. The verdict:")
    print("  - If you saw '>>> POSSIBLE ROUTE DATA' above, we can build real GPX")
    print("    activities with a map. Send me probe-output/03-*.json.")
    print("  - If not, Lime only gives totals -> Strava manual activity (no map).")
    print("\nToken and raw responses are in ./probe-output/ (gitignored). Don't commit them.")


if __name__ == "__main__":
    main()
