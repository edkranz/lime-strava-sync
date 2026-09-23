#!/usr/bin/env python3
"""Find the trip-detail endpoint using a real Trip id from user_transactions."""
import json, re, urllib.request, urllib.error, urllib.parse
from pathlib import Path

BASE = "https://web-production.lime.bike"
OUT = Path(__file__).parent / "probe-output"
login = json.load(open(OUT / "01-login.json"))
TOKEN = login["token"]
HEADERS = {
    "User-Agent": "Lime/3.149.0 (iPhone; iOS 17.5; Scale/3.00)",
    "Accept": "application/json", "Accept-Language": "en-US",
    "Platform": "iOS", "App-Version": "3.149.0",
    "Authorization": f"Bearer {TOKEN}",
}
ROUTE = re.compile(r"polyline|encoded|route|geojson|coordinate|coords|waypoint|"
                   r"\blat\b|\blng\b|\blon\b|latitude|longitude|path|track|"
                   r"geometry|breadcrumb|distance|duration|meters|miles|km",
                   re.IGNORECASE)

# grab first Trip id
txn = json.load(open(OUT / "discover_api_rider_v1_views_user_transactions.json"))
trip_id = None
for g in txn["user_transactions"]:
    for v in g.get("group_values", []):
        if v.get("object_type") == "Trip":
            trip_id = v["id"]; break
    if trip_id: break
print("using trip id:", trip_id, "\n")

enc = urllib.parse.quote(trip_id, safe="")

CANDIDATES = [
    f"/api/rider/v2/views/trip_receipt?trip_id={enc}",
    f"/api/rider/v1/views/trip_receipt?trip_id={enc}",
    f"/api/rider/v2/views/trip_summary?transaction_id={enc}&source=user_transactions",
    f"/api/rider/v1/views/trip_summary?transaction_id={enc}",
    f"/api/rider/v2/views/transaction?id={enc}",
    f"/api/rider/v1/views/transaction?id={enc}",
    f"/api/rider/v1/views/transaction_detail?id={enc}",
    f"/api/rider/v2/views/transaction_detail?transaction_id={enc}",
    f"/api/rider/v1/trips/{enc}",
    f"/api/rider/v2/trips/{enc}",
    f"/api/rider/v1/views/trip_detail?trip_id={enc}",
    f"/api/rider/v2/views/trip_detail?trip_id={enc}",
]

def call(path):
    r = urllib.request.Request(BASE + path, method="GET", headers=HEADERS)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8","replace")
    except urllib.error.URLError as e:
        return None, str(e)

def hints(obj, p=""):
    h=[]
    if isinstance(obj,dict):
        for k,v in obj.items():
            if ROUTE.search(str(k)): h.append(f"{p}.{k} = {repr(v)[:90]}")
            h+=hints(v,f"{p}.{k}")
    elif isinstance(obj,list):
        for i,v in enumerate(obj[:3]): h+=hints(v,f"{p}[{i}]")
    elif isinstance(obj,str) and len(obj)>20 and ROUTE.search(obj):
        h.append(f"{p} (str) = {obj[:90]!r}")
    return h

for path in CANDIDATES:
    status, body = call(path)
    tag = "  <<< 200" if status==200 else ""
    print(f"{str(status):>5}  {path.split('?')[0]}{tag}")
    if status==200:
        name = path.split("?")[0].replace("/","_")[:50]
        (OUT/f"detail{name}.json").write_text(body)
        try:
            obj=json.loads(body)
            print("       top-level:", list(obj.keys()) if isinstance(obj,dict) else type(obj))
            hh=hints(obj)
            if hh:
                print("       >>> DISTANCE/ROUTE-LOOKING FIELDS:")
                for x in hh: print("        ", x)
            else:
                print("       (no distance/route-looking fields)")
        except Exception as e:
            print("       non-JSON:", e)
