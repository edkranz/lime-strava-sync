// Runtime config, read from the KV key "config" on every run. Edit that JSON
// in the Cloudflare dashboard (Storage & Databases → KV → LIME_SYNC → config)
// to change behavior with NO redeploy. Anything omitted falls back to DEFAULTS.
import type { Env } from "./index";

export interface Config {
  pollMinMinutes: number; // min gap between real runs (jitter)
  pollMaxMinutes: number; // max gap between real runs
  sportType: string; // Strava sport_type, e.g. EBikeRide / Ride
  visibility: string; // Strava visibility: everyone | followers_only | only_me
  feed: "show" | "hide" | "recent"; // feed posting policy
  recentFeedHours: number; // when feed="recent": rides newer than this post
  minDistanceM: number; // skip trips shorter than this
  minDurationS: number; // skip trips briefer than this
  maxPages: number; // history pages scanned per run
  titleTemplate: string; // placeholders: {tod} {Tod}
  descriptionTemplate: string; // placeholders: {stats} {distance} {duration} {calories} {co2} {cost} {repo}
  repoUrl: string; // {repo} in the description
}

export const DEFAULTS: Config = {
  pollMinMinutes: 25,
  pollMaxMinutes: 40,
  sportType: "EBikeRide",
  visibility: "everyone",
  feed: "hide",
  recentFeedHours: 48,
  minDistanceM: 50,
  minDurationS: 30,
  maxPages: 3,
  titleTemplate: "{Tod} Lime bike ride \u{1F34B}‍\u{1F7E9}",
  descriptionTemplate: "{stats}\npowered by {repo}",
  repoUrl: "https://github.com/edkranz/lime-strava-sync",
};

/** DEFAULTS overlaid with the KV `config` JSON. Bad JSON → defaults (logged). */
export async function getConfig(env: Env): Promise<Config> {
  let overrides: Partial<Config> = {};
  try {
    const raw = await env.LIME_SYNC.get("config");
    if (raw) overrides = JSON.parse(raw);
  } catch (e: any) {
    console.error(`[CONFIG] bad KV config, using defaults: ${e?.message ?? e}`);
  }
  return { ...DEFAULTS, ...overrides };
}

/** Decide hide_from_home for one ride given the feed policy. */
export function shouldHide(cfg: Config, completedAtISO: string): boolean {
  if (cfg.feed === "hide") return true;
  if (cfg.feed === "show") return false;
  const ageH = (Date.now() - new Date(completedAtISO).getTime()) / 3_600_000;
  return ageH > cfg.recentFeedHours;
}
