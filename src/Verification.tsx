import { useEffect, useState } from 'react';
import { CheckCircle2, CircleDashed, Plane, XCircle } from 'lucide-react';
import { api, type Bounds } from './types';
import './verification.css';

export type Signature = { hypothesis: string; mechanism: string; consistent_with: string[]; question: string; needs: string[]; confidence: 'strong' | 'moderate' | 'weak'; basis: string };
export type SignedSpot = { rank: number; direction: 'increase' | 'decrease'; area_km2: number | null; mean_change_db: number | null; centre: [number, number]; bounds: Bounds; signature?: Signature; leading_polarization?: string | null };
type Judged = { note_id: number; t: number; drone_id: string; verdict: 'supports' | 'contradicts' | 'inconclusive'; reasons: string[]; surface_c?: number | null; cover?: string | null };
type Row = { target_id: string; observations: number; aircraft: string[]; counts: Record<string, number>; overall: string; latest: Judged[] };

const ago = (t: number) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };
const OVERALL: Record<string, string> = { supported: 'Supported in the field', contradicted: 'Contradicted in the field', mixed: 'Mixed field evidence', inconclusive: 'Observed, inconclusive', 'not yet observed': 'Not yet observed' };

/**
 * Radar to drones and back. Every changed patch shows what its polarimetric change is consistent with and the
 * question that would settle it; one button sends the fleet to verify them all, and each drone's measured
 * observations come back as supporting, contradicting or inconclusive evidence, per patch, as they arrive.
 */
export default function Verification({ jobId, spots, fleetArea, onSpot, onWatch }: {
  jobId: string; spots: SignedSpot[]; fleetArea: string | null | undefined; onSpot: (s: SignedSpot) => void; onWatch: () => void;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false); const [msg, setMsg] = useState('');
  const dispatched = fleetArea === jobId;
  useEffect(() => {
    let alive = true;
    const load = () => api<{ patches: Row[] }>(`/fleet/evidence/${encodeURIComponent(jobId)}`).then(v => { if (alive) setRows(v.patches); }).catch(() => {});
    void load(); const t = setInterval(load, 5000); return () => { alive = false; clearInterval(t); };
  }, [jobId]);
  async function send() {
    setBusy(true); setMsg('');
    try {
      const r = await api<{ targets: unknown[] }>('/fleet/dispatch', { method: 'POST', body: JSON.stringify({ investigation_id: jobId }) });
      setMsg(`The fleet is on its way to ${r.targets.length} patch${r.targets.length === 1 ? '' : 'es'}. Verdicts appear here as the drones report.`);
    } catch (e) { setMsg(e instanceof Error ? e.message : 'The fleet could not be dispatched.'); }
    finally { setBusy(false); }
  }
  const signed = spots.filter(s => s.signature);
  if (!signed.length) return null;
  return <section className="verify">
    <header><div><b>What the radar change is consistent with</b><small>Each patch's co- and cross-polarized change, read physically. Hypotheses to verify, not findings.</small></div></header>
    <ol>{signed.map(s => { const sig = s.signature!; const row = rows.find(r => r.target_id === `patch-${s.rank}`);
      return <li key={s.rank} className={`verify-row ${row?.overall.replace(/ /g, '-') ?? ''}`}>
        <button className="verify-head" onClick={() => onSpot(s)} title="Show this patch on the map">
          <span className={`verify-rank ${s.direction}`}>{s.rank}</span>
          <span className="verify-mech"><b>{sig.consistent_with.slice(0, 2).join(' or ')}</b><small>{sig.mechanism}</small></span>
          <em className={`verify-conf ${sig.confidence}`}>{sig.confidence}</em>
        </button>
        <p className="verify-basis"><span>{sig.basis}</span>{s.area_km2 != null && <span>{s.area_km2.toFixed(2)} km²</span>}</p>
        <p className="verify-q">{sig.question}<small> · helps: {sig.needs.map(n => n.toUpperCase()).join(', ')}</small></p>
        {row && <div className="verify-field">
          <span className={`verify-overall ${row.overall.replace(/ /g, '-')}`}>{row.overall === 'supported' ? <CheckCircle2 size={13} /> : row.overall === 'contradicted' ? <XCircle size={13} /> : <CircleDashed size={13} />}{OVERALL[row.overall] ?? row.overall}</span>
          {row.observations > 0 && <small>{row.counts.supports} support · {row.counts.contradicts} contradict · {row.counts.inconclusive} inconclusive · {row.aircraft.join(', ')}</small>}
          {row.latest[0] && <p>{row.latest[0].drone_id}, {ago(row.latest[0].t)}: {row.latest[0].reasons[0]}.</p>}
        </div>}
      </li>; })}</ol>
    <div className="verify-actions">
      <button className="primary" onClick={() => { void send(); }} disabled={busy}><Plane size={14} />{busy ? 'Dispatching…' : dispatched ? 'Re-dispatch the fleet' : 'Send drones to verify'}</button>
      {dispatched && <button className="secondary" onClick={onWatch}>Watch the fleet</button>}
    </div>
    {msg && <p className="verify-msg">{msg}</p>}
    <p className="verify-note">Drones judge only measured values: ESA WorldCover (2021, before the change) and Landsat surface temperature at the ground their camera sees. Field evidence is for you to weigh; soil moisture, wind on water and viewing geometry can mimic these signatures.</p>
  </section>;
}
