import { useCallback, useEffect, useReducer, useState } from 'react';
import { Copy, Maximize2, Plus, Video, X } from 'lucide-react';
import { ft, kt, type DroneState, type DroneStore, type EnrollRequest, type Enrollment, type FieldNote, type FleetMessage, type Patch, type Place, type Profile, type Source } from './drones';
import { dm } from './DroneFeed';
import { KeyValues } from './Panels';
import type { Bounds } from './types';

const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const fmt = (n: number, d = 0) => Number.isFinite(n) ? n.toLocaleString('en', { maximumFractionDigits: d, minimumFractionDigits: d }) : '—';
const ago = (t: number | null | undefined) => { if (!t) return '—'; const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };
const clock = (s: number) => `${Math.floor(s / 3600) ? `${Math.floor(s / 3600)}:` : ''}${String(Math.floor(s / 60) % 60).padStart(Math.floor(s / 3600) ? 2 : 1, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const PROTOCOL: Record<string, string> = { mavlink: 'MAVLink', 'terra-sim': 'Terra virtual fleet', 'dji-cloud': 'DJI Cloud API', other: 'Other' };
const SOURCE: Record<Source, { tag: string; long: string }> = {
  real: { tag: 'LIVE', long: 'real aircraft' },
  sitl: { tag: 'SITL', long: 'ArduPilot SITL: real flight firmware, simulated airframe' },
  virtual: { tag: 'SIM', long: "Terra's virtual autopilot" },
};
const PROFILES: { key: Profile; label: string; note: string }[] = [
  { key: 'change', label: 'Change watch', note: 'Orbit the patches where the radar measured change, then the summits; survey and map the perimeter.' },
  { key: 'thermal', label: 'Thermal watch', note: 'IR aircraft orbit the warmest ground in the latest cloud-free Landsat thermal scene.' },
  { key: 'search', label: 'Search', note: 'Four sectors, each flown as an expanding-square search from its centre at 90 m.' },
];
const NOTE_LABEL: Record<string, string> = { warm: 'Warm ground', change: 'Changed ground', station: 'On station', observation: 'Observation' };
const SENSORS = [['eo', 'EO'], ['ir', 'IR'], ['lidar', 'Lidar'], ['multispectral', 'Multispectral'], ['gas', 'Gas']] as const;

export function AircraftIcon({ aircraft }: { aircraft: DroneState['aircraft'] }) {
  return aircraft === 'fixed-wing'
    ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M4 10.5h16M8.5 19h7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>
    : <svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><path d="M8 8l8 8M16 8l-8 8" /><circle cx="6" cy="6" r="2.6" /><circle cx="18" cy="6" r="2.6" /><circle cx="6" cy="18" r="2.6" /><circle cx="18" cy="18" r="2.6" /></g><rect x="10" y="10" width="4" height="4" rx="1" fill="currentColor" /></svg>;
}

function sender(m: FleetMessage) {
  if (m.sender === 'coordinator') return { who: 'Coordinator', cls: 'coord' };
  if (m.sender === 'operator') return { who: 'You', cls: 'op' };
  if (m.sender.startsWith('agent:') && (m.kind === 'observation' || m.kind === 'relieved')) return { who: `${m.sender.slice(6)} agent`, cls: `agent ${m.kind}` };
  if (m.sender.startsWith('agent:')) return { who: `${m.sender.slice(6)} agent`, cls: 'agent' };
  if (m.sender.startsWith('drone:')) return { who: m.sender.slice(6), cls: 'drone' };
  return { who: m.sender, cls: '' };
}

type Option = { key: string; label: string; task: (d: DroneState) => { kind: string; label: string; lon?: number; lat?: number; radius_m?: number; agl_m?: number; bbox?: Bounds; size_m?: number } };

/** A field note: what an aircraft's camera was looking at, read from real data, where and when. */
function Note({ n, callsign, onFocus }: { n: FieldNote; callsign: string; onFocus: (lon: number, lat: number) => void }) {
  return <li className={`note ${n.kind}`}><button onClick={() => onFocus(n.lon, n.lat)} title="Show this place on the map">
    <i aria-hidden="true" /><span className="note-head"><b>{callsign}</b><em>{NOTE_LABEL[n.kind] ?? n.kind}</em><time>{ago(n.t)}</time></span>
    <span className="note-text">{n.text}</span></button></li>;
}

/** Add an aircraft: a token for that aircraft alone and the one command that connects it. */
function AddDrone({ store, count }: { store: DroneStore; count: number }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<EnrollRequest>({ id: '', name: '', kind: 'sitl', connection: 'udpin:0.0.0.0:14550', aircraft: 'quad', sensors: ['eo', 'ir'], relay: false });
  const [issued, setIssued] = useState<{ id: string; command: string } | null>(null);
  const [error, setError] = useState(''); const [copied, setCopied] = useState(false);
  const [list, setList] = useState<Enrollment[]>([]);
  const refresh = useCallback(() => { void store.enrollments().then(setList).catch(() => setList([])); }, [store]);
  useEffect(() => { if (open) refresh(); }, [open, refresh]);
  const set = (patch: Partial<EnrollRequest>) => setForm(f => ({ ...f, ...patch }));
  const id = form.id || `D-${String(count + 1).padStart(2, '0')}`;
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setError(''); setCopied(false);
    try {
      const r = await store.enroll({ ...form, id, name: form.name || id });
      setIssued({ id: r.id, command: r.command }); refresh();
    } catch (err) { setError(err instanceof Error ? err.message : 'The aircraft could not be added.'); }
  }
  async function copy() { if (!issued) return; try { await navigator.clipboard.writeText(issued.command); setCopied(true); } catch { setCopied(false); } }
  return <section className="fleet-add">
    <header><b>Add a drone</b><small>{count} connected</small><span className="grow" />
      <button className="mini-btn" onClick={() => { setOpen(o => !o); setIssued(null); }} aria-expanded={open}>{open ? <><X size={12} /> Close</> : <><Plus size={12} /> Add</>}</button></header>
    {!open && <p className="card-note">Each aircraft gets its own token and one command. ArduPilot SITL runs real flight firmware on this computer; a real aircraft joins from its ground station over MAVLink (PX4 or ArduPilot).</p>}
    {open && !issued && <form className="add-form" onSubmit={e => { void submit(e); }}>
      <div className="seg" role="group" aria-label="Kind of aircraft">
        <button type="button" className={form.kind === 'sitl' ? 'on' : ''} onClick={() => set({ kind: 'sitl', aircraft: form.aircraft === 'vtol' ? 'quad' : form.aircraft })}>ArduPilot SITL</button>
        <button type="button" className={form.kind === 'mavlink' ? 'on' : ''} onClick={() => set({ kind: 'mavlink' })}>Real aircraft</button></div>
      <label><span>ID</span><input value={form.id} placeholder={id} maxLength={32} pattern="[A-Za-z0-9_-]{1,32}" onChange={e => set({ id: e.target.value })} /></label>
      <label><span>Name</span><input value={form.name} placeholder="Merlin" maxLength={40} onChange={e => set({ name: e.target.value })} /></label>
      {form.kind === 'mavlink' && <label className="wide"><span>MAVLink link</span><input value={form.connection} maxLength={120} pattern="[A-Za-z0-9_.:/@=,\-]+" onChange={e => set({ connection: e.target.value })}
        title="udpin:0.0.0.0:14550 (telemetry radio or companion computer), tcp:host:5760, or a serial port such as /dev/ttyUSB0" /></label>}
      <label><span>Airframe</span><select value={form.aircraft} onChange={e => set({ aircraft: e.target.value as EnrollRequest['aircraft'] })}>
        <option value="quad">Multirotor</option><option value="fixed-wing">Fixed-wing</option>{form.kind === 'mavlink' && <option value="vtol">VTOL</option>}</select></label>
      <fieldset><legend>Payload</legend>{SENSORS.map(([k, label]) => <label key={k} className="check"><input type="checkbox" checked={form.sensors.includes(k)}
        onChange={e => set({ sensors: e.target.checked ? [...form.sensors, k] : form.sensors.filter(x => x !== k).length ? form.sensors.filter(x => x !== k) : form.sensors })} />{label}</label>)}
        <label className="check"><input type="checkbox" checked={form.relay} onChange={e => set({ relay: e.target.checked })} />Relay radio</label></fieldset>
      <button className="primary" type="submit">Create token and command</button>
      {error && <p className="card-note warn">{error}</p>}
    </form>}
    {open && issued && <div className="issued">
      <p>Run this from the Terra folder{form.kind === 'mavlink' ? ' on the ground-station computer' : ''}. The token inside works only for <b>{issued.id}</b>, is shown once, and can be revoked below.
        {form.kind === 'mavlink' ? ' Tasks stay advisory until you add --allow-commands; the pilot in command decides.' : ' The aircraft takes off from the launch pad when the coordinator gives it a job.'}</p>
      <pre><code>{issued.command}</code></pre>
      <div className="detail-actions"><button className="primary" onClick={() => { void copy(); }}><Copy size={13} /> {copied ? 'Copied' : 'Copy command'}</button>
        <button className="secondary" onClick={() => setIssued(null)}>Add another</button></div>
    </div>}
    {open && list.length > 0 && <ul className="enrolled">{list.map(e => <li key={`${e.drone_id}-${e.created_at}`} className={e.revoked ? 'revoked' : ''}>
      <b>{e.drone_id}</b><span>{e.label}</span><time>{ago(e.created_at)}</time>
      {e.revoked ? <em>revoked</em> : <button className="mini-btn" onClick={() => { void store.revoke(e.drone_id).then(refresh); }}>Revoke</button>}</li>)}</ul>}
  </section>;
}

/** The fleet, the selected aircraft, its task and airspace, people's tasks, and the agents' shared log. */
export default function DronesPanel({ store, selected, onSelect, onCamera, onFull, patches, places, regionName, regionBox, onMonitorHere, onProfile, onFocus }: {
  store: DroneStore; selected: string | null; onSelect: (id: string) => void; onCamera: (id: string) => void; onFull: (id: string) => void; patches: Patch[]; places: Place[];
  regionName: string; regionBox: Bounds; onMonitorHere: () => void; onProfile: (p: Profile) => void; onFocus: (lon: number, lat: number) => void;
}) {
  const [, tick] = useReducer((x: number) => x + 1, 0);
  const [sent, setSent] = useState('');
  useEffect(() => { const id = setInterval(tick, 500); const off = store.subscribe(tick); return () => { clearInterval(id); off(); }; }, [store]);
  const states = store.states(); const info = store.info;
  const s = states.find(d => d.id === selected) || null;
  const area = store.area(); const centre: [number, number] | null = area ? [(area[0] + area[2]) / 2, (area[1] + area[3]) / 2] : null;
  const airborne = states.filter(d => d.status === 'airborne').length;
  const real = states.filter(d => d.source === 'real'); const sitl = states.filter(d => d.source === 'sitl');
  const profile: Profile = info.area?.profile ?? 'change';
  const callsign = (id: string) => states.find(d => d.id === id)?.callsign ?? id.toUpperCase();
  const [allNotes, setAllNotes] = useState(false);
  const notes = (s ? info.notes.filter(n => n.drone_id === s.id) : info.notes);
  const box = (c: [number, number], half: number): Bounds => { const dx = half / (111_320 * Math.cos(c[1] * Math.PI / 180)), dy = half / 110_574; return [c[0] - dx, c[1] - dy, c[0] + dx, c[1] + dy]; };
  const summit = places.filter(p => p.kind === 'peak' || p.kind === 'volcano').sort((a, b) => (b.elevation_m ?? 0) - (a.elevation_m ?? 0))[0];
  const options: Option[] = [
    ...patches.slice(0, 4).map(p => ({ key: `orbit-${p.rank}`, label: `Orbit ${p.label}`, task: (d: DroneState) => ({ kind: 'orbit', label: `Orbit ${p.label}`, lon: p.centre[1], lat: p.centre[0], radius_m: d.aircraft === 'fixed-wing' ? 650 : 240, agl_m: d.aircraft === 'fixed-wing' ? 430 : 190 }) })),
    ...(summit ? [{ key: 'orbit-summit', label: `Orbit ${summit.name}`, task: (d: DroneState) => ({ kind: 'orbit', label: `Orbit ${summit.name}`, lon: summit.lon, lat: summit.lat, radius_m: d.aircraft === 'fixed-wing' ? 700 : 300, agl_m: d.aircraft === 'fixed-wing' ? 500 : 250 }) }] : []),
    ...(patches[0] ? [{ key: 'survey-1', label: `Survey around ${patches[0].label}`, task: () => ({ kind: 'survey', label: `Survey around ${patches[0].label}`, bbox: box([patches[0].centre[1], patches[0].centre[0]], 700), agl_m: 200 }) }] : []),
    ...(area ? [{ key: 'perimeter', label: 'Map the perimeter', task: (d: DroneState) => ({ kind: 'perimeter', label: 'Map the perimeter', bbox: area, agl_m: d.aircraft === 'fixed-wing' ? 430 : 250 }) }] : []),
    ...(centre ? [{ key: 'relay', label: 'Relay over the centre', task: () => ({ kind: 'relay', label: 'Radio relay', lon: centre[0], lat: centre[1], radius_m: 80, agl_m: 300 }) },
      { key: 'hold', label: 'Hold over the centre', task: () => ({ kind: 'hold', label: 'Hold', lon: centre[0], lat: centre[1], agl_m: 150 }) },
      { key: 'search', label: 'Search around the centre', task: () => ({ kind: 'search', label: 'Search the centre', lon: centre[0], lat: centre[1], size_m: 1200, agl_m: 90 }) }] : []),
    ...(patches[0] ? [{ key: 'search-1', label: `Search ${patches[0].label}`, task: () => ({ kind: 'search', label: `Search ${patches[0].label}`, lon: patches[0].centre[1], lat: patches[0].centre[0], size_m: 1000, agl_m: 90 }) }] : []),
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
      {real.length ? <span className="live-chip"><i />LIVE {real.length}</span> : null}
      {sitl.length ? <span className="sitl-chip" title="ArduPilot SITL: the real ArduCopter and ArduPlane firmware flying simulated airframes">SITL {sitl.length}</span> : null}
      {states.length - real.length - sitl.length > 0 ? <span className="sim-chip" title="Terra's virtual aircraft, connected through the same protocol as real drones">SIM {states.length - real.length - sitl.length}</span> : null}</header>
    <div className="profile-pick" role="group" aria-label="What the fleet is for">{PROFILES.map(p => <button key={p.key} className={profile === p.key ? 'on' : ''} disabled={!info.area}
      onClick={() => { if (p.key !== profile) onProfile(p.key); }} aria-pressed={profile === p.key}>{p.label}</button>)}</div>
    <p className="profile-note">{PROFILES.find(p => p.key === profile)?.note}</p>
    <div className="area-line"><span>Monitoring <b>{info.area?.name ?? 'no area yet'}</b></span>
      {(!info.area || info.area.bbox.some((v, i) => Math.abs(v - regionBox[i]) > 1e-6)) && <button className="mini-btn" onClick={onMonitorHere}
        title="Virtual aircraft are taken there; real aircraft far away stand by">Monitor {regionName.length > 22 ? `${regionName.slice(0, 22)}…` : regionName}</button>}</div>
    <div className={`coord-chip ${info.coordinator.running ? 'on' : ''}`} title="The durable coordinator agent (a Temporal workflow) and one agent per aircraft">
      <i />{info.coordinator.running ? <>Coordinator · round {info.coordinator.summary.round ?? '—'} · {info.coordinator.summary.text}</> : info.area ? 'Coordinator starting or offline: the investigation service may be down.' : 'Waiting for a monitoring area.'}</div>
    {!states.length && info.connections && <p className="card-note fleet-empty">No aircraft connected yet. The dev stack starts an ArduPilot SITL fleet when it is built (python3 scripts/sitl.py setup), otherwise a virtual fleet; add more below.</p>}
    <ul className="fleet-list">{states.map(d => <li key={d.id}><button className={d.id === selected ? 'on' : ''} onClick={() => onSelect(d.id)}>
      <span className="fleet-icon"><AircraftIcon aircraft={d.aircraft} /></span>
      <span className="fleet-name"><b>{d.callsign}</b> {d.name}<em className={`src-tag ${d.source}`}>{SOURCE[d.source].tag}</em><small>{d.mission.task}</small></span>
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
        ['Aircraft', `${s.aircraft === 'fixed-wing' ? 'Fixed-wing' : 'Quadcopter'} · ${SOURCE[s.source].long}`],
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
    <section className="fleet-notes">
      <header><b>Field notes</b><small>{s ? `${s.callsign}'s` : 'every aircraft'}, from what the cameras look at</small></header>
      {notes.length ? <ol>{notes.slice(0, allNotes ? 40 : 4).map(n => <Note key={n.id} n={n} callsign={callsign(n.drone_id)} onFocus={onFocus} />)}</ol>
        : <p className="card-note">On station and every two minutes after, each aircraft's agent notes the ground its camera is on: Landsat surface temperature, radar change, land cover, height and the weather.</p>}
      {notes.length > 4 && <button className="mini-btn" onClick={() => setAllNotes(a => !a)}>{allNotes ? 'Fewer' : `All ${Math.min(40, notes.length)} notes`}</button>}
    </section>
    <section className="fleet-log">
      <header><b>Coordination log</b><small>agents, aircraft and people, newest first</small></header>
      {info.messages.length ? <ol>{info.messages.slice(0, 30).map(m => { const w = sender(m); return <li key={m.id}>
        <span className={`who ${w.cls}`}>{w.who}</span><span className="what">{m.body.text ?? m.kind}</span><time>{ago(m.t)}</time></li>; })}</ol>
        : <p className="card-note">Decisions, hand-offs, warnings and acknowledgements appear here as the agents work.</p>}
    </section>
    {info.connections ? <AddDrone store={store} count={states.length} />
      : <p className="card-note fleet-empty">Drone connections are off: set TERRA_DRONE_TOKEN on the server, then add aircraft here.</p>}
    <p className="fleet-foot">Reserved airspace is published as ASTM F3548-shaped operational intents at <code>/api/fleet/operations</code>. Real aircraft are never flown from the browser.</p>
  </div>;
}
