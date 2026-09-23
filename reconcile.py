#!/usr/bin/env python3
"""
Reconcile already-uploaded Lime activities on Strava with the current template
and thresholds.

  reconcile.py plan      # read-only: list what would be retitled vs deleted
  reconcile.py apply     # retitle/re-describe keepers to the new template
  reconcile.py delete    # DELETE the sub-threshold junk activities

Maps Strava activities back to Lime trips via external_id = "lime-<tripid>.gpx".
"""
import json, sys, time, urllib.request, urllib.error, urllib.parse
from pathlib import Path
import sync

STRAVA = "https://www.strava.com/api/v3"


def sauth():
    return {"Authorization": f"Bearer {sync.strava_access_token()}"}


def list_lime_activities():
    """All Strava activities whose external_id marks them as ours."""
    out, page = [], 1
    while True:
        url = f"{STRAVA}/athlete/activities?per_page=100&page={page}"
        req = urllib.request.Request(url, headers=sauth())
        batch = json.load(urllib.request.urlopen(req))
        if not batch:
            break
        for a in batch:
            ext = a.get("external_id") or ""
            if ext.startswith("lime-"):
                out.append(a)
        page += 1
    return out


def trip_id_from(activity):
    # external_id == "lime-<tripid>.gpx"
    return activity["external_id"][len("lime-"):].rsplit(".gpx", 1)[0]


def classify():
    token = sync.lime_token()
    acts = list_lime_activities()
    keep, junk = [], []
    for a in acts:
        tid = trip_id_from(a)
        try:
            trip = sync.get_trip(token, tid)          # raises if below threshold / no polyline
            keep.append((a, trip))
        except ValueError as e:
            junk.append((a, str(e)))
        time.sleep(0.3)
    return keep, junk


def cmd_plan():
    keep, junk = classify()
    print(f"\n{len(keep)} keepers (will be retitled to new template):")
    for a, _ in keep:
        print(f"  {a['id']}  {a['distance']/1000:5.2f}km  {a['name']}")
    print(f"\n{len(junk)} below-threshold to DELETE:")
    for a, why in junk:
        print(f"  {a['id']}  {a['distance']/1000:5.2f}km  {a['name']}   [{why}]")


def cmd_apply():
    keep, _ = classify()
    for a, trip in keep:
        _, name, _ = sync.build_gpx(trip)
        body = json.dumps({"name": name, "description": sync.trip_description(trip),
                           "sport_type": "EBikeRide"}).encode()
        req = urllib.request.Request(f"{STRAVA}/activities/{a['id']}", data=body,
                                     method="PUT",
                                     headers={**sauth(), "Content-Type": "application/json"})
        urllib.request.urlopen(req)
        print(f"  updated {a['id']} -> {name}")
        time.sleep(0.3)
    print(f"\nRetitled {len(keep)} activities.")


def cmd_delete():
    # NOTE: Strava's public API has no delete-activity endpoint (DELETE returns
    # 401), so junk activities must be removed manually from the website/app.
    # This lists them with direct links; the threshold filter in sync.py stops
    # new ones being uploaded in the first place.
    _, junk = classify()
    if not junk:
        print("No below-threshold activities on Strava. Nothing to remove.")
        return
    print("Strava's API can't delete activities — remove these manually:")
    for a, why in junk:
        print(f"  {a['distance']:.0f} m  [{why}]  https://www.strava.com/activities/{a['id']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    {"plan": cmd_plan, "apply": cmd_apply, "delete": cmd_delete}[cmd]()
