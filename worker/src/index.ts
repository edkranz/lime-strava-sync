// Worker entry: cron (scheduled) + a protected manual trigger (fetch).
import { runSync } from "./sync";

export interface Env {
  LIME_SYNC: KVNamespace;
  STRAVA_CLIENT_ID: string;
  STRAVA_CLIENT_SECRET: string;
  TRIGGER_KEY: string;
  ENVIRONMENT?: string;
  MIN_DISTANCE_M?: string;
  MIN_DURATION_S?: string;
  SPORT_TYPE?: string;
  REPO_URL?: string;
  RECENT_FEED_HOURS?: string; // rides newer than this post to feed; older hide (default 48)
  MAX_PAGES?: string;
  JITTER_MIN_MIN?: string; // min minutes between real runs (default 25)
  JITTER_MAX_MIN?: string; // max minutes between real runs (default 40)
  // Email alerts via Resend (free tier). No-op until configured. See worker/README.md.
  RESEND_API_KEY?: string; // secret; from https://resend.com
  ALERT_TO?: string; // recipient (your Resend signup email works with the test sender)
  ALERT_FROM?: string; // sender, e.g. onboarding@resend.dev, or you@verified-domain
  ALERT_MIN_INTERVAL_H?: string; // dedupe window per condition (default 6h)
}

const numEnv = (v: string | undefined, dflt: number): number => {
  const n = v ? Number(v) : NaN;
  return Number.isFinite(n) ? n : dflt;
};

export default {
  // Cron fires every 5 min, but we only actually run once a random 25-40 min
  // has elapsed (jitter, tracked in KV) — steady enough to catch new rides,
  // irregular enough not to look like a metronome to Lime's bot protection.
  async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil((async () => {
      const now = Date.now();
      const nextAt = Number((await env.LIME_SYNC.get("next_run_at")) || 0);
      if (now < nextAt) {
        console.log(`[CRON] skip — next run in ~${Math.round((nextAt - now) / 60000)} min`);
        return;
      }
      const minM = numEnv(env.JITTER_MIN_MIN, 25);
      const maxM = numEnv(env.JITTER_MAX_MIN, 40);
      const jitterMs = (minM + Math.random() * (maxM - minM)) * 60000;
      // Set the next window BEFORE running, so a failure doesn't cause hammering.
      await env.LIME_SYNC.put("next_run_at", String(now + jitterMs));
      console.log(`[CRON] running; next in ~${Math.round(jitterMs / 60000)} min`);
      await runSync(env);
    })());
  },

  // Manual trigger for testing + a status endpoint. Both require ?key=TRIGGER_KEY.
  //   GET /sync?key=...            run a real sync now
  //   GET /sync?key=...&dry=1      build GPX but don't write to Strava
  //   GET /sync?key=...&all=1      scan more history pages (backfill)
  //   GET /sync?key=...&force=1    re-upload even if already seen
  //   GET /status?key=...          last run summary
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // Don't expose the local scheduled-sim endpoint in production.
    if (url.pathname === "/__scheduled" && env.ENVIRONMENT === "production") {
      return new Response("Not Found", { status: 404 });
    }

    const json = (obj: unknown, status = 200) =>
      new Response(JSON.stringify(obj, null, 2), {
        status,
        headers: { "Content-Type": "application/json" },
      });

    if (url.pathname === "/sync" || url.pathname === "/status") {
      if (url.searchParams.get("key") !== env.TRIGGER_KEY) {
        return json({ error: "unauthorized" }, 401);
      }
      if (url.pathname === "/status") {
        const last = await env.LIME_SYNC.get("last_run");
        return json(last ? JSON.parse(last) : { note: "no run yet" });
      }
      const result = await runSync(env, {
        dry: url.searchParams.get("dry") === "1",
        force: url.searchParams.get("force") === "1",
        maxPages: url.searchParams.get("all") === "1" ? 50 : undefined,
      });
      return json(result, result.ok ? 200 : 500);
    }

    return new Response(
      "lime-strava-sync worker. Use GET /sync?key=… (add &dry=1 to test) or /status?key=…\n",
      { headers: { "Content-Type": "text/plain" } },
    );
  },
};
