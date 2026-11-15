import { useEffect, useReducer, useState } from 'react';
import { Anchor, MapPin, Pause, Plane, Play, Undo2 } from 'lucide-react';
import type { DroneStore, Profile } from './drones';
import type { Bounds } from './types';
import './fleetOps.css';

const ago = (s: number) => s < 60 ? `${Math.round(s)} s ago` : `${Math.round(s / 60)} min ago`;
const MODE = { normal: 'Coordinated', hold: 'Held', recall: 'Returning' } as const;

/**
 * Orchestration at a glance: deploy the fleet anywhere, fleet-wide commands, and the durable agents that run it
 * (the coordinator and one agent per aircraft), each with its round, its own pace and its last heartbeat.
 */
export default function FleetOps({ store, deploying, onDeploy, regionName, onLaunchHere }: {
  store: DroneStore; deploying: boolean; onDeploy: () => void; regionName: string; onLaunchHere: () => Promise<void>;
}) {
  const [, tick] = useReducer((x: number) => x + 1, 0);
  const [busy, setBusy] = useState(''); const [error, setError] = useState('');
  useEffect(() => store.subscribe(tick), [store]);
  const info = store.info; const mode = info.mode.mode; const c = info.coordinator;
  const agents = info.agents.filter(a => a.kind === 'drone');
  const live = agents.filter(a => a.age_s < 45).length;
  const callsign = (id: string) => id.replace(/^agent:/, '').toUpperCase();
  const states = store.states(); const online = states.filter(d => d.status !== 'lost' && d.status !== 'stale').length;
  const waiting = !info.error && info.area && !states.length;
  async function launch() {
    setBusy('launch'); setError('');
    try { await onLaunchHere(); } catch (e) { setError(e instanceof Error ? e.message : 'The fleet could not launch.'); }
    finally { setBusy(''); }
  }

  async function command(action: 'hold' | 'recall' | 'resume') {
    setBusy(action); setError('');
    try { await store.command(action); } catch (e) { setError(e instanceof Error ? e.message : 'The command was not accepted.'); }
    finally { setBusy(''); }
  }

  return <section className={`fleet-ops ${online ? 'connected' : ''}`}>
    <header><b>Flight controls</b><span className={`ops-mode ${mode}`}>{info.area ? MODE[mode] : 'Ready to plan'}</span><span className="grow" />
      <small>{online ? `${online} connected` : info.simulation.managed ? `${info.simulation.aircraft_count} simulated aircraft` : 'Simulation / connected aircraft'}</small></header>

    <p className="ops-location"><MapPin size={12} /><span>{regionName}</span><small>selected on map</small></p>
    <div className="ops-deploy">
      <button className="primary" onClick={() => { void launch(); }} disabled={!store.ready || Boolean(info.error) || Boolean(busy)}><Plane size={14} />{busy === 'launch' ? 'Starting mission…' : 'Launch here'}</button>
      <button className={`secondary ${deploying ? 'on' : ''}`} onClick={onDeploy} disabled={Boolean(busy)} aria-pressed={deploying}><MapPin size={13} />{deploying ? 'Cancel drawing' : 'Draw flight area'}</button>
    </div>
    <p className="ops-note" role="status">{waiting ? 'Starting the simulator. Aircraft will appear as they connect.' : info.area ? <>Operating over <b>{info.area.name}</b>. Aircraft take off as missions are assigned.</> : 'Launch over this landscape, or draw a perimeter and choose a launch pad.'}</p>

    <div className="ops-commands" role="group" aria-label="Fleet-wide commands">
      <button onClick={() => { void command('hold'); }} disabled={!online || mode === 'hold' || Boolean(busy)}><Pause size={13} />Hold</button>
      <button onClick={() => { void command('recall'); }} disabled={!online || mode === 'recall' || Boolean(busy)}><Undo2 size={13} />Return</button>
      <button onClick={() => { void command('resume'); }} disabled={!online || mode === 'normal' || Boolean(busy)}><Play size={13} />Resume</button>
    </div>
    {mode !== 'normal' && <p className="ops-note warn">{info.mode.reason}{info.mode.since ? ` · since ${ago(Date.now() / 1000 - info.mode.since)}` : ''}</p>}
    {error && <p className="ops-note warn" role="alert">{error}</p>}

    <details className="ops-agents">
      <summary className="ops-agents-head"><Anchor size={12} /><span>Mission coordination</span><em>{live}/{agents.length} agents{c.summary.pending_acks ? ` · ${c.summary.pending_acks} awaiting acknowledgement` : ''}</em></summary>
      <ol>
        <li className={c.running ? 'ok' : 'warn'}><b>Coordinator</b><span>{c.summary.text ?? 'Starting…'}</span>
          <time>{c.age_s != null ? ago(c.age_s) : '—'}</time><small>every {c.summary.next_s ?? '—'} s</small></li>
        {agents.map(a => <li key={a.id} className={a.age_s < 45 ? (a.status === 'online' ? 'ok' : 'idle') : 'warn'}>
          <b>{callsign(a.id)}</b><span>{a.text ?? '—'}</span><time>{ago(a.age_s)}</time>
          <small>{a.age_s >= 45 ? 'restarting' : `round ${a.round ?? '—'} · every ${a.next_s ?? '—'} s`}</small></li>)}
      </ol>
      <p className="ops-note">Agents pace themselves (about 2 s while something changes, up to 20 s for a silent aircraft) and are woken at once by awards, acknowledgements and registrations. The coordinator restarts any agent whose heartbeat goes quiet.</p>
    </details>
  </section>;
}

const km = (b: Bounds) => [(b[2] - b[0]) * 111.32 * Math.cos((b[1] + b[3]) / 2 * Math.PI / 180), (b[3] - b[1]) * 110.574];
const PROFILES: { key: Profile; label: string }[] = [{ key: 'change', label: 'Change watch' }, { key: 'thermal', label: 'Thermal watch' }, { key: 'search', label: 'Search' }];

/** Confirming a drawn deployment: its size, a name, what the fleet is for, and where it launches from. */
export function DeployCard({ area, pad, profile, onLaunch, onRedraw, onCancel }: {
  area: Bounds; pad: [number, number] | null; profile: Profile; onLaunch: (name: string, p: Profile) => Promise<void>; onRedraw: () => void; onCancel: () => void;
}) {
  const [w, h] = km(area); const big = w > 220 || h > 220;
  const at = pad ?? [(area[0] + area[2]) / 2, (area[1] + area[3]) / 2];
  const [name, setName] = useState(`Deployment ${Math.abs(at[1]).toFixed(2)}° ${at[1] >= 0 ? 'N' : 'S'} ${Math.abs(at[0]).toFixed(2)}° ${at[0] >= 0 ? 'E' : 'W'}`);
  const [kind, setKind] = useState<Profile>(profile); const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function launch() {
    setBusy(true); setError('');
    try { await onLaunch(name.trim(), kind); }
    catch (e) { setError(e instanceof Error ? e.message : 'The fleet could not launch. Please retry.'); }
    finally { setBusy(false); }
  }
  return <div className="deploy-card" role="dialog" aria-label="Confirm the deployment">
    <header><b>Deploy the fleet here</b><span>{w.toFixed(1)} × {h.toFixed(1)} km perimeter</span></header>
    <label><span>Name</span><input value={name} maxLength={80} onChange={e => setName(e.target.value)} /></label>
    <div className="deploy-kinds" role="group" aria-label="What the fleet does">{PROFILES.map(p =>
      <button key={p.key} className={kind === p.key ? 'on' : ''} aria-pressed={kind === p.key} onClick={() => setKind(p.key)}>{p.label}</button>)}</div>
    <p>Launch pad at {at[1].toFixed(4)}, {at[0].toFixed(4)}. Click the map to move it. The fleet maps this perimeter, orbits what matters inside it and keeps a relay up.</p>
    {big && <p className="warn">Larger than 2° across: draw a smaller perimeter.</p>}
    {error && <p className="warn" role="alert">{error}</p>}
    <div className="deploy-actions">
      <button className="primary" disabled={busy || big || !name.trim()} onClick={() => { void launch(); }}>{busy ? 'Launching…' : 'Launch fleet'}</button>
      <button className="secondary" onClick={onRedraw}>Redraw</button><button className="secondary" onClick={onCancel}>Cancel</button>
    </div>
  </div>;
}
