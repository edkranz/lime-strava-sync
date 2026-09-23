# lime-strava-sync

Sync your [Lime](https://www.li.me/) bike/scooter rides to [Strava](https://www.strava.com/) as GPS activities — with the actual route drawn on the map.

Lime has no public API, but the mobile app's private **rider API** exposes everything needed: an encoded route polyline, distance, duration, and timestamps for every completed trip. This repo documents that API (reverse-engineered from the app) and provides a working sync tool built on top of it.

> **Status:** Working, as a **manual / on-demand** tool (`sync.py`). Verified end-to-end against a live account (Sydney, app v3.149): login → history → route polyline → GPX → Strava activity with map.
>
> **It cannot run unattended.** Lime allows **only one active session per account** (see [Why it's manual](#why-its-manual-one-session-per-account)), so a background sync and the phone app can't both be logged in. This is a hard limit of Lime's design, not a TODO.
>
> **Legal:** This uses Lime's private endpoints, which almost certainly violates their Terms of Service. Use only against **your own account**, infrequently, and don't redistribute captured tokens. No affiliation with Lime or Strava.

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
    "id": "XXXXXXXXXXXXX",
    "attributes": { "num_trips": 42, "currency": "AUD", ... }
  }
}
```

Email magic-link login also exists (`POST /api/rider/v2/onboarding/magic-link` → `.../onboarding/login`, with an `X-Device-Token`) but SMS is simpler.

The JWT payload carries `expires_at = iat + 120s`, but **that claim is not enforced** — tokens keep working for hours. What actually ends a session is a *new login on the account* (see below), not the clock.

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
          "id": "AAAAAAAAAAAAAAAAAAAA...="     // opaque base32 id → trip_summary
        }
      ]
    }
  ],
  "next_cursor": "BBBBBBBBBBBBBBBBBBBB...="
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
| `polyline` | `"_p~iF~ps\|U_ulLn..."` | **Encoded polyline** (Google algorithm, precision 5). The route. |
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
                             │  skip if failed/no polyline, or below
                             │  MIN_DISTANCE_M (50) / MIN_DURATION_S (30)
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
- `sport_type` and `hide_from_home` aren't accepted by the uploads endpoint — set them with a `PUT /activities/{id}` once the upload has processed.
- **Strava's public API cannot delete activities** (`DELETE` returns 401). Below-threshold junk that predates the filter must be removed from the Strava website/app; the threshold just stops new junk being uploaded.

### Trivial-trip threshold

Unlock-then-relock attempts show up as ~0 m / few-second trips. `sync.py` skips any trip under `MIN_DISTANCE_M` (50 m) **or** `MIN_DURATION_S` (30 s). `reconcile.py plan` lists already-uploaded activities that fall under the current threshold so they can be pruned by hand.

---

## Why it's manual (one session per account)

Lime enforces **one active session per account**. A new login anywhere — the phone app *or* this tool — invalidates the previous token. This was confirmed directly: log the tool in (token works ✅), then log back into the app, and the tool's token immediately returns `401`. A stable `X-Device-Token` makes no difference; the limit is per-account, not per-device.

That kills any always-on/automated design, because of a circular dependency: **you can't take a Lime ride without the app, and using the app invalidates the sync's token.** So a background job can never hold a usable session between rides.

**The workflow that does work — on-demand batch sync:**

1. Ride normally; the app stays logged in.
2. When you want to sync, run `python3 sync.py upload --all`. It does the SMS login (which logs the app out), pulls **all new rides with full route/map**, uploads them, and dedupes via `seen-trips.json`.
3. Re-login the app next time you ride.

Cost: one app re-login per sync session. You keep the maps; it's just not hands-off.

### Dead end: the Cloudflare Worker (`worker/`)

`worker/` contains a full TypeScript port (cron + KV token store + Strava OAuth refresh + Resend email alerts). **It was deployed, worked twice, then abandoned** — kept here as a documented dead end. Two independent reasons it can't host this:

1. **One session per account** (above) — fatal on its own.
2. Lime's API is behind **Cloudflare bot protection that intermittently 403-blocks Worker egress IPs** (`server: cloudflare` challenge page). The same token/headers work fine from a residential IP.

If Lime ever dropped the one-session rule, the Worker would still be at the mercy of #2.

---

## Files in this repo

**The tool:**

| Script | Purpose |
|---|---|
| `sync.py` | The sync. `sync.py upload [--all]` = SMS login → pull rides → GPX → Strava. `sync.py gpx` builds GPX only (no upload). `sync.py strava-auth` does the one-time Strava OAuth. |
| `reconcile.py` | Retitle already-uploaded activities to the current template; list below-threshold junk to prune. |

**Reverse-engineering probes** (read-only; used to map the API):

| Script | Purpose |
|---|---|
| `probe.py` | Interactive SMS login → save token → fetch history → dump a trip's detail |
| `discover.py` | Reuse saved token, brute-probe candidate history endpoints |
| `detail.py` | Find the trip-detail endpoint + scan for route/distance fields |
| `device_login.py` | The one-session experiment: log in as a distinct device, then check if the app kills the token |

**`worker/`** — the abandoned Cloudflare Worker port (see [dead end](#dead-end-the-cloudflare-worker-worker)).

Tokens and raw responses land in `probe-output/`, `seen-trips.json`, `strava-tokens.json` (**all gitignored** — they contain your JWT, Strava tokens, and personal data).

---

## Setup (one-time)

1. Create a personal Strava API app at [strava.com/settings/api](https://www.strava.com/settings/api) (callback domain `localhost`).
2. `cp .env.example .env` and fill in `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` (`.env` is gitignored; `sync.py` loads it automatically). Real env vars still override `.env` if set.
3. `python3 sync.py strava-auth` → authorize in browser, paste the code. Saves `strava-tokens.json`.

Then, whenever you want to sync: `python3 sync.py upload --all`.

---

## Notes / findings

- **Token lifetime:** the `expires_at` JWT claim (120 s) is not enforced; sessions really end on a competing login. See [Why it's manual](#why-its-manual-one-session-per-account).
- **Rate limits:** bursts of requests from one IP trip Lime's Cloudflare bot protection (403). Space calls out; the on-demand tool's volume is fine.
- **Backfill:** `next_cursor` pages reliably back through full history (52 trips on the test account).
- **Scooter vs bike:** only bike trips verified so far; scooter `trip_summary` shape/polyline unconfirmed.
```
