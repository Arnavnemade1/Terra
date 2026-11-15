import { useEffect, useMemo, useState } from 'react';
import { distanceKm, orbiter, subPoint, type Orbiter, type Tle } from './geo';
import { api } from './types';

/** Roles in display order, with an approximate half-swath: how close the ground track must come for the sensor to see the area. */
export const ROLES: { role: string; label: string; reachKm: number }[] = [
  { role: 'Radar imaging', label: 'Radar', reachKm: 350 },
  { role: 'Optical land imaging', label: 'Optical land imaging', reachKm: 145 },
  { role: 'Multispectral Earth observations', label: 'MODIS · Terra and Aqua', reachKm: 1150 },
  { role: 'Weather and environmental observations', label: 'Weather and fire (VIIRS)', reachKm: 1500 },
  { role: 'Ocean and land observations', label: 'Ocean and land colour', reachKm: 630 },
  { role: 'Soil moisture', label: 'Soil moisture', reachKm: 500 },
  { role: 'Water-surface altimetry', label: 'Water height', reachKm: 60 },
  { role: 'Laser altimetry', label: 'Ice and canopy height', reachKm: 15 },
];
export type Sat = { norad: number; name: string; role: string; group: string; reachKm: number; orbit: Orbiter; epoch?: string; key: boolean };
export type Pass = { norad: number; name: string; group: string; at: number; km: number };

const title = (name: string) => name.replace(/\s*\(.*?\)\s*/g, ' ').replace(/\s+/g, ' ').trim()
  .replace(/\b([A-Z])([A-Z]+)\b/g, (_, a: string, b: string) => a + b.toLowerCase()).replace(/\bNisar\b/, 'NISAR').replace(/\bSwot\b/, 'SWOT').replace(/\bSmap\b/, 'SMAP').replace(/\bAlos\b/, 'ALOS').replace(/\bNoaa\b/, 'NOAA').replace(/\bIcesat\b/, 'ICESat').replace(/\bSaocom\b/, 'SAOCOM').replace(/\bCsg\b/, 'CSG');

/** Orbital elements for NISAR and the Earth-resources fleet, from the server's feed. */
export function useFleet() {
  const [data, setData] = useState<{ nisar: Tle & { role?: string }; fleet: (Tle & { role?: string; element_epoch?: string })[]; source: string; fleet_source?: string; fetched_at: string; status: string }>();
  const [error, setError] = useState('');
  useEffect(() => {
    const c = new AbortController();
    void api<NonNullable<typeof data>>('/feeds/satellites', { signal: c.signal }).then(setData).catch(() => { if (!c.signal.aborted) setError('Orbital elements are unavailable right now.'); });
    return () => c.abort();
  }, []);
  const sats = useMemo<Sat[]>(() => {
    if (!data) return [];
    const all = [{ ...data.nisar, role: 'Radar imaging' }, ...data.fleet];
    return all.flatMap(t => {
      const o = orbiter(t); if (!o) return [];
      const r = ROLES.find(x => x.role === t.role);
      return [{ norad: t.norad, name: title(t.name), role: t.role || 'Earth observation', group: r?.label || 'Other Earth observation', reachKm: r?.reachKm ?? 100, orbit: o,
        epoch: (t as { element_epoch?: string }).element_epoch, key: Boolean(r) }];
    });
  }, [data]);
  return { sats, source: data ? [data.source, data.fleet_source].filter(Boolean).join(' · ') : '', fetchedAt: data?.fetched_at, status: data?.status, error };
}

/**
 * Closest approaches of each satellite's ground track to a point, within its reach, over the next `hours`.
 * A coarse search (3-minute steps, ~30 per orbit) finds each local minimum of the distance, which is then refined to
 * the second by golden-section search: about half the propagations of a 1-minute scan, and more precise.
 */
export function predictPasses(sats: Sat[], lat: number, lon: number, from: number, hours = 48, stepSec = 180): Pass[] {
  const out: Pass[] = [];
  for (const s of sats) {
    const dist = (t: number) => { const p = subPoint(s.orbit, new Date(t)); return p ? distanceKm(lat, lon, p.lat, p.lon) : Infinity; };
    let prev = Infinity, prevPrev = Infinity, prevT = from;
    for (let t = from; t <= from + hours * 3600_000; t += stepSec * 1000) {
      const d = dist(t);
      if (prev < prevPrev && prev <= d) {
        // The minimum lies within one step either side of prevT.
        let a = prevT - stepSec * 1000, b = prevT + stepSec * 1000; const g = 0.381966;
        let x1 = a + g * (b - a), x2 = b - g * (b - a), f1 = dist(x1), f2 = dist(x2);
        while (b - a > 2000) { if (f1 < f2) { b = x2; x2 = x1; f2 = f1; x1 = a + g * (b - a); f1 = dist(x1); } else { a = x1; x1 = x2; f1 = f2; x2 = b - g * (b - a); f2 = dist(x2); } }
        const at = f1 < f2 ? x1 : x2, km = Math.min(f1, f2);
        if (km <= s.reachKm && at >= from) out.push({ norad: s.norad, name: s.name, group: s.group, at: Math.round(at), km: Math.round(km) });
      }
      prevPrev = prev; prev = d; prevT = t;
    }
  }
  return out.sort((a, b) => a.at - b.at);
}

/**
 * The same, computed one satellite per browser task so a 48-hour prediction for the whole fleet never blocks the page
 * (it used to hold the main thread for ~0.3 s whenever the area or the 10-minute window changed).
 */
export function usePasses(sats: Sat[], lat: number, lon: number, from: number, enabled: boolean): Pass[] {
  const [passes, setPasses] = useState<Pass[]>([]);
  useEffect(() => {
    if (!enabled || !sats.length) { setPasses([]); return; }
    let cancelled = false, i = 0; const out: Pass[] = [];
    let handle = 0;
    const step = () => {
      if (cancelled) return;
      const t0 = performance.now();
      while (i < sats.length && performance.now() - t0 < 8) out.push(...predictPasses([sats[i++]], lat, lon, from));
      if (i < sats.length) handle = window.setTimeout(step, 0);
      else setPasses(out.sort((a, b) => a.at - b.at));
    };
    handle = window.setTimeout(step, 0);
    return () => { cancelled = true; clearTimeout(handle); };
  }, [sats, lat, lon, from, enabled]);
  return passes;
}

/**
 * Imaging geometry for side-looking radars whose swath is known. NISAR's L-band SAR looks left and images the ground
 * at 33–47° incidence, which from about 747 km is roughly 420–680 km from its ground track; a close overpass alone does
 * not mean the radar sees the area.
 */
export const SWATH: Record<number, { nearKm: number; farKm: number; side: 'left' | 'right'; label: string }> = {
  65053: { nearKm: 420, farKm: 680, side: 'left', label: 'L-SAR 33–47° incidence, left-looking' },
};
const bearing = (lat1: number, lon1: number, lat2: number, lon2: number) => {
  const r = Math.PI / 180, p1 = lat1 * r, p2 = lat2 * r, dl = (lon2 - lon1) * r;
  return (Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)) / r + 360) % 360;
};
/** Which side of the ground track a point lies on at time `t` (negative: left), and how far from the sub-satellite point. */
export function crossTrack(s: Sat, lat: number, lon: number, t: number): { side: number; km: number } | null {
  const p = subPoint(s.orbit, new Date(t)), q = subPoint(s.orbit, new Date(t + 10_000)); if (!p || !q) return null;
  return { side: ((bearing(p.lat, p.lon, lat, lon) - bearing(p.lat, p.lon, q.lat, q.lon) + 540) % 360) - 180, km: distanceKm(lat, lon, p.lat, p.lon) };
}
/** The next time the point falls inside the satellite's imaging swath (closest approach of that pass), or null. */
export function swathPass(s: Sat, lat: number, lon: number, from: number, days = 13): number | null {
  const sw = SWATH[s.norad]; if (!sw) return null;
  let prev = Infinity, prevPrev = Infinity, prevT = from;
  for (let t = from; t <= from + days * 86_400_000;) {
    const p = subPoint(s.orbit, new Date(t)); if (!p) { t += 60_000; continue; }
    const d = distanceKm(lat, lon, p.lat, p.lon);
    if (prev < prevPrev && prev <= d && prev >= sw.nearKm && prev <= sw.farKm) {
      const x = crossTrack(s, lat, lon, prevT);
      if (x && (x.side < 0) === (sw.side === 'left')) return prevT;
    }
    prevPrev = prev; prev = d; prevT = t;
    // Far from the area, skip ahead in proportion to the distance (ground speed is about 7 km/s).
    t += d > sw.farKm + 1500 ? Math.min(600_000, (d - sw.farKm - 1000) / 7 * 1000) : 15_000;
    if (d > sw.farKm + 1500) { prev = Infinity; prevPrev = Infinity; }
  }
  return null;
}

/** Altitude and distance-to-area series for one satellite. */
export function trackSeries(s: Sat, lat: number, lon: number, from: number, minutes: number, stepSec: number) {
  const x: number[] = [], alt: (number | null)[] = [], dist: (number | null)[] = [];
  for (let t = from; t <= from + minutes * 60_000; t += stepSec * 1000) {
    const p = subPoint(s.orbit, new Date(t)); x.push(t);
    alt.push(p ? p.altKm : null); dist.push(p ? distanceKm(lat, lon, p.lat, p.lon) : null);
  }
  return { x, alt, dist };
}
