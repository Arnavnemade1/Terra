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

/** Closest approaches of each satellite's ground track to a point, within its reach, over the next `hours`. */
export function predictPasses(sats: Sat[], lat: number, lon: number, from: number, hours = 48, stepSec = 60): Pass[] {
  const out: Pass[] = [];
  for (const s of sats) {
    let prev = Infinity, prevPrev = Infinity, prevT = from;
    for (let t = from; t <= from + hours * 3600_000; t += stepSec * 1000) {
      const p = subPoint(s.orbit, new Date(t)); if (!p) continue;
      const d = distanceKm(lat, lon, p.lat, p.lon);
      if (prev < prevPrev && prev <= d && prev <= s.reachKm) out.push({ norad: s.norad, name: s.name, group: s.group, at: prevT, km: Math.round(prev) });
      prevPrev = prev; prev = d; prevT = t;
    }
  }
  return out.sort((a, b) => a.at - b.at);
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
