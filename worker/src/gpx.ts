// Polyline decoding + GPX building + the Strava title/description template.
// Ported from the Python sync.py so the Worker produces identical output.

export interface Trip {
  id: string;
  polyline: string;
  distanceM: number;
  durationS: number;
  startedAt: string; // ISO 8601 UTC
  completedAt: string;
  calories?: number | null;
  co2G?: number | null;
  costCents?: number | null;
  currency?: string | null;
}

/** Google encoded polyline (precision 5) -> [ [lat, lng], ... ]. */
export function decodePolyline(s: string): [number, number][] {
  const pts: [number, number][] = [];
  let i = 0,
    lat = 0,
    lng = 0;
  while (i < s.length) {
    for (let axis = 0; axis < 2; axis++) {
      let shift = 0,
        result = 0,
        b: number;
      do {
        b = s.charCodeAt(i++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      const delta = result & 1 ? ~(result >> 1) : result >> 1;
      if (axis === 0) lat += delta;
      else lng += delta;
    }
    pts.push([lat / 1e5, lng / 1e5]);
  }
  return pts;
}

function timeOfDay(hour: number): string {
  if (hour >= 5 && hour < 12) return "morning";
  if (hour >= 12 && hour < 14) return "midday";
  if (hour >= 14 && hour < 18) return "afternoon";
  return "evening";
}

// Sydney (UTC+10, no DST handling — matches the Python which used the host tz).
// Kept configurable so the template's time-of-day label is correct per rider.
const TZ_OFFSET_HOURS = 10;

function localHour(iso: string): number {
  const d = new Date(iso);
  return (d.getUTCHours() + TZ_OFFSET_HOURS) % 24;
}

// Individual stat strings (only those present), used by {stats} and the
// per-field placeholders in the description template.
function stats(trip: Trip) {
  const distance = trip.distanceM ? `${(trip.distanceM / 1000).toFixed(2)} km` : "";
  const duration = trip.durationS
    ? `${Math.floor(trip.durationS / 60)}m${String(trip.durationS % 60).padStart(2, "0")}s`
    : "";
  const calories = trip.calories ? `${trip.calories} kcal` : "";
  const co2 = trip.co2G ? `${trip.co2G}g CO2 saved` : "";
  const cost = trip.costCents ? `${trip.currency} $${(trip.costCents / 100).toFixed(2)}` : "";
  return { distance, duration, calories, co2, cost };
}

/** Fill a title template. Placeholders: {tod} {Tod} (morning/evening/…). */
export function activityName(trip: Trip, template: string): string {
  const tod = timeOfDay(localHour(trip.startedAt));
  return template
    .replaceAll("{tod}", tod)
    .replaceAll("{Tod}", tod.charAt(0).toUpperCase() + tod.slice(1));
}

/**
 * Fill a description template. Placeholders: {stats} (comma-joined present
 * stats), {distance} {duration} {calories} {co2} {cost} {repo}.
 */
export function activityDescription(trip: Trip, template: string, repoUrl: string): string {
  const s = stats(trip);
  const joined = [s.distance, s.duration, s.calories, s.co2, s.cost].filter(Boolean).join(", ");
  return template
    .replaceAll("{stats}", joined)
    .replaceAll("{distance}", s.distance)
    .replaceAll("{duration}", s.duration)
    .replaceAll("{calories}", s.calories)
    .replaceAll("{co2}", s.co2)
    .replaceAll("{cost}", s.cost)
    .replaceAll("{repo}", repoUrl);
}

function isoZ(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Build a GPX 1.1 track. Timestamps are synthesized evenly across the ride window. */
export function buildGpx(trip: Trip, name: string): string {
  const pts = decodePolyline(trip.polyline);
  if (pts.length < 2) throw new Error(`polyline too short (${pts.length} pts)`);
  const start = new Date(trip.startedAt);
  const end = new Date(trip.completedAt);
  const spanMs = end.getTime() - start.getTime();
  const n = pts.length;

  const trkpts = pts
    .map(([lat, lng], idx) => {
      const t = new Date(start.getTime() + (spanMs * idx) / (n - 1));
      return `      <trkpt lat="${lat.toFixed(6)}" lon="${lng.toFixed(6)}"><time>${isoZ(t)}</time></trkpt>`;
    })
    .join("\n");

  return `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="lime-strava-sync" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><time>${isoZ(start)}</time></metadata>
  <trk>
    <name>${name}</name>
    <trkseg>
${trkpts}
    </trkseg>
  </trk>
</gpx>
`;
}
