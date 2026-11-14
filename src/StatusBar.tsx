import { useEffect, useState } from 'react';
import { Copy, Check } from 'lucide-react';
import type { Telemetry } from './Globe';
import type { FeedState } from './liveLayers';
import { decimal, dmsPretty, duration, fmtKm, mgrs, scaleBar, sunElevation } from './geo';

type Props = { t: Telemetry | null; feeds: Record<string, FeedState>; onSources: () => void };
const FORMATS = ['decimal', 'dms', 'mgrs'] as const;
type Format = typeof FORMATS[number];
const FORMAT_NAMES: Record<Format, string> = { decimal: 'Decimal degrees', dms: 'Degrees, minutes, seconds', mgrs: 'MGRS grid reference' };

function readFormat(): Format {
  try { const v = localStorage.getItem('terra.coordFormat'); return FORMATS.includes(v as Format) ? v as Format : 'decimal'; } catch { return 'decimal'; }
}

export default function StatusBar({ t, feeds, onSources }: Props) {
  const [now, setNow] = useState(Date.now());
  const [format, setFormat] = useState<Format>(readFormat);
  const [copied, setCopied] = useState(false);
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  useEffect(() => { try { localStorage.setItem('terra.coordFormat', format); } catch { /* per-viewer convenience only */ } }, [format]);
  const lat = t?.centerLat ?? null, lon = t?.centerLon ?? null;
  const has = lat !== null && lon !== null;
  const text = !has ? 'Point at the Earth' : format === 'decimal' ? decimal(lat, lon) : format === 'dms' ? dmsPretty(lat, lon) : mgrs(lat, lon);
  const scale = t?.metersPerPixel ? scaleBar(t.metersPerPixel) : null;
  const states = Object.values(feeds);
  const live = states.filter(f => f.state === 'live').length;
  const degraded = states.some(f => f.state === 'offline' || f.state === 'stale');
  const copy = () => { if (!has) return; void navigator.clipboard?.writeText(format === 'decimal' ? `${lat!.toFixed(5)}, ${lon!.toFixed(5)}` : text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200); }); };
  return <footer className="status" aria-label="Map status">
    <div className="status-scale" aria-label={scale ? `Scale bar ${scale.label}` : 'Scale unavailable'}>
      {scale ? <><span className="scale-bar" style={{ width: `${scale.px}px` }} /><span>{scale.label}</span></> : <span className="dim">Scale —</span>}
    </div>
    <span className="coord">
      <button className="status-btn num" onClick={() => setFormat(f => FORMATS[(FORMATS.indexOf(f) + 1) % FORMATS.length])} title={`${FORMAT_NAMES[format]} at the centre of the view. Click to switch format.`}>
        {text}
      </button>
      {has && <button className="copy" onClick={copy} aria-label="Copy coordinates" title="Copy coordinates">{copied ? <Check size={12} /> : <Copy size={12} />}</button>}
    </span>
    <span className="status-item hide-sm"><span className="dim">Eye</span> {t ? fmtKm(t.alt).toLowerCase() : '—'}</span>
    {has && <span className="status-item hide-sm"><span className="dim">Sun</span> {sunElevation(lat!, lon!, new Date(now)).toFixed(0)}°</span>}
    <span className="status-spacer" />
    {t?.nisar && <span className="status-item hide-md"><b className="accent">NISAR</b> {Math.abs(t.nisar.lat).toFixed(1)}° {t.nisar.lat >= 0 ? 'N' : 'S'}, {Math.abs(t.nisar.lon).toFixed(1)}° {t.nisar.lon >= 0 ? 'E' : 'W'}
      {t.approach && <span className="dim"> · over your area in {duration(t.approach.at.getTime() - now)}</span>}</span>}
    <button className="status-item status-btn" onClick={onSources} title="Data sources">
      <i className={`led ${degraded ? 'warn' : live ? 'ok' : ''}`} />{live} of {Math.max(states.length, 5)} sources live
    </button>
    <span className="status-item num">{new Date(now).toISOString().slice(11, 19)} <span className="dim">UTC</span></span>
  </footer>;
}
