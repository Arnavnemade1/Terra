import { useEffect, useReducer, useState } from 'react';
import { Maximize2, Video } from 'lucide-react';
import { ft, kt, type DroneState, type DroneStore, type FleetMessage, type Patch, type Place } from './drones';
import { dm } from './DroneFeed';
import { KeyValues } from './Panels';
import type { Bounds } from './types';

const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const fmt = (n: number, d = 0) => Number.isFinite(n) ? n.toLocaleString('en', { maximumFractionDigits: d, minimumFractionDigits: d }) : '—';
const ago = (t: number | null | undefined) => { if (!t) return '—'; const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };
const clock = (s: number) => `${Math.floor(s / 3600) ? `${Math.floor(s / 3600)}:` : ''}${String(Math.floor(s / 60) % 60).padStart(Math.floor(s / 3600) ? 2 : 1, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const PROTOCOL: Record<string, string> = { mavlink: 'MAVLink', 'terra-sim': 'Terra virtual fleet', 'dji-cloud': 'DJI Cloud API', other: 'Other' };

export function AircraftIcon({ aircraft }: { aircraft: DroneState['aircraft'] }) {
  return aircraft === 'fixed-wing'
    ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M4 10.5h16M8.5 19h7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>
    : <svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><path d="M8 8l8 8M16 8l-8 8" /><circle cx="6" cy="6" r="2.6" /><circle cx="18" cy="6" r="2.6" /><circle cx="6" cy="18" r="2.6" /><circle cx="18" cy="18" r="2.6" /></g><rect x="10" y="10" width="4" height="4" rx="1" fill="currentColor" /></svg>;
}

function sender(m: FleetMessage) {
  if (m.sender === 'coordinator') return { who: 'Coordinator', cls: 'coord' };
  if (m.sender === 'operator') return { who: 'You', cls: 'op' };
  if (m.sender.startsWith('agent:')) return { who: `${m.sender.slice(6)} agent`, cls: 'agent' };
  if (m.sender.startsWith('drone:')) return { who: m.sender.slice(6), cls: 'drone' };
  return { who: m.sender, cls: '' };
}

type Option = { key: string; label: string; task: (d: DroneState) => { kind: string; label: string; lon?: number; lat?: number; radius_m?: number; agl_m?: number; bbox?: Bounds } };

/** The fleet, the selected aircraft, its task and airspace, people's tasks, and the agents' shared log. */
export default function DronesPanel({ store, selected, onSelect, onCamera, onFull, patches, places, regionName, regionBox, onMonitorHere }: {
  store: DroneStore; selected: string | null; onSelect: (id: string) => void; onCamera: (id: string) => void; onFull: (id: string) => void; patches: Patch[]; places: Place[];
  regionName: string; regionBox: Bounds; onMonitorHere: () => void;
}) {
  const [, tick] = useReducer((x: number) => x + 1, 0);
  const [sent, setSent] = useState('');
  useEffect(() => { const id = setInterval(tick, 500); const off = store.subscribe(tick); return () => { clearInterval(id); off(); }; }, [store]);
  const states = store.states(); const info = store.info;
  const s = states.find(d => d.id === selected) || null;
  const area = store.area(); const centre: [number, number] | null = area ? [(area[0] + area[2]) / 2, (area[1] + area[3]) / 2] : null;
  const airborne = states.filter(d => d.status === 'airborne').length;
  const real = states.filter(d => d.source === 'real');
  const box = (c: [number, number], half: number): Bounds => { const dx = half / (111_320 * Math.cos(c[1] * Math.PI / 180)), dy = half / 110_574; return [c[0] - dx, c[1] - dy, c[0] + dx, c[1] + dy]; };
  const summit = places.filter(p => p.kind === 'peak' || p.kind === 'volcano').sort((a, b) => (b.elevation_m ?? 0) - (a.elevation_m ?? 0))[0];
  const options: Option[] = [
    ...patches.slice(0, 4).map(p => ({ key: `orbit-${p.rank}`, label: `Orbit ${p.label}`, task: (d: DroneState) => ({ kind: 'orbit', label: `Orbit ${p.label}`, lon: p.centre[1], lat: p.centre[0], radius_m: d.aircraft === 'fixed-wing' ? 650 : 240, agl_m: d.aircraft === 'fixed-wing' ? 430 : 190 }) })),
    ...(summit ? [{ key: 'orbit-summit', label: `Orbit ${summit.name}`, task: (d: DroneState) => ({ kind: 'orbit', label: `Orbit ${summit.name}`, lon: summit.lon, lat: summit.lat, radius_m: d.aircraft === 'fixed-wing' ? 700 : 300, agl_m: d.aircraft === 'fixed-wing' ? 500 : 250 }) }] : []),
    ...(patches[0] ? [{ key: 'survey-1', label: `Survey around ${patches[0].label}`, task: () => ({ kind: 'survey', label: `Survey around ${patches[0].label}`, bbox: box([patches[0].centre[1], patches[0].centre[0]], 700), agl_m: 200 }) }] : []),
    ...(area ? [{ key: 'perimeter', label: 'Map the perimeter', task: (d: DroneState) => ({ kind: 'perimeter', label: 'Map the perimeter', bbox: area, agl_m: d.aircraft === 'fixed-wing' ? 430 : 250 }) }] : []),
    ...(centre ? [{ key: 'relay', label: 'Relay over the centre', task: () => ({ kind: 'relay', label: 'Radio relay', lon: centre[0], lat: centre[1], radius_m: 80, agl_m: 300 }) },
      { key: 'hold', label: 'Hold over the centre', task: () => ({ kind: 'hold', label: 'Hold', lon: centre[0], lat: centre[1], agl_m: 150 }) }] : []),
    { key: 'return', label: 'Return to launch', task: () => ({ kind: 'return', label: 'Return to launch' }) },
  ];
  async function assign(d: DroneState, key: string) {
    const o = options.find(x => x.key === key); if (!o) return;
    try {
      const r = await store.assign(d.id, o.task(d));
      setSent(r.advisory ? `Sent to ${d.callsign}'s ground station; its pilot decides whether to fly it.` : `${d.callsign}: ${o.label}. The coordinator holds it for 15 minutes.`);
    } catch (e) { setSent(e instanceof Error ? e.message : 'The task could not be sent.'); }
  }
  const op = s?.operation; const t = s?.task;

  return <div className="drones-panel">
    <header className="fleet-head"><b>Drone Fleet</b><span className="quiet">· {airborne} of {states.length} airborne</span><span className="grow" />
      {real.length ? <span className="live-chip"><i />LIVE</span> : states.length ? <span className="sim-chip" title="Terra's virtual aircraft, connected through the same protocol as real drones">SIM</span> : null}</header>
    <div className="area-line"><span>Monitoring <b>{info.area?.name ?? 'no area yet'}</b></span>
      {(!info.area || info.area.bbox.some((v, i) => Math.abs(v - regionBox[i]) > 1e-6)) && <button className="mini-btn" onClick={onMonitorHere}
        title="Virtual aircraft are taken there; real aircraft far away stand by">Monitor {regionName.length > 22 ? `${regionName.slice(0, 22)}…` : regionName}</button>}</div>
    <div className={`coord-chip ${info.coordinator.running ? 'on' : ''}`} title="The durable coordinator agent (a Temporal workflow) and one agent per aircraft">
      <i />{info.coordinator.running ? <>Coordinator · round {info.coordinator.summary.round ?? '—'} · {info.coordinator.summary.text}</> : info.area ? 'Coordinator starting or offline: the investigation service may be down.' : 'Waiting for a monitoring area.'}</div>
    {!states.length && <p className="card-note fleet-empty">{info.connections ? 'No aircraft connected yet. The dev stack starts a virtual fleet; real aircraft join through scripts/drone_bridge.py.' : 'Drone connections are off: set TERRA_DRONE_TOKEN on the server.'}</p>}
    <ul className="fleet-list">{states.map(d => <li key={d.id}><button className={d.id === selected ? 'on' : ''} onClick={() => onSelect(d.id)}>
      <span className="fleet-icon"><AircraftIcon aircraft={d.aircraft} /></span>
      <span className="fleet-name"><b>{d.callsign}</b> {d.name}<small>{d.mission.task}</small></span>
      <span className="fleet-status"><em className={d.status === 'stale' || d.status === 'lost' ? 'warn' : d.status === 'landed' ? 'quiet' : ''}>{d.status === 'lost' ? 'No signal' : d.status === 'stale' ? 'Delayed' : d.mission.status}</em><small className="num">{d.status === 'landed' ? 'on ground' : `${fmt(Math.round(ft(d.aglM) / 10) * 10)} ft`}</small></span>
      <span className="fleet-battery"><i><b style={{ width: `${Number.isFinite(d.batteryPct) ? d.batteryPct : 0}%`, background: d.batteryPct < 30 ? 'var(--warn)' : 'var(--ok)' }} /></i><small className="num">{Number.isFinite(d.batteryPct) ? `${Math.round(d.batteryPct)}%` : '—'}</small></span>
    </button></li>)}</ul>
    {s && <section className="fleet-detail">
      <header><b>{s.callsign}</b> <span>{s.name}</span><span className="grow" /><span className="status-chip">{s.mission.status}</span></header>
      <div className="fleet-stats">
        <div><small>Altitude</small><b className="num">{fmt(Math.round(ft(s.aglM) / 10) * 10)}</b><em>ft AGL</em></div>
        <div><small>Ground speed</small><b className="num">{fmt(kt(s.speedMps))}</b><em>kn</em></div>
        <div><small>Heading</small><b className="num">{Math.round(s.headingDeg) % 360}°</b><em>{COMPASS[Math.round(s.headingDeg / 45) % 8]}</em></div>
        <div><small>Battery</small><b className="num">{Number.isFinite(s.batteryPct) ? `${Math.round(s.batteryPct)}%` : '—'}</b><em>{Number.isFinite(s.minutesLeft) ? `~${Math.max(0, Math.round(s.minutesLeft))} min to reserve` : 'not reported'}</em></div>
        <div><small>Link</small><b className="num">{Number.isFinite(s.linkDbm) ? `${Math.round(s.linkDbm)}`.replace('-', '−') : '—'}</b><em>dBm</em></div>
        <div><small>Connected</small><b className="num">{clock(s.flightS)}</b><em>{s.ageS !== undefined ? `last report ${fmt(s.ageS, 1)} s` : ''}</em></div>
      </div>
      <KeyValues rows={[
        ['Task', t ? <span key="t">{t.label} <em className={`task-status ${t.status}`}>{t.status}</em></span> : '—'],
        ['Why', t?.reason || (t?.created_by === 'operator' ? 'Assigned in the workspace' : '—')],
        ['Airspace', op ? `${fmt(op.alt_lower_m)}–${fmt(op.alt_upper_m)} m above ground · ${op.geometry.type === 'circle' ? `${fmt(op.geometry.radius_m)} m radius` : 'survey box'} · ${op.state}` : '—'],
        ['Agent', s.agent ? `${s.agent.summary.text ?? ''} · ${fmt(s.agent.age_s, 0)} s ago` : 'Starting…'],
        ['Aircraft', `${s.aircraft === 'fixed-wing' ? 'Fixed-wing' : 'Quadcopter'} · ${s.source === 'real' ? 'real' : 'virtual'}`],
        ['Link', `${PROTOCOL[s.protocol] ?? s.protocol}${s.autopilot ? ` · ${s.autopilot}` : ''}`],
        ['Sensors', s.sensor], ['Position', <span className="num" key="p">{dm(s.lat, s.lon)}</span>], ['Gimbal', `${Math.round(s.gimbal.headingDeg)}° · ${Math.round(s.gimbal.pitchDeg)}°`],
      ]} />
      <div className="detail-actions">
        <button className="primary" onClick={() => onFull(s.id)}><Maximize2 size={13} /> Full camera view</button>
        <button className="secondary" onClick={() => onCamera(s.id)}><Video size={13} /> Camera</button>
      </div>
      <label className="task-pick"><span>Give a task</span>
        <select value="" onChange={e => { void assign(s, e.target.value); }}><option value="" disabled>Choose…</option>{options.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}</select></label>
      {sent && <p className="card-note">{sent}</p>}
    </section>}
    <section className="fleet-log">
      <header><b>Coordination log</b><small>agents, aircraft and people, newest first</small></header>
      {info.messages.length ? <ol>{info.messages.slice(0, 30).map(m => { const w = sender(m); return <li key={m.id}>
        <span className={`who ${w.cls}`}>{w.who}</span><span className="what">{m.body.text ?? m.kind}</span><time>{ago(m.t)}</time></li>; })}</ol>
        : <p className="card-note">Decisions, hand-offs, warnings and acknowledgements appear here as the agents work.</p>}
    </section>
    <section className="fleet-connect">
      <b>Real drones</b>
      <p>{info.connections ? `${real.length ? `${real.length} connected. ` : ''}A ground station joins with ` : 'Set TERRA_DRONE_TOKEN on the server, then a ground station joins with '}
        <code>scripts/drone_bridge.py --mavlink …</code> (PX4 or ArduPilot). It registers the aircraft, reports twice a second and acknowledges each task.
        Tasks are advisory: orbit, hold, relay and return reach the autopilot only with <code>--allow-commands</code>, and the pilot in command decides.
        Reserved airspace is available as ASTM F3548-shaped operational intents at <code>/api/fleet/operations</code>.</p>
    </section>
  </div>;
}
