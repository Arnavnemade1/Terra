import { useMemo, useState } from 'react';
import { ArrowUpRight, CloudRain, Droplets, Gauge, Satellite, Thermometer, Waves, Wind } from 'lucide-react';
import type { Hazard } from './Search';
import { EVENT_STYLE, ALERT_COLOR } from './liveLayers';
import { periodMinutes, subPoint } from './geo';
import type { Pass, Sat } from './fleet';
import { prettyDate, safeUrl } from './types';

export type Conditions = { source: string; source_url?: string; note: string; unavailable: string[]; acquisitions: { date: string; rain_72h_mm: number | null; soil_moisture_m3m3: number | null; soil_layer: string; temperature_c: number | null; wind_kmh: number | null; relative_humidity_pct: number | null; model?: string }[] };
export type Profile = { points: { lon: number; lat: number; distance_km: number; elevation_m: number | null; radar: Record<string, number | null> }[]; layers: { id: string; title: string; units: string }[]; terrain_source: string; notes: string[]; status: string };

const n = (v: unknown, d = 1) => typeof v === 'number' && Number.isFinite(v) ? v.toLocaleString('en', { maximumFractionDigits: d, minimumFractionDigits: d }).replace(/^-/, '−') : '—';
const capital = (s?: string | null) => s ? s.charAt(0).toUpperCase() + s.slice(1) : '';

// ---------------------------------------------------------------- layers
export type LayerRowSpec = { id: string; name: string; sub?: string; thumb?: string; swatch?: React.ReactNode; on: boolean; value?: number; max?: number; disabled?: boolean; note?: string };
export function LayersList({ rows, onToggle, onValue }: { rows: LayerRowSpec[]; onToggle: (id: string, on: boolean) => void; onValue: (id: string, v: number) => void }) {
  return <ul className="layer-list">{rows.map(r => <li key={r.id} className={`${r.on ? 'on' : ''} ${r.disabled ? 'disabled' : ''}`}>
    <label className="check"><input type="checkbox" checked={r.on} disabled={r.disabled} onChange={e => onToggle(r.id, e.target.checked)} /><span /></label>
    <span className="thumb">{r.thumb ? <img src={r.thumb} alt="" loading="lazy" /> : r.swatch}</span>
    <span className="layer-text"><b>{r.name}</b>{(r.note || r.sub) && <small>{r.note || r.sub}</small>}</span>
    {r.value !== undefined && <span className="layer-value">
      <input type="range" min={0} max={r.max ?? 1} step={0.05} value={r.value} disabled={r.disabled || !r.on} onChange={e => onValue(r.id, Number(e.target.value))} aria-label={`${r.name} ${r.max && r.max > 1 ? 'exaggeration' : 'opacity'}`} />
      <em>{Math.round(r.value * 100)}%</em></span>}
  </li>)}</ul>;
}
export const Swatch = {
  ring: (c: string) => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><circle cx="15" cy="15" r="7" fill="none" stroke={c} strokeWidth="2" /></svg>,
  diamond: (c: string) => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><rect x="10" y="10" width="10" height="10" fill={c} transform="rotate(45 15 15)" /></svg>,
  heat: (c: string) => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><circle cx="11" cy="12" r="5" fill={c} opacity=".35" /><circle cx="11" cy="12" r="2" fill={c} /><circle cx="20" cy="19" r="4" fill={c} opacity=".35" /><circle cx="20" cy="19" r="1.6" fill={c} /></svg>,
  outline: (c: string) => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><path d="M6 9 L22 5 L25 21 L9 25 Z" fill={c} fillOpacity=".15" stroke={c} strokeWidth="1.4" /></svg>,
  patches: () => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><rect x="4" y="6" width="11" height="8" fill="none" stroke="#f0a35e" strokeWidth="1.6" /><rect x="15" y="16" width="10" height="8" fill="none" stroke="#7fb0e0" strokeWidth="1.6" /><circle cx="9.5" cy="10" r="2" fill="#fff5e6" /></svg>,
  contours: () => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><g fill="none" stroke="#c8b98f" strokeWidth="1.1"><path d="M3 22 C9 16 15 23 27 15" /><path d="M5 15 C10 10 16 16 25 9" opacity=".7" /><path d="M9 8 C13 5 17 9 22 5" opacity=".5" /></g></svg>,
  cover: () => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#cdb994" /><path d="M0 0 H17 V12 H8 V30 H0 Z" fill="#4f5f37" /><path d="M17 18 H30 V30 H17 Z" fill="#b9a35f" /><path d="M22 0 H30 V8 H22 Z" fill="#4d6f86" /></svg>,
  trees: () => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><path d="M9 6 L14 20 H4 Z" fill="#5b6b3c" /><path d="M20 9 L25 23 H15 Z" fill="#4a5832" /><ellipse cx="12" cy="24" rx="3" ry="2" fill="#77713f" /></svg>,
  peak: () => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><path d="M15 8 L23 22 H7 Z" fill="#f8f4ea" stroke="#16130e" strokeWidth="1.5" strokeLinejoin="round" /></svg>,
  road: () => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><path d="M4 26 C10 18 12 12 26 4" fill="none" stroke="#e9e3d2" strokeWidth="2" /></svg>,
  text: (t: string) => <svg viewBox="0 0 30 30"><rect width="30" height="30" fill="#1c1d19" /><text x="15" y="19" textAnchor="middle" fontSize="11" fill="#d8d4c8" fontFamily="Archivo, sans-serif">{t}</text></svg>,
};

// ---------------------------------------------------------------- cards
export function Card({ title, sub, action, children }: { title: string; sub?: string; action?: React.ReactNode; children: React.ReactNode }) {
  return <section className="card-block"><header><b>{title}</b>{sub && <small>{sub}</small>}{action}</header>{children}</section>;
}
export function KeyValues({ rows }: { rows: [string, React.ReactNode][] }) {
  return <dl className="kv">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}

/** Modelled weather at each radar acquisition, beside the measured water-like area. */
export function ConditionsGrid({ value, waterBefore, waterAfter, pending }: { value?: Conditions; waterBefore?: number | null; waterAfter?: number | null; pending?: boolean }) {
  const a = value?.acquisitions || [];
  if (!a.length) return <p className="card-note">{pending ? 'Loading weather for the radar dates…' : 'Weather at the radar dates appears once an investigation has run.'}</p>;
  const pair = (k: keyof Conditions['acquisitions'][number], d: number) => a.length > 1 ? <>{n(a[0][k], d)}<i>→</i>{n(a[1][k], d)}</> : <>{n(a[0][k], d)}</>;
  const wet = a.some(x => (x.rain_72h_mm ?? 0) >= 10);
  const soilShift = a.length > 1 && a[0].soil_moisture_m3m3 !== null && a[1].soil_moisture_m3m3 !== null ? Math.abs(a[1].soil_moisture_m3m3! - a[0].soil_moisture_m3m3!) : 0;
  const cells: [React.ReactNode, string, React.ReactNode, string, boolean][] = [
    [<CloudRain size={15} />, 'Rain, prior 72 h', pair('rain_72h_mm', 1), 'mm', wet],
    [<Droplets size={15} />, `Soil moisture (${a[0].soil_layer})`, pair('soil_moisture_m3m3', 2), 'm³/m³', soilShift >= 0.05],
    [<Thermometer size={15} />, 'Air temperature', pair('temperature_c', 0), '°C', false],
    [<Wind size={15} />, 'Wind at 10 m', pair('wind_kmh', 0), 'km/h', a.some(x => (x.wind_kmh ?? 0) >= 25)],
    [<Gauge size={15} />, 'Relative humidity', pair('relative_humidity_pct', 0), '%', false],
    [<Waves size={15} />, 'Water-like area (measured)', waterBefore !== undefined && waterBefore !== null ? <>{n(waterBefore, 2)}{waterAfter !== undefined && waterAfter !== null && <><i>→</i>{n(waterAfter, 2)}</>}</> : '—', 'km²', false],
  ];
  return <>
    <div className="cond-grid">{cells.map(([icon, label, value, unit, alert], i) => <div key={i} className={alert ? 'alert' : ''}><span className="cond-icon">{icon}</span><span><small>{label}</small><b>{value} <em>{unit}</em></b></span></div>)}</div>
    <p className="card-note">{a.length > 1 ? `Before ${prettyDate(a[0].date, true)} → after ${prettyDate(a[1].date, true)}. ` : ''}{wet || soilShift >= 0.05 ? 'Moisture differs between the dates and can change radar brightness on its own. ' : ''}Modelled at the area centre. <a href={value?.source_url || 'https://open-meteo.com/en/docs/historical-weather-api'} target="_blank" rel="noreferrer">Open-Meteo</a></p>
  </>;
}

// ---------------------------------------------------------------- hazards
const GROUPS: [string, string][] = [['volcanoes', 'Volcanoes'], ['severeStorms', 'Tropical cyclones and storms'], ['floods', 'Floods'], ['drought', 'Drought'], ['earthquakes', 'Earthquakes, M4.5+'], ['wildfires', 'Wildfires'], ['seaLakeIce', 'Sea and lake ice']];
export const hazardColor = (h: { category: string }) => EVENT_STYLE[h.category]?.color || '#cfcabd';

export function HazardsPanel({ hazards, state, selected, onSelect, onInvestigate }: { hazards: Hazard[]; state: string; selected: Hazard | null; onSelect: (h: Hazard) => void; onInvestigate: (h: Hazard) => void }) {
  const [open, setOpen] = useState<Record<string, boolean>>({ volcanoes: true, severeStorms: true, floods: true, drought: false, earthquakes: true, wildfires: false, seaLakeIce: false, other: false });
  const groups = useMemo(() => {
    const known = new Set(GROUPS.map(g => g[0]));
    const all = [...GROUPS, ['other', 'Other reports'] as [string, string]];
    return all.map(([id, label]) => ({ id, label, items: hazards.filter(h => id === 'other' ? !known.has(h.category) : h.category === id) })).filter(g => g.items.length);
  }, [hazards]);
  const strongest = hazards.filter(h => h.category === 'earthquakes').map(h => Number(/M(\d+(\.\d+)?)/.exec(h.title)?.[1] || 0)).sort((a, b) => b - a)[0];
  const alerts = hazards.filter(h => h.alert === 'red' || h.alert === 'orange').length;
  return <div className="mode-panel">
    <header className="mode-head"><b>Hazards</b><span>· {hazards.length} reported</span><span className={`live ${state === 'offline' ? 'off' : ''}`}>● {state === 'loading' ? 'UPDATING' : state === 'offline' ? 'OFFLINE' : 'LIVE'}</span></header>
    <div className="kpis">
      <div><b>{alerts}</b><small>orange or red alerts</small></div>
      <div><b>{hazards.filter(h => h.category === 'volcanoes').length}</b><small>volcanoes</small></div>
      <div><b>{hazards.filter(h => h.category === 'severeStorms').length}</b><small>cyclones</small></div>
      <div><b>{strongest ? `M${strongest.toFixed(1)}` : '—'}</b><small>largest quake, 7 d</small></div>
    </div>
    <div className="mode-scroll">{groups.map(g => <div key={g.id} className="group">
      <button className="group-head" onClick={() => setOpen(o => ({ ...o, [g.id]: !o[g.id] }))} aria-expanded={open[g.id] !== false}><span>{g.label}</span><em>{g.items.length}</em></button>
      {open[g.id] !== false && <ul>{g.items.slice(0, 40).map(h => <li key={h.id}><button className={selected?.id === h.id ? 'on' : ''} onClick={() => onSelect(h)}>
        <i className="glyph-dot" style={{ background: hazardColor(h) }} />
        <span className="row-text"><b>{h.title}</b><small>{h.alert && h.alert !== 'green' ? <em style={{ color: ALERT_COLOR[h.alert] }}>● {capital(h.alert)} alert</em> : <em className="muted">● {h.provider || 'Reported'}</em>}{h.severity ? ` · ${h.severity}` : ''}</small></span>
        <time>{h.date ? prettyDate(h.date, true) : ''}</time>
      </button></li>)}</ul>}
    </div>)}
    {state === 'offline' && <p className="card-note">Hazard sources are unavailable. Try again shortly.</p>}</div>
    {selected && <section className="detail">
      <header><b>{selected.title}</b>{selected.alert && <span className="pill" style={{ color: ALERT_COLOR[selected.alert] || undefined }}>● {capital(selected.alert)}</span>}</header>
      <KeyValues rows={[['Type', selected.category_title], ['Reported', selected.date ? prettyDate(selected.date) : '—'], ...(selected.severity ? [['Severity', selected.severity] as [string, string]] : []),
        ...(selected.country ? [['Country', selected.country] as [string, string]] : []), ['Location', `${selected.lat.toFixed(3)}, ${selected.lon.toFixed(3)}`], ['Source', selected.provider || '—']]} />
      {selected.summary && <p className="card-note">{selected.summary}</p>}
      <div className="detail-actions"><button className="primary" onClick={() => onInvestigate(selected)}>Investigate the ground here</button>
        {safeUrl(selected.source) && <a className="secondary" href={safeUrl(selected.source)} target="_blank" rel="noreferrer">Report <ArrowUpRight size={12} /></a>}</div>
    </section>}
  </div>;
}

// ---------------------------------------------------------------- satellites
export function SatellitesPanel({ sats, passes, now, selected, onSelect, onFly, onSensor, source, nisarNext, error }: {
  sats: Sat[]; passes: Pass[]; now: number; selected: Sat | null; onSelect: (s: Sat) => void; onFly: (s: Sat) => void; onSensor: (s: Sat) => void; source: string; nisarNext: Pass | null; error: string;
}) {
  const [all, setAll] = useState(false);
  const shown = all ? sats : sats.filter(s => s.key);
  const groups = useMemo(() => {
    const order: string[] = []; for (const s of shown) if (!order.includes(s.group)) order.push(s.group);
    const rank = (g: string) => g === 'Radar' ? 0 : g === 'Optical land imaging' ? 1 : g.startsWith('Other') ? 9 : 2;
    return order.sort((a, b) => rank(a) - rank(b)).map(g => ({ group: g, items: shown.filter(s => s.group === g).sort((a, b) => (a.norad === 65053 ? -1 : b.norad === 65053 ? 1 : a.name.localeCompare(b.name))) }));
  }, [shown]);
  const pos = selected ? subPoint(selected.orbit, new Date(now)) : null;
  const next = selected ? (selected.norad === 65053 && nisarNext ? nisarNext : passes.find(p => p.norad === selected.norad && p.at >= now)) : null;
  const until = (t: number) => { const m = Math.max(0, Math.round((t - now) / 60000)); return m < 60 ? `${m} min` : m < 2880 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 1440)} d ${Math.round((m % 1440) / 60)} h`; };
  return <div className="mode-panel">
    <header className="mode-head"><b>Satellites</b><span>· {sats.filter(s => s.key).length} key missions · {sats.length} tracked</span><span className="live">● LIVE</span></header>
    <div className="kpis">
      <div><b>{nisarNext ? until(nisarNext.at) : '—'}</b><small>until NISAR passes your area</small></div>
      <div><b>{passes.length}</b><small>passes over area, 48 h</small></div>
      <div><b>{sats.filter(s => s.group === 'Radar').length}</b><small>radar satellites</small></div>
      <div><b>{sats.filter(s => s.group === 'Optical land imaging').length}</b><small>optical imagers</small></div>
    </div>
    <div className="mode-scroll">{error && <p className="card-note">{error}</p>}
      {groups.map(g => <div key={g.group} className="group"><div className="group-head static"><span>{g.group}</span><em>{g.items.length}</em></div>
        <ul>{g.items.slice(0, all ? 400 : 40).map(s => { const p = subPoint(s.orbit, new Date(now)); const np = passes.find(x => x.norad === s.norad && x.at >= now);
          return <li key={s.norad}><button className={selected?.norad === s.norad ? 'on' : ''} onClick={() => onSelect(s)}>
            <Satellite size={14} className={s.norad === 65053 ? 'accent' : ''} />
            <span className="row-text"><b>{s.name}</b><small>{np ? <em className="ok">● Over your area in {until(np.at)}</em> : <em className="muted">● {p ? `${Math.abs(p.lat).toFixed(0)}°${p.lat >= 0 ? 'N' : 'S'} ${Math.abs(p.lon).toFixed(0)}°${p.lon >= 0 ? 'E' : 'W'}` : 'Position unavailable'}</em>}</small></span>
            <time>{p ? `${Math.round(p.altKm)} km` : ''}</time>
          </button></li>; })}</ul></div>)}
      <button className="link more-link" onClick={() => setAll(v => !v)}>{all ? 'Show key missions only' : `Show all ${sats.length} Earth-resources satellites`}</button>
    </div>
    {selected && pos && <section className="detail">
      <header><b>{selected.name}</b><span className="pill">{selected.group}</span></header>
      <div className="stat-row"><div><b>{Math.round(pos.altKm)}</b><small>altitude, km</small></div><div><b>{pos.speedKms.toFixed(2)}</b><small>speed, km/s</small></div><div><b>{periodMinutes(selected.orbit).toFixed(1)}</b><small>orbit, min</small></div></div>
      <KeyValues rows={[['Next pass over area', next ? `${new Date(next.at).toISOString().slice(0, 16).replace('T', ' ')} UTC · ${next.km} km · in ${until(next.at)}` : selected.norad === 65053 ? 'None in the next 13 days' : 'None within 48 h'],
        ['Inclination', `${(selected.orbit.rec.inclo * 180 / Math.PI).toFixed(2)}°`], ['Sub-point', `${pos.lat.toFixed(2)}°, ${pos.lon.toFixed(2)}°`], ['NORAD', String(selected.norad)],
        ['Elements', selected.epoch ? `epoch ${prettyDate(selected.epoch)}` : 'CelesTrak GP']]} />
      <p className="card-note">A pass means the ground track comes within about {selected.reachKm} km, a rough reach for this kind of sensor. It is not a confirmed acquisition.</p>
      <div className="detail-actions"><button className="primary" onClick={() => onSensor(selected)}>Sensor view at next pass</button><button className="secondary" onClick={() => onFly(selected)}>Fly to satellite</button></div>
    </section>}
    <p className="mode-foot">{source}</p>
  </div>;
}
