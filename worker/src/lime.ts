// Lime rider API client. See ../../README.md for endpoint docs.
import type { Env } from "./index";
import type { Trip } from "./gpx";

const BASE = "https://web-production.lime.bike";
const HEADERS = {
  "User-Agent": "Lime/3.149.0 (iPhone; iOS 17.5; Scale/3.00)",
  Accept: "application/json",
  "Accept-Language": "en-US",
  Platform: "iOS",
  "App-Version": "3.149.0",
};

/** Thrown when Lime rejects the token (401). The signal that a manual re-auth is needed. */
export class LimeAuthError extends Error {}
/** Thrown when a trip should be skipped (failed ride, no polyline, below threshold). */
export class SkipTrip extends Error {}

async function limeGet(token: string, path: string): Promise<any> {
  const res = await fetch(BASE + path, {
    headers: { ...HEADERS, Authorization: `Bearer ${token}` },
  });
  if (res.status === 401) throw new LimeAuthError(`Lime 401 on ${path}`);
  if (!res.ok) {
    const body = (await res.text()).slice(0, 300);
    const server = res.headers.get("server") ?? "?";
    throw new Error(`Lime GET ${path} -> HTTP ${res.status} [server=${server}] ${body}`);
  }
  return res.json();
}

/** Trip ids, newest first, from user_transactions. Stops at maxPages. */
export async function listTripIds(token: string, maxPages: number): Promise<string[]> {
  const ids: string[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < maxPages; page++) {
    const q = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    const data: any = await limeGet(token, `/api/rider/v1/views/user_transactions${q}`);
    for (const group of data.user_transactions ?? []) {
      for (const v of group.group_values ?? []) {
        if (v.object_type === "Trip") ids.push(v.id);
      }
    }
    cursor = data.next_cursor ?? null;
    if (!cursor) break;
  }
  return ids;
}

/** Fetch one trip's detail, normalized. Throws SkipTrip for non-syncable trips. */
export async function getTrip(
  token: string,
  tripId: string,
  minDistanceM: number,
  minDurationS: number,
): Promise<Trip> {
  const q = `transaction_id=${encodeURIComponent(tripId)}`;
  const data: any = await limeGet(token, `/api/rider/v1/views/trip_summary?${q}`);
  const t = data?.data?.attributes?.trip?.attributes;
  if (!t) throw new SkipTrip(`${tripId}: no trip data`);
  if (!t.polyline) throw new SkipTrip(`${tripId}: no polyline (status=${t.status})`);
  const dist = t.distance_meters ?? 0;
  const dur = t.duration_seconds ?? 0;
  if (dist < minDistanceM || dur < minDurationS)
    throw new SkipTrip(`${tripId}: below threshold (${dist} m, ${dur} s)`);
  return {
    id: tripId,
    polyline: t.polyline,
    distanceM: dist,
    durationS: dur,
    startedAt: t.started_at,
    completedAt: t.completed_at,
    calories: t.calories_burned,
    co2G: t.co2_saved_grams,
    costCents: t.cost_amount_cents,
    currency: t.currency,
  };
}

export async function getLimeToken(env: Env): Promise<string> {
  const token = await env.LIME_SYNC.get("lime_token");
  if (!token) throw new LimeAuthError("no lime_token in KV — seed it (see worker/README.md)");
  return token;
}
