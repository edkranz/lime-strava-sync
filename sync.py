#!/usr/bin/env python3
"""
lime-strava-sync — pull Lime rides and push them to Strava as GPS activities.

Subcommands:
    gpx          Build GPX for recent Lime trips (default: latest). Writes to ./gpx/.
                 No network writes anywhere — safe dry run.
    strava-auth  One-time Strava OAuth helper -> saves strava-tokens.json.
    upload       Build GPX for a trip and upload it to Strava.

Uses a stored Lime token (probe-output/01-login.json or $LIME_TOKEN) and, if it's
missing or expired, does an SMS login itself (prompts for phone + code). Set
$LIME_PHONE (or in .env) to skip the phone prompt. Strava uploads happen ONLY
under `upload`.

Examples:
    python3 sync.py gpx                       # latest ride -> ./gpx/<id>.gpx
    python3 sync.py gpx --all                 # every ride on page 1
    python3 sync.py strava-auth               # get Strava tokens (once)
    python3 sync.py upload                     # latest ride -> Strava
    python3 sync.py upload --trip-id <id>      # a specific ride
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "probe-output"
GPX_DIR = ROOT / "gpx"
SEEN_FILE = ROOT / "seen-trips.json"          # gitignored
STRAVA_TOKENS = ROOT / "strava-tokens.json"   # gitignored
ENV_FILE = ROOT / ".env"                       # gitignored


def load_dotenv(path=ENV_FILE):
    """Minimal .env loader (stdlib only). Real environment variables win over .env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_dotenv()

LIME_BASE = "https://web-production.lime.bike"
STRAVA_BASE = "https://www.strava.com"

# Shown at the end of every synced ride's Strava description. Override with $REPO_URL.
REPO_URL = os.environ.get("REPO_URL", "https://github.com/edkranz/lime-strava-sync")

# Skip trivial trips (unlock-then-relock, aborted rides). Under either bound = skip.
MIN_DISTANCE_M = 50
MIN_DURATION_S = 30

# Rides finished within this many hours post to the feed; older ones are hidden
# (so a backfill doesn't spam followers, but genuinely recent rides still show).
RECENT_FEED_HOURS = 48

LIME_HEADERS = {
    "User-Agent": "Lime/3.149.0 (iPhone; iOS 17.5; Scale/3.00)",
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "Platform": "iOS",
    "App-Version": "3.149.0",
}


# ─────────────────────────────── HTTP helper ────────────────────────────────
def http(method, url, *, headers=None, body=None, form=None, multipart=None):
    """Return (status, parsed_json_or_text). Never raises on HTTP error."""
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif multipart is not None:
        boundary = "----limestrava" + uuid.uuid4().hex
        data = _encode_multipart(multipart, boundary)
        h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except urllib.error.URLError as e:
        return None, f"NETWORK ERROR: {e}"
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _encode_multipart(fields, boundary):
    """fields: dict of name -> str, or name -> ('filename', bytes, content_type)."""
    parts = []
    for name, val in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        if isinstance(val, tuple):
            filename, content, ctype = val
            parts.append(
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
            )
            parts.append(content if isinstance(content, bytes) else content.encode())
            parts.append(b"\r\n")
        else:
            parts.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            parts.append(f"{val}\r\n".encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


# ─────────────────────────────── Lime side ──────────────────────────────────
_active_token = None  # the token in use this run; refreshed on login


def _stored_token():
    """A previously-saved token, from $LIME_TOKEN or probe-output/01-login.json."""
    tok = os.environ.get("LIME_TOKEN")
    if tok:
        return tok
    try:
        return json.load(open(OUT / "01-login.json"))["token"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None


def lime_login():
    """Interactive SMS login. Saves the response to probe-output/01-login.json.

    Heads up: Lime allows one session per account, so logging in here logs the
    Lime *app* out — you'll re-login the app next time you ride.
    """
    global _active_token
    phone = os.environ.get("LIME_PHONE") or input(
        "Lime phone (+international, e.g. +61412345678): "
    ).strip()
    if not phone.startswith("+"):
        sys.exit("Phone must start with + and a country code.")
    print("Requesting SMS code from Lime...")
    http("GET", LIME_BASE + "/api/rider/v1/login?"
         + urllib.parse.urlencode({"phone": phone}), headers=LIME_HEADERS)
    code = input("Enter the code Lime just texted you: ").strip()
    status, data = http("POST", LIME_BASE + "/api/rider/v1/login",
                        headers=LIME_HEADERS, body={"login_code": code, "phone": phone})
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        sys.exit(f"Lime login failed (HTTP {status}): {str(data)[:200]}")
    OUT.mkdir(exist_ok=True)
    json.dump(data, open(OUT / "01-login.json", "w"))
    _active_token = token
    print("Logged in to Lime ✅  (this logs the Lime app out — one session per account)")
    return token


def lime_token():
    """Return a working Lime token, doing an SMS login if none is stored or it's dead."""
    global _active_token
    _active_token = _stored_token()
    if _active_token:
        status, _ = http("GET", LIME_BASE + "/api/rider/v1/views/user_transactions",
                         headers={**LIME_HEADERS, "Authorization": f"Bearer {_active_token}"})
        if status == 200:
            return _active_token
        print("Stored Lime token is missing or expired — logging in.")
    else:
        print("No Lime token found — logging in.")
    return lime_login()


def lime_get(path, token=None):
    tok = _active_token or token
    status, data = http("GET", LIME_BASE + path,
                        headers={**LIME_HEADERS, "Authorization": f"Bearer {tok}"})
    if status == 401:
        print("Lime token rejected mid-run — logging in again.")
        tok = lime_login()
        status, data = http("GET", LIME_BASE + path,
                            headers={**LIME_HEADERS, "Authorization": f"Bearer {tok}"})
    if status != 200:
        sys.exit(f"Lime GET {path} -> HTTP {status}: {str(data)[:300]}")
    return data


def list_trip_ids(token, all_pages=False):
    """Return trip ids, newest first, from user_transactions."""
    ids, cursor = [], None
    while True:
        path = "/api/rider/v1/views/user_transactions"
        if cursor:
            path += "?" + urllib.parse.urlencode({"cursor": cursor})
        data = lime_get(path, token)
        for group in data.get("user_transactions", []):
            for v in group.get("group_values", []):
                if v.get("object_type") == "Trip":
                    ids.append(v["id"])
        cursor = data.get("next_cursor")
        if not all_pages or not cursor:
            break
    return ids


def get_trip(token, trip_id):
    """Return the normalized fields we care about from trip_summary."""
    q = urllib.parse.urlencode({"transaction_id": trip_id})
    data = lime_get(f"/api/rider/v1/views/trip_summary?{q}", token)
    t = data["data"]["attributes"]["trip"]["attributes"]
    if not t.get("polyline"):
        raise ValueError(f"trip {trip_id} has no polyline (status={t.get('status')})")
    dist, dur = t.get("distance_meters") or 0, t.get("duration_seconds") or 0
    if dist < MIN_DISTANCE_M or dur < MIN_DURATION_S:
        raise ValueError(f"below threshold ({dist} m, {dur} s)")
    return {
        "id": trip_id,
        "polyline": t["polyline"],
        "distance_m": t.get("distance_meters"),
        "duration_s": t.get("duration_seconds"),
        "started_at": t["started_at"],
        "completed_at": t["completed_at"],
        "calories": t.get("calories_burned"),
        "co2_g": t.get("co2_saved_grams"),
        "cost_cents": t.get("cost_amount_cents"),
        "currency": t.get("currency"),
    }


# ─────────────────────────── polyline + GPX ─────────────────────────────────
def decode_polyline(s):
    """Google encoded polyline -> [(lat, lng), ...] (precision 5)."""
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(s):
        for axis in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lng += delta
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def time_of_day(hour):
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 14:
        return "midday"
    if 14 <= hour < 18:
        return "afternoon"
    return "evening"


def build_gpx(trip):
    pts = decode_polyline(trip["polyline"])
    if len(pts) < 2:
        raise ValueError(f"trip {trip['id']}: polyline too short ({len(pts)} pts)")
    start = datetime.fromisoformat(trip["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(trip["completed_at"].replace("Z", "+00:00"))
    span = (end - start).total_seconds()
    n = len(pts)

    def stamp(i):
        # timestamps are synthesized: Lime gives no per-point times.
        return (start + timedelta(seconds=span * i / (n - 1))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    trkpts = "\n".join(
        f'      <trkpt lat="{lat:.6f}" lon="{lng:.6f}"><time>{stamp(i)}</time></trkpt>'
        for i, (lat, lng) in enumerate(pts)
    )
    name = f"{time_of_day(start.astimezone().hour).capitalize()} Lime bike ride 🍋‍🟩"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="lime-strava-sync" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><time>{start.strftime('%Y-%m-%dT%H:%M:%SZ')}</time></metadata>
  <trk>
    <name>{name}</name>
    <trkseg>
{trkpts}
    </trkseg>
  </trk>
</gpx>
""", name, pts


def trip_description(trip):
    bits = []
    if trip["distance_m"]:
        bits.append(f"{trip['distance_m']/1000:.2f} km")
    if trip["duration_s"]:
        bits.append(f"{trip['duration_s']//60}m{trip['duration_s']%60:02d}s")
    if trip["calories"]:
        bits.append(f"{trip['calories']} kcal")
    if trip["co2_g"]:
        bits.append(f"{trip['co2_g']}g CO2 saved")
    if trip["cost_cents"]:
        bits.append(f"{trip['currency']} ${trip['cost_cents']/100:.2f}")
    return ", ".join(bits) + f"\npowered by {REPO_URL}"


# ─────────────────────────────── Strava side ────────────────────────────────
def strava_creds():
    cid = os.environ.get("STRAVA_CLIENT_ID")
    secret = os.environ.get("STRAVA_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit(
            "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET (from your Strava API app "
            "at https://www.strava.com/settings/api)."
        )
    return cid, secret


def strava_auth():
    cid, secret = strava_creds()
    params = urllib.parse.urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": "http://localhost",
        "approval_prompt": "force",
        "scope": "activity:write,activity:read",
    })
    print("1) Open this URL in your browser and click Authorize:\n")
    print(f"   {STRAVA_BASE}/oauth/authorize?{params}\n")
    print("2) Your browser will fail to load a localhost page — that's fine.")
    print("   Copy the `code` value from the redirected URL bar")
    print("   (http://localhost/?state=&code=THISPART&scope=...).\n")
    code = input("Paste the code: ").strip()
    status, data = http("POST", f"{STRAVA_BASE}/oauth/token", form={
        "client_id": cid, "client_secret": secret,
        "code": code, "grant_type": "authorization_code",
    })
    if status != 200:
        sys.exit(f"Token exchange failed (HTTP {status}): {data}")
    STRAVA_TOKENS.write_text(json.dumps({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    }, indent=2))
    print(f"\nSaved tokens -> {STRAVA_TOKENS} (gitignored). You're ready to `upload`.")


def strava_access_token():
    if not STRAVA_TOKENS.exists():
        sys.exit("No Strava tokens. Run `python3 sync.py strava-auth` first.")
    tok = json.loads(STRAVA_TOKENS.read_text())
    if tok["expires_at"] > time.time() + 60:
        return tok["access_token"]
    # refresh
    cid, secret = strava_creds()
    status, data = http("POST", f"{STRAVA_BASE}/oauth/token", form={
        "client_id": cid, "client_secret": secret,
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
    })
    if status != 200:
        sys.exit(f"Strava token refresh failed (HTTP {status}): {data}")
    tok.update(access_token=data["access_token"],
               refresh_token=data["refresh_token"],
               expires_at=data["expires_at"])
    STRAVA_TOKENS.write_text(json.dumps(tok, indent=2))
    return tok["access_token"]


def strava_upload(gpx_text, name, description, trip_id, sport_type, hide_from_home=False):
    access = strava_access_token()
    auth = {"Authorization": f"Bearer {access}"}
    status, data = http("POST", f"{STRAVA_BASE}/api/v3/uploads", headers=auth,
                        multipart={
                            "file": (f"{trip_id}.gpx", gpx_text.encode(), "application/gpx+xml"),
                            "data_type": "gpx",
                            "name": name,
                            "description": description,
                            "external_id": f"lime-{trip_id}",
                        })
    if status not in (200, 201):
        return None, f"upload rejected (HTTP {status}): {data}"
    upload_id = data.get("id")
    # poll until processed
    for _ in range(20):
        time.sleep(2)
        s, d = http("GET", f"{STRAVA_BASE}/api/v3/uploads/{upload_id}", headers=auth)
        if isinstance(d, dict):
            if d.get("error"):
                return None, f"Strava processing error: {d['error']}"
            if d.get("activity_id"):
                aid = d["activity_id"]
                # uploads endpoint can't set these; patch the activity after processing.
                # hide_from_home keeps backfilled rides off followers' feeds while
                # still counting toward the profile and stats.
                http("PUT", f"{STRAVA_BASE}/api/v3/activities/{aid}", headers=auth,
                     body={"sport_type": sport_type, "hide_from_home": hide_from_home})
                return aid, None
    return None, "timed out waiting for Strava to process the upload"


# ──────────────────────────── seen-store (dedupe) ───────────────────────────
def load_seen():
    return set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()


def mark_seen(trip_id):
    seen = load_seen()
    seen.add(trip_id)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ─────────────────────────────── commands ───────────────────────────────────
def pick_trip_ids(args, token):
    if args.trip_id:
        return [args.trip_id]
    ids = list_trip_ids(token, all_pages=args.all)
    if not ids:
        sys.exit("No trips found in user_transactions.")
    return ids if args.all else ids[:1]


def cmd_gpx(args):
    token = lime_token()
    GPX_DIR.mkdir(exist_ok=True)
    for tid in pick_trip_ids(args, token):
        try:
            trip = get_trip(token, tid)
            gpx, name, pts = build_gpx(trip)
        except (ValueError, KeyError) as e:
            print(f"skip {tid[:16]}… {e}")
            continue
        path = GPX_DIR / f"{tid[:20]}.gpx"
        path.write_text(gpx)
        print(f"{name}: {len(pts)} pts, {trip['distance_m']/1000:.2f} km, "
              f"{trip['duration_s']//60}m -> {path}")


def should_hide(trip, args):
    """Hide from the feed unless the ride is recent. --hide/--no-hide force it."""
    if args.no_hide:
        return False
    if args.hide:
        return True
    end = datetime.fromisoformat(trip["completed_at"].replace("Z", "+00:00"))
    age_h = (datetime.now(timezone.utc) - end).total_seconds() / 3600
    return age_h > RECENT_FEED_HOURS


def cmd_upload(args):
    token = lime_token()
    seen = load_seen()
    # By default, rides finished within RECENT_FEED_HOURS post to the feed and
    # older ones are hidden (so a backfill stays quiet but recent rides show).
    # --no-hide forces every ride to the feed; --hide hides every ride.
    ids = pick_trip_ids(args, token)
    for i, tid in enumerate(ids):
        if tid in seen and not args.force:
            print(f"skip {tid[:16]}… already synced (use --force to re-upload)")
            continue
        try:
            trip = get_trip(token, tid)
            gpx, name, pts = build_gpx(trip)
        except (ValueError, KeyError) as e:
            print(f"skip {tid[:16]}… {e}")
            continue
        hide = should_hide(trip, args)
        feed = "hidden from feed" if hide else "posted to feed"
        print(f"uploading {name} ({trip['distance_m']/1000:.2f} km, {len(pts)} pts, {feed})…")
        aid, err = strava_upload(gpx, name, trip_description(trip), tid,
                                 args.sport_type, hide_from_home=hide)
        if err:
            print(f"  FAILED: {err}")
            continue
        mark_seen(tid)
        print(f"  OK -> https://www.strava.com/activities/{aid}")
        # be gentle on Lime/Strava during a multi-trip backfill
        if len(ids) > 1 and i < len(ids) - 1:
            time.sleep(1.5)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gpx", help="build GPX only (no uploads)")
    g.add_argument("--trip-id")
    g.add_argument("--all", action="store_true", help="all trips, not just latest")
    g.set_defaults(func=cmd_gpx)

    a = sub.add_parser("strava-auth", help="one-time Strava OAuth")
    a.set_defaults(func=lambda args: strava_auth())

    u = sub.add_parser("upload", help="build GPX and upload to Strava")
    u.add_argument("--trip-id")
    u.add_argument("--all", action="store_true")
    u.add_argument("--force", action="store_true", help="re-upload even if seen")
    u.add_argument("--sport-type", default="EBikeRide",
                   help="Strava sport_type (default EBikeRide; e.g. Ride)")
    u.add_argument("--hide", action="store_true",
                   help=f"hide every ride from the feed (default: hide only rides "
                        f"older than {RECENT_FEED_HOURS}h)")
    u.add_argument("--no-hide", action="store_true",
                   help="post every ride to the feed, regardless of age")
    u.set_defaults(func=cmd_upload)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
