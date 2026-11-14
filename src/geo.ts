import { forward as toMgrs, toPoint as fromMgrs } from 'mgrs';
import * as sat from 'satellite.js';

const DEG = Math.PI / 180;

export const pad = (n: number, width: number) => String(Math.floor(Math.abs(n))).padStart(width, '0');

/** 16°26'45.96"N 078°57'23.10"E */
/** Degrees, minutes and seconds rounded as a whole, so 19.4° reads 19°24′00″ and never 19°23′60″. */
function splitDms(v: number, decimals: number): [number, number, number] {
  const scale = 10 ** decimals;
  const total = Math.round(Math.abs(v) * 3600 * scale);
  const d = Math.floor(total / (3600 * scale)); const m = Math.floor((total % (3600 * scale)) / (60 * scale));
  return [d, m, (total % (60 * scale)) / scale];
}

export function dms(lat: number, lon: number): string {
  const part = (v: number, w: number, pos: string, neg: string) => {
    const [d, m, s] = splitDms(v, 2);
    return `${pad(d, w)}°${pad(m, 2)}'${s.toFixed(2).padStart(5, '0')}"${v >= 0 ? pos : neg}`;
  };
  return `${part(lat, 2, 'N', 'S')} ${part(lon, 3, 'E', 'W')}`;
}

/** MGRS at 10 m precision, spaced like a field readout: 18M VS 1234 5678 */
export function mgrs(lat: number, lon: number): string {
  if (lat < -80 || lat > 84) return 'POLAR · UPS';
  try {
    const raw = toMgrs([lon, lat], 4);
    const m = raw.match(/^(\d{1,2}[A-Z])([A-Z]{2})(\d{4})(\d{4})$/);
    return m ? `${m[1]} ${m[2]} ${m[3]} ${m[4]}` : raw;
  } catch { return '—'; }
}

export function utcStamp(d = new Date()): string {
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1, 2)}-${pad(d.getUTCDate(), 2)} ${pad(d.getUTCHours(), 2)}:${pad(d.getUTCMinutes(), 2)}:${pad(d.getUTCSeconds(), 2)}Z`;
}

/** Solar elevation (degrees) from the NOAA low-precision algorithm (±0.5°). */
export function sunElevation(lat: number, lon: number, date = new Date()): number {
  const jd = date.getTime() / 86400000 + 2440587.5; const n = jd - 2451545.0;
  const L = (280.46 + 0.9856474 * n) % 360; const g = ((357.528 + 0.9856003 * n) % 360) * DEG;
  const lambda = (L + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * DEG; const eps = (23.439 - 0.0000004 * n) * DEG;
  const dec = Math.asin(Math.sin(eps) * Math.sin(lambda));
  const ra = Math.atan2(Math.cos(eps) * Math.sin(lambda), Math.cos(lambda));
  const gmst = (18.697374558 + 24.06570982441908 * n) % 24;
  const ha = (gmst * 15 + lon) * DEG - ra;
  const el = Math.asin(Math.sin(lat * DEG) * Math.sin(dec) + Math.cos(lat * DEG) * Math.cos(dec) * Math.cos(ha));
  return el / DEG;
}

export function viewBand(heightM: number): string {
  if (heightM > 6_000_000) return 'GLOBAL';
  if (heightM > 1_500_000) return 'CONTINENTAL';
  if (heightM > 150_000) return 'REGIONAL';
  if (heightM > 8_000) return 'LOCAL';
  return 'SITE';
}

export function distanceKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const a = Math.sin((lat2 - lat1) * DEG / 2) ** 2 + Math.cos(lat1 * DEG) * Math.cos(lat2 * DEG) * Math.sin((lon2 - lon1) * DEG / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(a)));
}

export function ago(ms: number, now = Date.now()): string {
  const s = Math.max(0, (now - ms) / 1000);
  if (s < 3600) return `${Math.round(s / 60)}M AGO`;
  if (s < 86400) return `${Math.round(s / 3600)}H AGO`;
  return `${Math.round(s / 86400)}D AGO`;
}

export function fmtKm(m: number): string {
  if (m >= 10_000) return `${Math.round(m / 1000).toLocaleString('en')} KM`;
  if (m >= 1000) return `${(m / 1000).toFixed(1)} KM`;
  return `${Math.round(m)} M`;
}

// ---------- Orbits (SGP4 via satellite.js) ----------
export type Tle = { name: string; norad: number; line1: string; line2: string };
export type Orbiter = { tle: Tle; rec: sat.SatRec };
export type SubPoint = { lat: number; lon: number; altKm: number; speedKms: number };

export function orbiter(tle: Tle): Orbiter | null {
  try { const rec = sat.twoline2satrec(tle.line1, tle.line2); return rec.error ? null : { tle, rec }; } catch { return null; }
}

export function subPoint(o: Orbiter, date: Date): SubPoint | null {
  const pv = sat.propagate(o.rec, date);
  if (!pv || typeof pv.position === 'boolean' || !pv.position || typeof pv.velocity === 'boolean' || !pv.velocity) return null;
  const geo = sat.eciToGeodetic(pv.position, sat.gstime(date));
  const v = pv.velocity;
  const out = { lat: sat.degreesLat(geo.latitude), lon: sat.degreesLong(geo.longitude), altKm: geo.height, speedKms: Math.hypot(v.x, v.y, v.z) };
  return Object.values(out).every(Number.isFinite) && out.altKm > 80 ? out : null;
}

export function periodMinutes(o: Orbiter): number { return (2 * Math.PI) / o.rec.no; }

/** Ground track samples from `fromMin` to `toMin` minutes relative to `at`. */
export function groundTrack(o: Orbiter, at: Date, fromMin: number, toMin: number, stepSec = 40): SubPoint[] {
  const out: SubPoint[] = [];
  for (let t = fromMin * 60; t <= toMin * 60; t += stepSec) {
    const p = subPoint(o, new Date(at.getTime() + t * 1000)); if (p) out.push(p);
  }
  return out;
}

/**
 * Next time the sub-satellite point passes within `withinKm` of a target, scanning
 * up to `days` ahead. This is a geometric nadir approach, not a tasked acquisition.
 */
export function nextApproach(o: Orbiter, lat: number, lon: number, withinKm: number, from = new Date(), days = 13): { at: Date; km: number } | null {
  const end = from.getTime() + days * 86400000;
  let t = from.getTime();
  while (t < end) {
    const p = subPoint(o, new Date(t));
    if (p) {
      const d = distanceKm(p.lat, p.lon, lat, lon);
      if (d < withinKm) {
        let best = { at: new Date(t), km: d };
        for (let s = t; s < t + 600_000; s += 10_000) {
          const q = subPoint(o, new Date(s)); if (!q) continue;
          const dq = distanceKm(q.lat, q.lon, lat, lon); if (dq < best.km) best = { at: new Date(s), km: dq };
        }
        return best;
      }
      // Skip ahead in proportion to distance: ground speed is about 6.8 km/s.
      t += Math.max(20_000, Math.min(600_000, ((d - withinKm) / 7) * 1000 * 0.8));
    } else t += 60_000;
  }
  return null;
}

/** 5°24′02.8″ S   74°38′59.9″ W */
export function dmsPretty(lat: number, lon: number): string {
  const part = (v: number, pos: string, neg: string) => {
    const [d, m, s] = splitDms(v, 1);
    return `${d}°${pad(m, 2)}′${s.toFixed(1).padStart(4, '0')}″ ${v >= 0 ? pos : neg}`;
  };
  return `${part(lat, 'N', 'S')}  ${part(lon, 'E', 'W')}`;
}

/** A round scale-bar length near `targetPx` pixels: 1, 2 or 5 × 10ⁿ metres. */
export function scaleBar(metersPerPixel: number, targetPx = 96): { px: number; label: string } {
  const raw = metersPerPixel * targetPx; const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  const nice = [1, 2, 5, 10].map(f => f * exp).reduce((best, v) => Math.abs(v - raw) < Math.abs(best - raw) ? v : best);
  return { px: nice / metersPerPixel, label: nice >= 1000 ? `${(nice / 1000).toLocaleString('en')} km` : `${nice} m` };
}

/** Approximate area of a lon/lat box in km². */
export function boxAreaKm2(b: [number, number, number, number]): number {
  const lat = ((b[1] + b[3]) / 2) * DEG;
  return Math.abs((b[2] - b[0]) * 111.32 * Math.cos(lat) * (b[3] - b[1]) * 110.57);
}

export function duration(ms: number): string {
  const m = Math.max(0, Math.round(ms / 60000)); const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), r = m % 60;
  return d ? `${d} d ${h} h` : h ? `${h} h ${r} min` : `${r} min`;
}

export type ParsedCoordinate = { lat: number; lon: number; note?: string; format: string };

/**
 * Read coordinates the way people type them:
 *   19.43, -99.13 · 19.43 -99.13 · 19.43N 99.13W · 19.43° N, 99.13° W · N19.43 W99.13
 *   19°25'48"N 99°07'48"W · 19 25 48 N 99 7 48 W
 * Order is latitude first unless hemisphere letters say otherwise; an impossible latitude
 * with a valid longitude in first place is read as "longitude, latitude" and noted.
 */
export function parseCoordinates(input: string): ParsedCoordinate | null {
  const raw = input.trim();
  // Links copied from map sites: …/@19.4,-155.2,12z, ?q=19.4,-155.2, ?ll=… and geo:19.4,-155.2
  const link = raw.match(/(?:@|[?&](?:q|ll|query|center|mlat)=|^geo:)\s*(-?\d+(?:\.\d+)?)\s*(?:,|%2C|&mlon=)\s*(-?\d+(?:\.\d+)?)/i);
  if (link) {
    const lat = Number(link[1]), lon = Number(link[2]);
    if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat, lon, format: raw.startsWith('geo:') ? 'geo: link' : 'Map link' };
  }
  // MGRS / USNG grid references, e.g. 4QHG 1234 5678 or 33SWB1234567890
  const grid = raw.replace(/\s+/g, '').toUpperCase();
  if (/^\d{1,2}[C-HJ-NP-X][A-HJ-NP-Z]{2}(\d{2}|\d{4}|\d{6}|\d{8}|\d{10})$/.test(grid)) {
    try { const [lon, lat] = fromMgrs(grid); if (Number.isFinite(lat) && Number.isFinite(lon)) return { lat, lon, format: 'MGRS grid reference' }; } catch { /* not a valid grid square */ }
  }
  const text = raw.replace(/[′’']/g, "'").replace(/[″”"]/g, '"').replace(/−/g, '-').replace(/\s+/g, ' ');
  if (!/\d/.test(text) || /[a-df-mo-rt-vx-z]{2,}/i.test(text.replace(/\b[NSEW]\b/gi, ''))) return null;
  const part = String.raw`([NSEW])?\s*(-?\d+(?:\.\d+)?)\s*°?\s*(?:(\d+(?:\.\d+)?)\s*'?\s*(?:(\d+(?:\.\d+)?)\s*"?)?)?\s*([NSEW])?`;
  const m = text.match(new RegExp(`^${part}\\s*[,;/ ]\\s*${part}$`, 'i'));
  if (!m) return null;
  const read = (pre?: string, d?: string, mi?: string, se?: string, post?: string) => {
    let v = Math.abs(Number(d)) + (mi ? Number(mi) / 60 : 0) + (se ? Number(se) / 3600 : 0);
    const hemi = (pre || post || '').toUpperCase();
    if (String(d).startsWith('-') || hemi === 'S' || hemi === 'W') v = -v;
    return { v, hemi };
  };
  const a = read(m[1], m[2], m[3], m[4], m[5]);
  const b = read(m[6], m[7], m[8], m[9], m[10]);
  let lat = a.v, lon = b.v, note: string | undefined;
  if ('EW'.includes(a.hemi) && a.hemi && 'NS'.includes(b.hemi) && b.hemi) { lat = b.v; lon = a.v; }
  else if (!a.hemi && !b.hemi && Math.abs(a.v) > 90 && Math.abs(b.v) <= 90) { lat = b.v; lon = a.v; note = 'Read as longitude, latitude because the first number cannot be a latitude.'; }
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  const format = m[3] || m[8] ? 'Degrees, minutes, seconds' : (a.hemi || b.hemi) ? 'Decimal degrees with N/S/E/W' : 'Decimal degrees';
  return { lat, lon, note, format };
}

export function decimal(lat: number, lon: number, digits = 4): string {
  return `${Math.abs(lat).toFixed(digits)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(digits)}° ${lon >= 0 ? 'E' : 'W'}`;
}
