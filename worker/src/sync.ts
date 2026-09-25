// Core sync: Lime history -> new trips -> Strava. Shared by cron and manual trigger.
import type { Env } from "./index";
import { getLimeToken, listTripIds, getTrip, SkipTrip, LimeAuthError } from "./lime";
import { getAccessToken, uploadGpx } from "./strava";
import { buildGpx, activityName, activityDescription } from "./gpx";

export interface SyncOptions {
  dry?: boolean; // build GPX but don't touch Strava
  force?: boolean; // re-upload even if already seen
  maxPages?: number; // history pages to scan
}

export interface SyncResult {
  ok: boolean;
  uploaded: { tripId: string; activityId?: number; name: string }[];
  skipped: { tripId: string; reason: string }[];
  seenAlready: number;
  error?: string;
  authError?: boolean;
}

function num(v: string | undefined, dflt: number): number {
  const n = v ? Number(v) : NaN;
  return Number.isFinite(n) ? n : dflt;
}

// Transient failures we deliberately DON'T alert on: the CF bot-block and rate
// limits are expected at 30-min cadence and self-heal on the next tick.
function isTransient(error: string): boolean {
  return /403|cloudflare|429|rate limit/i.test(error);
}

/**
 * Email an alert, but only for conditions worth waking someone: a dead Lime
 * token (needs manual re-auth) or a genuine non-transient error. Deduped per
 * condition so a persistent problem doesn't send one email every 30 minutes.
 * No-op (log only) until the EMAIL binding + ALERT_TO are configured.
 */
async function maybeAlert(env: Env, result: SyncResult): Promise<void> {
  let kind: string | null = null;
  if (result.authError) kind = "auth";
  else if (result.error && !isTransient(result.error)) kind = "error";
  if (!kind) return;

  const subject =
    kind === "auth"
      ? "⚠️ lime-strava-sync: Lime token expired — re-auth needed"
      : "⚠️ lime-strava-sync: sync error";
  const body =
    (kind === "auth"
      ? "The Lime session token was rejected (401). Re-auth by SMS and push a fresh token to KV:\n" +
        "  wrangler kv key put --binding LIME_SYNC --remote lime_token \"<new JWT>\"\n\n"
      : "") + `Error: ${result.error}\nAt: ${new Date().toISOString()}`;

  // Dedupe: skip if we already alerted this kind within the interval.
  const windowH = num(env.ALERT_MIN_INTERVAL_H, 6);
  const lastRaw = await env.LIME_SYNC.get("last_alert");
  if (lastRaw) {
    const last = JSON.parse(lastRaw);
    if (last.kind === kind && Date.now() - last.at < windowH * 3600_000) {
      console.log(`[ALERT] suppressed (${kind}, within ${windowH}h dedupe window)`);
      return;
    }
  }

  if (!env.RESEND_API_KEY || !env.ALERT_TO || !env.ALERT_FROM) {
    console.log(`[ALERT] would send "${subject}" but Resend not configured yet`);
    return;
  }
  try {
    const res = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.RESEND_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ from: env.ALERT_FROM, to: env.ALERT_TO, subject, text: body }),
    });
    if (!res.ok) throw new Error(`Resend HTTP ${res.status} ${await res.text()}`);
    await env.LIME_SYNC.put("last_alert", JSON.stringify({ kind, at: Date.now() }));
    console.log(`[ALERT] sent "${subject}" to ${env.ALERT_TO}`);
  } catch (e: any) {
    console.error(`[ALERT] send failed: ${e?.message ?? e}`);
  }
}

export async function runSync(env: Env, opts: SyncOptions = {}): Promise<SyncResult> {
  const result: SyncResult = { ok: false, uploaded: [], skipped: [], seenAlready: 0 };
  const minDistanceM = num(env.MIN_DISTANCE_M, 50);
  const minDurationS = num(env.MIN_DURATION_S, 30);
  const sportType = env.SPORT_TYPE || "EBikeRide";
  const repoUrl = env.REPO_URL || "https://github.com/edkranz/lime-strava-sync";
  // Rides finished within RECENT_FEED_HOURS post to the feed; older ones hide.
  const recentFeedH = num(env.RECENT_FEED_HOURS, 48);
  const maxPages = opts.maxPages ?? num(env.MAX_PAGES, 3);
  const started = Date.now();

  try {
    const limeToken = await getLimeToken(env);
    const ids = await listTripIds(limeToken, maxPages);
    console.log(`[SYNC] ${ids.length} trips in history (scanned ${maxPages} page(s))`);

    let access: string | null = null; // fetched lazily so a dry run needs no Strava

    for (const tripId of ids) {
      const seenKey = `seen:${tripId}`;
      if (!opts.force && (await env.LIME_SYNC.get(seenKey))) {
        result.seenAlready++;
        continue;
      }
      let trip;
      try {
        trip = await getTrip(limeToken, tripId, minDistanceM, minDurationS);
      } catch (e) {
        if (e instanceof SkipTrip) {
          result.skipped.push({ tripId, reason: e.message });
          console.log(`[SKIP] ${e.message}`);
          continue;
        }
        throw e;
      }

      const gpx = buildGpx(trip);
      const name = activityName(trip);
      const desc = activityDescription(trip, repoUrl);

      if (opts.dry) {
        console.log(`[DRY] would upload ${tripId} "${name}" (${(trip.distanceM / 1000).toFixed(2)} km)`);
        result.uploaded.push({ tripId, name });
        continue;
      }

      if (!access) access = await getAccessToken(env);
      const ageH = (Date.now() - new Date(trip.completedAt).getTime()) / 3_600_000;
      const hide = ageH > recentFeedH;
      const activityId = await uploadGpx(access, gpx, name, desc, tripId, sportType, hide);
      await env.LIME_SYNC.put(seenKey, "1");
      result.uploaded.push({ tripId, activityId, name });
      console.log(`[OK] ${tripId} -> https://www.strava.com/activities/${activityId}`);
    }

    result.ok = true;
  } catch (e: any) {
    result.error = e?.message ?? String(e);
    if (e instanceof LimeAuthError) {
      result.authError = true;
      console.error(`[LIME AUTH] ${result.error} — re-auth needed: push a fresh token to KV`);
    } else {
      console.error(`[SYNC ERROR] ${result.error}`);
    }
  }

  // status snapshot for the /status endpoint and log review
  await env.LIME_SYNC.put(
    "last_run",
    JSON.stringify({
      at: new Date().toISOString(),
      durationMs: Date.now() - started,
      uploaded: result.uploaded.length,
      skipped: result.skipped.length,
      seenAlready: result.seenAlready,
      ok: result.ok,
      authError: result.authError ?? false,
      error: result.error ?? null,
    }),
  );
  console.log(
    `[SYNC DONE] uploaded=${result.uploaded.length} skipped=${result.skipped.length} ` +
      `seen=${result.seenAlready} ok=${result.ok}${result.error ? " error=" + result.error : ""}`,
  );
  await maybeAlert(env, result);
  return result;
}
