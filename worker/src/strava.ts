// Strava client: OAuth token refresh (persisted to KV) + GPX upload.
import type { Env } from "./index";

const API = "https://www.strava.com/api/v3";
const OAUTH = "https://www.strava.com/oauth/token";

interface StravaTokens {
  access_token: string;
  refresh_token: string;
  expires_at: number; // unix seconds
}

/**
 * Return a valid access token, refreshing if within 60s of expiry.
 * Strava may rotate the refresh_token, so the new set is written back to KV.
 */
export async function getAccessToken(env: Env): Promise<string> {
  const raw = await env.LIME_SYNC.get("strava_tokens");
  if (!raw) throw new Error("no strava_tokens in KV — seed it (see worker/README.md)");
  const tok: StravaTokens = JSON.parse(raw);
  if (tok.expires_at > Date.now() / 1000 + 60) return tok.access_token;

  const res = await fetch(OAUTH, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: env.STRAVA_CLIENT_ID,
      client_secret: env.STRAVA_CLIENT_SECRET,
      grant_type: "refresh_token",
      refresh_token: tok.refresh_token,
    }),
  });
  if (!res.ok) throw new Error(`Strava token refresh failed: HTTP ${res.status}`);
  const d: any = await res.json();
  const next: StravaTokens = {
    access_token: d.access_token,
    refresh_token: d.refresh_token,
    expires_at: d.expires_at,
  };
  await env.LIME_SYNC.put("strava_tokens", JSON.stringify(next));
  return next.access_token;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Upload a GPX and wait for processing. Returns the new activity id.
 * Sets sport_type + hide_from_home afterwards (the uploads endpoint can't).
 */
export async function uploadGpx(
  access: string,
  gpx: string,
  name: string,
  description: string,
  tripId: string,
  sportType: string,
  hideFromHome: boolean,
): Promise<number> {
  const auth = { Authorization: `Bearer ${access}` };
  const form = new FormData();
  form.append("file", new Blob([gpx], { type: "application/gpx+xml" }), `${tripId}.gpx`);
  form.append("data_type", "gpx");
  form.append("name", name);
  form.append("description", description);
  form.append("external_id", `lime-${tripId}`);

  const res = await fetch(`${API}/uploads`, { method: "POST", headers: auth, body: form });
  if (res.status === 429) throw new Error("Strava rate limit (429)");
  if (!res.ok) throw new Error(`Strava upload rejected: HTTP ${res.status} ${await res.text()}`);
  const uploadId = (await res.json<any>()).id;

  for (let i = 0; i < 20; i++) {
    await sleep(2000);
    const s = await fetch(`${API}/uploads/${uploadId}`, { headers: auth });
    const d: any = await s.json();
    if (d.error) throw new Error(`Strava processing error: ${d.error}`);
    if (d.activity_id) {
      await fetch(`${API}/activities/${d.activity_id}`, {
        method: "PUT",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({ sport_type: sportType, hide_from_home: hideFromHome }),
      });
      return d.activity_id;
    }
  }
  throw new Error("timed out waiting for Strava to process upload");
}
