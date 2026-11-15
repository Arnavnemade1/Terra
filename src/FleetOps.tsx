import { useEffect, useReducer, useState } from 'react';
import { Anchor, MapPin, Pause, Play, Undo2 } from 'lucide-react';
import type { DroneStore } from './drones';
import './fleetOps.css';

const ago = (s: number) => s < 60 ? `${Math.round(s)} s ago` : `${Math.round(s / 60)} min ago`;
const MODE = { normal: 'Auction', hold: 'Held', recall: 'Recalled' } as const;

/**
 * Orchestration at a glance: deploy the fleet anywhere, fleet-wide commands, and the durable agents that run it
 * (the coordinator and one agent per aircraft), each with its round, its own pace and its last heartbeat.
 */
export default function FleetOps({ store, deploying, onDeploy, radiusKm, onRadius }: {
  store: DroneStore; deploying: boolean; onDeploy: () => void; radiusKm: number; onRadius: (km: number) => void;
}) {
  const [, tick] = useReducer((x: number) => x + 1, 0);
  const [busy, setBusy] = useState(''); const [error, setError] = useState('');
  useEffect(() => store.subscribe(tick), [store]);
  const info = store.info; const mode = info.mode.mode; const c = info.coordinator;
  const agents = info.agents.filter(a => a.kind === 'drone');
  const live = agents.filter(a => a.age_s < 45).length;
  const callsign = (id: string) => id.replace(/^agent:/, '').toUpperCase();

  async function command(action: 'hold' | 'recall' | 'resume') {
    setBusy(action); setError('');
    try { await store.command(action); } catch (e) { setError(e instanceof Error ? e.message : 'The command was not accepted.'); }
    finally { setBusy(''); }
  }

  return <section className="fleet-ops">
    <header><b>Orchestration</b><span className={`ops-mode ${mode}`}>{MODE[mode]}</span><span className="grow" />
      <small>{c.running ? `round ${c.summary.round ?? '—'} · next in ${c.summary.next_s ?? '—'} s` : 'coordinator offline'}</small></header>

    <div className="ops-deploy">
      <button className={`primary ${deploying ? 'on' : ''}`} onClick={onDeploy} aria-pressed={deploying}><MapPin size={13} />{deploying ? 'Click the map…' : 'Deploy anywhere'}</button>
      <div className="ops-radius" role="group" aria-label="Area around the launch site">{[2, 5, 10].map(km =>
        <button key={km} className={radiusKm === km ? 'on' : ''} aria-pressed={radiusKm === km} onClick={() => onRadius(km)}>{km} km</button>)}</div>
    </div>
    <p className="ops-note">{info.area ? <>Launching from <b>{info.area.name}</b>. </> : null}Click any place on Earth: the launch site moves there and the fleet watches the area around it.</p>

    <div className="ops-commands" role="group" aria-label="Fleet-wide commands">
      <button onClick={() => { void command('hold'); }} disabled={mode === 'hold' || Boolean(busy)}><Pause size={13} />Hold all</button>
      <button onClick={() => { void command('recall'); }} disabled={mode === 'recall' || Boolean(busy)}><Undo2 size={13} />Recall all</button>
      <button onClick={() => { void command('resume'); }} disabled={mode === 'normal' || Boolean(busy)}><Play size={13} />Resume</button>
    </div>
    {mode !== 'normal' && <p className="ops-note warn">{info.mode.reason}{info.mode.since ? ` · since ${ago(Date.now() / 1000 - info.mode.since)}` : ''}</p>}
    {error && <p className="ops-note warn">{error}</p>}

    <div className="ops-agents">
      <div className="ops-agents-head"><Anchor size={12} /><span>Durable agents</span><em>{live} of {agents.length} live{c.summary.pending_acks ? ` · ${c.summary.pending_acks} awaiting acknowledgement` : ''}</em></div>
      <ol>
        <li className={c.running ? 'ok' : 'warn'}><b>Coordinator</b><span>{c.summary.text ?? 'Starting…'}</span>
          <time>{c.age_s != null ? ago(c.age_s) : '—'}</time><small>every {c.summary.next_s ?? '—'} s</small></li>
        {agents.map(a => <li key={a.id} className={a.age_s < 45 ? (a.status === 'online' ? 'ok' : 'idle') : 'warn'}>
          <b>{callsign(a.id)}</b><span>{a.text ?? '—'}</span><time>{ago(a.age_s)}</time>
          <small>{a.age_s >= 45 ? 'restarting' : `round ${a.round ?? '—'} · every ${a.next_s ?? '—'} s`}</small></li>)}
      </ol>
      <p className="ops-note">Agents pace themselves (about 2 s while something changes, up to 20 s for a silent aircraft) and are woken at once by awards, acknowledgements and registrations. The coordinator restarts any agent whose heartbeat goes quiet.</p>
    </div>
  </section>;
}
