# lime-strava-sync

Sync your [Lime](https://www.li.me/) bike/scooter rides to [Strava](https://www.strava.com/) as GPS activities — with the actual route drawn on the map.

Lime has no public API, but the mobile app's private **rider API** exposes everything needed: an encoded route polyline, distance, duration, and timestamps for every completed trip. This repo documents that API (reverse-engineered from the app) and specs a sync tool built on top of it.

> **Status:** API reverse-engineered and verified against a live account (Sydney, app v3.149). Sync implementation TODO. See [Sync design](#sync-design).
>
> **Legal:** This uses Lime's private endpoints, which almost certainly violates their Terms of Service. Use only against **your own account**, poll infrequently, and don't redistribute captured tokens. No affiliation with Lime or Strava.

---

## The Lime rider API

Base URL: `https://web-production.lime.bike`

All authenticated calls send `Authorization: Bearer <token>`. The API is JSON:API-styled (`data.attributes`, cursor pagination). App-like headers help avoid rejection:

```
User-Agent: Lime/3.149.0 (iPhone; iOS 17.5; Scale/3.00)
Platform: iOS
App-Version: 3.149.0
```

### 1. Authentication (SMS one-time code)

```
GET /api/rider/v1/login?phone=%2B61412345678       # triggers the SMS code
POST /api/rider/v1/login                            # submit the code
     Content-Type: application/json
     {"login_code": "123456", "phone": "+61412345678"}
```

The POST returns the bearer token plus a full user profile:

```jsonc
{
  "token": "<JWT, ~211 chars>",
  "user": {
    "id": "NFZE6ULW3GKEM",
    "attributes": { "num_trips": 48, "currency": "AUD", ... }
  }
}
```

Email magic-link login also exists (`POST /api/rider/v2/onboarding/magic-link` → `.../onboarding/login`) but SMS is simpler to automate.

> **Token lifetime is the main operational risk** and is not yet measured — see [Open questions](#open-questions).

### 2. Trip history — `user_transactions`

The old `views/ride_history` path is **gone (404)** in the current API. The working endpoint is:

```
GET /api/rider/v1/views/user_transactions
GET /api/rider/v1/views/user_transactions?cursor=<next_cursor>   # pagination
```

Returns transactions grouped by month, plus a `next_cursor` for the next page:

```jsonc
{
  "user_transactions": [
    {
      "group_title": "September 2026",
      "group_values": [
        {
          "title": "Bike ride",
          "date": "9/23/2026, 8:46 AM",
          "cost_amount": "AUD $2.75",
          "object_type": "Trip",              // filter on this
          "id": "GIYDENRNGA4S2MRSKQ...="       // opaque base32 id → trip_summary
        }
      ]
    }
  ],
  "next_cursor": "GIYDENRNGA3C2MJYKQ...="
}
```

Only rows with `object_type == "Trip"` are rides (there are also fees, top-ups, etc.). The `id` is what you pass to `trip_summary`.

### 3. Trip detail — `trip_summary` ⭐

This is the endpoint that makes real GPS sync possible. **Not documented anywhere else.**

```
GET /api/rider/v1/views/trip_summary?transaction_id=<id>
```

Relevant fields under `data.attributes.trip.attributes`:

| Field | Example | Notes |
|---|---|---|
| `polyline` | `"jlumEus\|y[?CAG..."` | **Encoded polyline** (Google algorithm, precision 5). The route. |
| `distance_meters` | `5014` | |
| `duration_seconds` | `1147` | |
| `started_at` | `2026-09-22T22:46:53.000Z` | ISO 8601 UTC |
| `completed_at` | `2026-09-22T23:06:00.000Z` | |
| `calories_burned` | `56` | optional, for description |
| `co2_saved_grams` | `1280` | optional |
| `cost_amount_cents` | `275` | + `currency` |

The `v2` variant (`/api/rider/v2/views/trip_summary?transaction_id=<id>&source=user_transactions`) returns the same polyline under `data.attributes.map_items[].attributes.polyline`.

**Caveats:**
- The polyline is **simplified** (~37 points for a 5 km ride), so the map is approximate, not survey-grade.
- There are **no per-point timestamps** — only ride start/end. See the timestamp strategy below.
- `route_image_url` was `null` on the tested trip; don't rely on it.

---

## Sync design

```
Lime login (SMS) ──> token ──┐
                             ▼
        GET user_transactions (paged via next_cursor)
                             │  filter object_type == "Trip"
                             ▼
        for each new trip id (not in seen-store):
             GET trip_summary?transaction_id=id
                             │
                             ▼
             decode polyline ─> synthesize timestamps ─> build GPX
                             │
                             ▼
             POST https://www.strava.com/api/v3/uploads  (data_type=gpx)
                             │
                             ▼
             record trip id in seen-store
```

### Timestamp synthesis

GPX track points want timestamps but Lime gives none per point. Distribute the `N` decoded polyline points evenly across `[started_at, completed_at]`:

```
t_i = started_at + (completed_at - started_at) * i / (N - 1)
```

Pace comes out roughly constant. Good enough for Strava to draw the map and compute distance/time; it won't reflect real speed variation.

### Strava upload

- Personal Strava API app in **single-athlete** mode; store the refresh token as a secret.
- `POST /api/v3/uploads` with `data_type=gpx`, the GPX body, plus `name`, `description`, `sport_type`.
- Strava has **no e-scooter/e-bike-share type** — pick `EBikeRide` (bikes) or `Ride`. Make it configurable.
- Poll the returned upload id until it processes; on `status: "ready"` you get an `activity_id`.
- Dedupe on the Lime trip `id` (e.g. a KV store / local sqlite / JSON file). Also set the GPX `<time>` so Strava's own duplicate detection helps.

### Deployment options

- **Cloudflare Worker + Cron Trigger** (every few hours) with KV for the seen-store — cheap, unattended.
- Or a local cron / GitHub Action.

The blocker for full unattended operation is **token lifetime**: if the Lime JWT is short-lived and refresh requires a fresh SMS code, a human is needed periodically. Measure this before committing to a schedule (below).

---

## Reverse-engineering tools (in this repo)

These are the throwaway probes used to map the API. They read your own account only and write nothing to Lime/Strava.

| Script | Purpose |
|---|---|
| `probe.py` | Interactive: SMS login → save token → fetch history → dump a trip's detail |
| `discover.py` | Reuse saved token, brute-probe candidate history endpoints |
| `detail.py` | Find the trip-detail endpoint + scan for route/distance fields |

Raw responses land in `probe-output/` (**gitignored** — they contain your JWT and personal data).

---

## Open questions

- [ ] **Token lifetime** — how long does the login JWT stay valid? Is there a refresh endpoint, or does every renewal need a new SMS? *This decides whether the sync can run unattended.*
- [ ] **Rate limits** — how aggressively can `user_transactions` / `trip_summary` be polled before Lime flags the account?
- [ ] Does `next_cursor` reliably page all the way back through history for the initial backfill (48 trips on the test account)?
- [ ] Do scooter trips (vs bikes) return the same `trip_summary` shape and a polyline?
```
