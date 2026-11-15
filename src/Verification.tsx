import { useState } from 'react';
import { CheckCircle2, CircleDashed, FileText, Plane, XCircle } from 'lucide-react';
import type { Bounds } from './types';
import { OVERALL, ago, dispatchFleet, useFieldEvidence } from './fieldEvidence';
import './verification.css';

export type Signature = { hypothesis: string; mechanism: string; consistent_with: string[]; question: string; needs: string[]; confidence: 'strong' | 'moderate' | 'weak'; basis: string };
export type SignedSpot = { rank: number; direction: 'increase' | 'decrease'; area_km2: number | null; mean_change_db: number | null; centre: [number, number]; bounds: Bounds; signature?: Signature; leading_polarization?: string | null };

/**
 * Radar to drones and back. Every changed patch shows what its polarimetric change is consistent with and the
 * question that would settle it; one button sends the fleet to verify them all, and each drone's measured
 * observations come back as supporting, contradicting or inconclusive evidence, per patch, as they arrive.
 */
export default function Verification({ jobId, spots, fleetArea, onSpot, onWatch, onReport }: {
  jobId: string; spots: SignedSpot[]; fleetArea: string | null | undefined; onSpot: (s: SignedSpot) => void; onWatch: () => void;
  /** Open the investigation report at this patch's field verification. */
  onReport?: (rank: number) => void;
}) {
  const rows = useFieldEvidence(jobId).value?.patches ?? [];
  const [busy, setBusy] = useState(false); const [msg, setMsg] = useState('');
  const dispatched = fleetArea === jobId;
  async function send() {
    setBusy(true); setMsg('');
    try {
      const n = await dispatchFleet(jobId);
      setMsg(`The fleet is on its way to ${n} patch${n === 1 ? '' : 'es'}. Verdicts appear here and in the report as the drones report.`);
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
          {row.alternatives?.map(a => <p key={a} className="verify-alt">Alternative: {a}.</p>)}
          {row.latest[0] && <p>{row.latest[0].drone_id}, {ago(row.latest[0].t)}: {row.latest[0].reasons[0]}.</p>}
          {row.observations > 0 && onReport && <button className="verify-report" onClick={() => onReport(s.rank)}><FileText size={12} />Field evidence in the report</button>}
        </div>}
      </li>; })}</ol>
    <div className="verify-actions">
      <button className="primary" onClick={() => { void send(); }} disabled={busy}><Plane size={14} />{busy ? 'Dispatching…' : dispatched ? 'Re-dispatch the fleet' : 'Send drones to verify'}</button>
      {dispatched && <button className="secondary" onClick={onWatch}>Watch the fleet</button>}
    </div>
    {msg && <p className="verify-msg">{msg}</p>}
    <p className="verify-note">Drones judge only measured values at the ground their camera sees: Sentinel-2 before and after the radar dates (snow, cloud and plumes are recognised and named), ESA WorldCover (2021) and Landsat surface temperature. Field evidence is for you to weigh; soil moisture, wind on water and viewing geometry can mimic these signatures.</p>
  </section>;
}
