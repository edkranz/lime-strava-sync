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

export function activityName(trip: Trip): string {
  const tod = timeOfDay(localHour(trip.startedAt));
  const cap = tod.charAt(0).toUpperCase() + tod.slice(1);
  return `${cap} Lime bike ride \u{1F34B}‍\u{1F7E9}`;
}

export function activityDescription(trip: Trip, repoUrl: string): string {
  const bits: string[] = [];
  if (trip.distanceM) bits.push(`${(trip.distanceM / 1000).toFixed(2)} km`);
  if (trip.durationS) {
    const m = Math.floor(trip.durationS / 60);
    const s = String(trip.durationS % 60).padStart(2, "0");
    bits.push(`${m}m${s}s`);
  }
  if (trip.calories) bits.push(`${trip.calories} kcal`);
  if (trip.co2G) bits.push(`${trip.co2G}g CO2 saved`);
  if (trip.costCents) bits.push(`${trip.currency} $${(trip.costCents / 100).toFixed(2)}`);
  return bits.join(", ") + `\npowered by ${repoUrl}`;
}

function isoZ(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Build a GPX 1.1 track. Timestamps are synthesized evenly across the ride window. */
export function buildGpx(trip: Trip): string {
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
    <name>${activityName(trip)}</name>
    <trkseg>
${trkpts}
    </trkseg>
  </trk>
</gpx>
`;
}
