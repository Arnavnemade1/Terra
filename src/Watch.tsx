import { useCallback, useEffect, useState } from 'react';
import { BellRing, Check, Eye, Plane, RefreshCw, Square, X } from 'lucide-react';
import { api, type Bounds } from './types';
import './watch.css';

type Acq = { id: string; date?: string; track?: number; frame?: number; direction?: string; maturity?: string; product_version?: string };
export type AreaWatch = {
  id: string; name: string; bbox: Bounds; investigation_id?: string | null; status: string; message?: string; updated_at?: string;
  next_pass_at?: string | null; last_check_at?: string; reference?: Acq; latest_investigation_id?: string; cycles?: number;
  assessment?: { significant: boolean; summary: string; changed_pct?: number; patches?: number };
  proposal?: { id: string; investigation_id: string; summary: string; patches?: number; at: string } | null;
  dispatched?: { targets?: number; investigation_id?: string; at?: string };
  history?: { at: string; status: string; message: string }[];
};

const LABEL: Record<string, string> = {
  starting: 'Starting', watching: 'Watching', waiting_pass: 'Waiting for NISAR', checking: 'Checking the archive', waiting_data: 'Waiting for data',
  investigating: 'Measuring', proposal: 'Needs your decision', dispatched: 'Drones dispatched', dismissed: 'Dismissed', expired: 'Proposal expired',
  stopped: 'Stopped', failed: 'Could not start',
};
const day = (s?: string | null) => s ? new Date(s).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }) : '—';
const stamp = (s?: string | null) => s ? `${new Date(s).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })}, ${new Date(s).toISOString().slice(11, 16)} UTC` : '—';
const until = (s: string, now: number) => {
  const ms = Date.parse(s) - now; if (ms <= 0) return 'now';
  const h = Math.floor(ms / 3_600_000); const d = Math.floor(h / 24);
  return d ? `in ${d} d ${h % 24} h` : h ? `in ${h} h ${Math.floor(ms / 60_000) % 60} min` : `in ${Math.max(1, Math.round(ms / 60_000))} min`;
};
const ago = (s: string, now: number) => { const m = Math.max(0, Math.round((now - Date.parse(s)) / 60_000)); return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };

/**
 * A durable watch over this investigation's area: Terra wakes on NISAR's next pass, finds the new acquisition with the
 * same viewing geometry, measures the change against the last one, and asks before sending drones.
 */
export default function Watch({ jobId, name, bbox, onOpen }: { jobId: string; name: string; bbox: Bounds; onOpen: (investigationId: string) => void }) {
  const [watch, setWatch] = useState<AreaWatch | null>(null);
  const [busy, setBusy] = useState(''); const [error, setError] = useState('');
  const [now, setNow] = useState(Date.now());
  const id = `watch-${jobId}`;
  const load = useCallback(() => api<{ watches: AreaWatch[] }>('/watches').then(v => setWatch(v.watches.find(w => w.id === id) ?? null)).catch(() => {}), [id]);
  useEffect(() => { void load(); const t = setInterval(() => { void load(); setNow(Date.now()); }, 4000); return () => clearInterval(t); }, [load]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label); setError('');
    try { await fn(); await load(); } catch (e) { setError(e instanceof Error ? e.message : 'The watch did not answer.'); }
    finally { setBusy(''); }
  }
  const start = () => run('start', () => api('/watches', { method: 'POST', body: JSON.stringify({ name, bbox, investigation_id: jobId }) }));
  const signal = (path: string, body?: object) => run(path, () => api(`/watches/${encodeURIComponent(id)}/${path}`, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) }));

  const live = watch && !['stopped', 'failed'].includes(watch.status);
  if (!live) return <section className="watch">
    <header><BellRing size={14} /><div><b>Watch this area</b><small>Terra wakes on NISAR’s next pass over {name}, measures the change against the latest acquisition and asks you before sending drones.</small></div></header>
    {watch?.status === 'failed' && <p className="watch-msg">{watch.message}</p>}
    <div className="watch-actions"><button className="primary" onClick={start} disabled={Boolean(busy)}><Eye size={14} />{busy === 'start' ? 'Starting…' : watch ? 'Watch again' : 'Watch this area'}</button></div>
    {error && <p className="watch-msg warn">{error}</p>}
  </section>;

  const w = watch!;
  const ref = w.reference;
  return <section className={`watch on ${w.status}`}>
    <header><BellRing size={14} /><div><span className="watch-title"><b>Watching {w.name}</b><span className={`watch-chip ${w.status}`}>{LABEL[w.status] ?? w.status}</span></span>
      <small>A durable agent: it keeps its place through restarts and only flies when you approve.</small></div></header>
    <dl className="watch-facts">
      <div><dt>Next NISAR pass</dt><dd>{w.next_pass_at ? <><b>{until(w.next_pass_at, now)}</b><small>{stamp(w.next_pass_at)}{ref?.direction ? ` · ${ref.direction.toLowerCase()}` : ''}</small></> : '—'}</dd></div>
      <div><dt>Baseline</dt><dd>{ref ? <><b>{day(ref.date)}</b><small>track {ref.track ?? '—'}{ref.frame != null ? `, frame ${ref.frame}` : ''}{ref.maturity ? ` · ${ref.maturity.toLowerCase()}` : ''}{ref.product_version ? ` ${ref.product_version}` : ''}</small></> : '—'}</dd></div>
      <div><dt>Last checked</dt><dd><b>{w.last_check_at ? ago(w.last_check_at, now) : 'not yet'}</b></dd></div>
    </dl>
    {w.message && w.status !== 'proposal' && <p className="watch-msg">{w.message}</p>}
    {w.proposal && <div className="watch-proposal">
      <b><Plane size={13} />New radar change: send drones to verify?</b>
      <p>{w.proposal.summary}</p>
      <div className="watch-actions">
        <button className="primary" disabled={Boolean(busy)} onClick={() => signal('decide', { action: 'approve', proposal: w.proposal!.id })}><Check size={14} />{busy === 'decide' ? 'Sending…' : 'Approve dispatch'}</button>
        <button className="secondary" disabled={Boolean(busy)} onClick={() => signal('decide', { action: 'dismiss', proposal: w.proposal!.id })}><X size={14} />Dismiss</button>
        <button className="link" onClick={() => onOpen(w.proposal!.investigation_id)}>Open the comparison</button>
      </div>
    </div>}
    {!w.proposal && w.latest_investigation_id && <button className="link watch-open" onClick={() => onOpen(w.latest_investigation_id!)}>Open the latest comparison</button>}
    <div className="watch-actions">
      <button className="secondary" disabled={Boolean(busy) || ['checking', 'investigating', 'proposal'].includes(w.status)} onClick={() => signal('check')}><RefreshCw size={13} />{busy === 'check' ? 'Checking…' : 'Check now'}</button>
      <button className="secondary" disabled={Boolean(busy)} onClick={() => signal('stop')}><Square size={12} />Stop</button>
    </div>
    {error && <p className="watch-msg warn">{error}</p>}
    {(() => { const log = [...(w.history ?? [])].reverse().filter((h, i) => !(i === 0 && h.message === w.message)).slice(0, 4);
      return log.length > 0 && <details className="watch-log"><summary>Activity</summary><ol>{log.map((h, i) => <li key={i}><time>{ago(h.at, now)}</time><span>{h.message}</span></li>)}</ol></details>; })()}
  </section>;
}
