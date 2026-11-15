import { useState } from 'react';
import { Download, MapPin, Plane, Video } from 'lucide-react';
import { GeoFrame, groundAspect, type Frame } from './Compare';
import type { Hotspot } from './Dossier';
import { OVERALL, SHORT, ago, dispatchFleet, type FieldEvidence, type PatchEvidence, type Verdict } from './fieldEvidence';
import type { Bounds } from './types';
import type { Signature } from './Verification';

export type FieldAircraft = { id: string; callsign: string; live: boolean } | null;

const SOURCE: Record<string, string> = { sitl: 'ArduPilot SITL (real flight firmware on simulated airframes)', virtual: 'Terra’s virtual fleet', real: 'connected aircraft' };
const utc = (t: number) => `${new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
const n = (v: number, one: string, many = `${one}s`) => `${v.toLocaleString('en')} ${v === 1 ? one : many}`;
const signed = (v: number | null | undefined, d = 1) => typeof v === 'number' && Number.isFinite(v) ? `${v > 0 ? '+' : ''}${v.toFixed(d)}`.replace('-', '−') : '—';

/** A ground-square crop around a patch with some of its surroundings, so the eye can tell patch from context. */
function around(b: Bounds): Bounds {
  const [w, s, e, nn] = b; const lat = (s + nn) / 2; const k = Math.cos(lat * Math.PI / 180);
  const half = Math.max((e - w) * k, nn - s) * 0.95 + 0.0035;  // degrees of latitude, about 390 m minimum
  const cx = (w + e) / 2, cy = lat;
  return [cx - half / k, cy - half, cx + half / k, cy + half];
}
const at = (lon: number, lat: number, b: Bounds): React.CSSProperties => ({ left: `${((lon - b[0]) / (b[2] - b[0])) * 100}%`, top: `${((b[3] - lat) / (b[3] - b[1])) * 100}%` });

/** One patch's ground: the radar change and the later true-colour image, the patch outlined, every drone look as a dot. */
function PatchMaps({ bounds, rank, tone, points, change, after }: { bounds: Bounds; rank: number; tone: 'up' | 'down'; points: [number, number, Verdict, number][]; change?: Frame; after?: Frame }) {
  const view = around(bounds);
  const dots = points.filter(([lon, lat]) => lon >= view[0] && lon <= view[2] && lat >= view[1] && lat <= view[3]);
  const frame = (f: Frame) => <figure key={f.label}><figcaption><b>{f.label}</b>{f.date && <span>{f.date}</span>}</figcaption>
    <GeoFrame bbox={view} frames={[f]} boxes={[{ id: String(rank), bounds, label: String(rank), tone }]} className="field-frame">
      {dots.map(([lon, lat, v, id]) => <i key={`${id}-${lon}`} className={`field-dot ${v}`} style={at(lon, lat, view)} />)}
    </GeoFrame></figure>;
  const frames = [change, after].filter(Boolean) as Frame[];
  if (!frames.length) return null;
  return <div className="field-maps" style={{ ['--aspect' as string]: String(groundAspect(view)) }}>{frames.map(frame)}</div>;
}

/**
 * The report's field verification: what the radar change in each patch is consistent with, the question that would
 * settle it, and what the drones found when they looked, judged only from measured layers at each camera's ground
 * point. Live while the report is open; the investigation keeps the latest evidence, so a printed or later report has it.
 */
export default function FieldSection({ jobId, hotspots, evidence, live, error, canDispatch, dispatched, change, after, aircraftFor, onDrone, onMap, artifactHref }: {
  jobId: string; hotspots: Hotspot[]; evidence: FieldEvidence | null; live: boolean; error: string; canDispatch: boolean; dispatched: boolean;
  change?: Frame; after?: Frame; aircraftFor?: (rank: number, observers: string[]) => FieldAircraft;
  onDrone?: (id: string) => void; onMap?: (h: Hotspot) => void; artifactHref?: string;
}) {
  const [busy, setBusy] = useState(false); const [msg, setMsg] = useState('');
  const rows = new Map((evidence?.patches ?? []).map(p => [p.target_id, p]));
  const spots = hotspots.filter(h => (h as Hotspot & { signature?: Signature }).signature || rows.has(`patch-${h.rank}`));
  if (!spots.length) return null;
  const observed = spots.filter(h => (rows.get(`patch-${h.rank}`)?.observations ?? 0) > 0);
  const waiting = spots.filter(h => !observed.includes(h));
  const totals = evidence?.totals ?? { supports: 0, contradicts: 0, inconclusive: 0 };
  const looks = observed.reduce((a, h) => a + (rows.get(`patch-${h.rank}`)?.observations ?? 0), 0);
  const aircraft = evidence?.aircraft ?? [];
  const origins = new Set(aircraft.map(a => a.origin ?? 'virtual'));
  const simulated = [...origins].filter(o => o !== 'real');
  const verdicts = observed.map(h => rows.get(`patch-${h.rank}`)!.overall);
  const count = (k: string) => verdicts.filter(v => v === k).length;
  async function send() {
    setBusy(true); setMsg('');
    try { const k = await dispatchFleet(jobId); setMsg(`The fleet is on its way to ${n(k, 'patch', 'patches')}. Observations appear here as the drones report.`); }
    catch (e) { setMsg(e instanceof Error ? e.message : 'The fleet could not be dispatched.'); }
    finally { setBusy(false); }
  }

  return <section className="report-section" id="r-field">
    <div className="section-head"><h2>Field verification</h2>
      <span className={`field-status ${live && dispatched ? 'live' : ''}`}>{live && dispatched ? <><i aria-hidden="true" />Live · the fleet is verifying</> : live ? 'From the fleet’s field notes'
        : evidence?.recorded_at ? `Recorded ${evidence.recorded_at.replace('T', ' ').replace('Z', ' UTC')}` : error ? 'Fleet unavailable' : 'Connecting to the fleet…'}</span></div>
    <p className="lede">The radar says how much the ground changed, not what changed. Each patch’s co- and cross-polarized change is read as hypotheses, and drones fly to the patch to check them: every look is judged against the hypothesis from measured layers at the ground point under the camera.</p>
    {observed.length > 0 ? <p className="field-summary">
      <b>{n(looks, 'observation')}</b> by {n(aircraft.length || new Set(observed.flatMap(h => rows.get(`patch-${h.rank}`)!.aircraft)).size, 'aircraft', 'aircraft')} over {observed.length} of {n(spots.length, 'patch', 'patches')}:
      {' '}{[count('supported') && `${count('supported')} supported`, count('contradicted') && `${count('contradicted')} contradicted`, count('mixed') && `${count('mixed')} mixed`,
        count('alternative explanation') && `${count('alternative explanation')} better explained otherwise`, count('inconclusive') && `${count('inconclusive')} inconclusive`].filter(Boolean).join(', ')}.
      {' '}<span className="field-totals">Individual looks: {totals.supports} support, {totals.contradicts} contradict, {totals.inconclusive} inconclusive.</span>
      {canDispatch && !dispatched && <span className="field-again"><button className="field-btn" disabled={busy} onClick={() => { void send(); }}><Plane size={13} />{busy ? 'Dispatching…' : 'Send the fleet to look again'}</button>{msg && <small>{msg}</small>}</span>}
    </p> : <div className="field-empty">
      <p>{dispatched ? 'The fleet is on its way to these patches. Observations appear here as each aircraft reaches its patch and looks.' : 'No aircraft has looked at these patches yet.'}</p>
      {canDispatch && !dispatched && <button className="field-btn primary" disabled={busy} onClick={() => { void send(); }}><Plane size={14} />{busy ? 'Dispatching…' : 'Send drones to verify'}</button>}
      {msg && <p className="field-msg">{msg}</p>}
    </div>}
    {simulated.length > 0 && <p className="field-disclosure">Flown by {simulated.map(o => SOURCE[o] ?? o).join(' and ')}. These aircraft fly real mission plans over this area, but their cameras render satellite imagery and terrain, so a look samples Sentinel-2, ESA WorldCover and Landsat at the ground point it covers; it is not on-site photography.</p>}

    {observed.length > 0 && <ol className="field-list">{observed.map(h => {
      const row = rows.get(`patch-${h.rank}`) as PatchEvidence; const b = row.brief; const sig = (h as Hotspot & { signature?: Signature }).signature;
      const consistent = (b.consistent_with?.length ? b.consistent_with : sig?.consistent_with ?? []).slice(0, 2).join(', or ');
      // The latest looks, one line per distinct finding (an orbiting aircraft repeats itself).
      const distinct = row.latest.filter((j, i, all) => all.findIndex(x => x.verdict === j.verdict && x.reasons[0] === j.reasons[0]) === i).slice(0, 3);
      const craft = aircraftFor?.(h.rank, row.aircraft) ?? null;
      return <li key={h.rank} id={`r-field-${h.rank}`} className={`field-patch ${row.overall.replace(/ /g, '-')}`}>
        <PatchMaps bounds={h.bounds} rank={h.rank} tone={h.direction === 'increase' ? 'up' : 'down'} points={row.points ?? []} change={change} after={after} />
        <div className="field-text">
          <h3><span className={`field-rank ${h.direction}`}>{h.rank}</span><span className="field-hyp">{consistent || (h.direction === 'increase' ? 'Brighter return' : 'Darker return')}</span>
            <em className={`field-verdict ${row.overall.replace(/ /g, '-')}`}>{SHORT[row.overall] ?? row.overall}</em></h3>
          <p className="field-meta num">{h.direction === 'increase' ? 'Brighter' : 'Darker'} · {h.area_km2 != null ? `${h.area_km2.toFixed(2)} km²` : '—'} · {signed(h.mean_change_db)} dB{b.confidence ? ` · radar reading ${b.confidence}` : ''}</p>
          {b.question && <p className="field-q"><b>Question</b>{b.question}</p>}
          <p className="field-count"><b>{OVERALL[row.overall] ?? row.overall}</b>: {row.counts.supports} support, {row.counts.contradicts} contradict, {row.counts.inconclusive} inconclusive · {row.aircraft.join(', ')}</p>
          <ul className="field-obs">{distinct.map(j => <li key={j.note_id} className={j.verdict}><i aria-hidden="true" /><span><time className="num">{utc(j.t)}</time> · <b>{j.drone_id}</b> {j.verdict}: {j.reasons[0]}{j.reasons[0]?.endsWith('.') ? '' : '.'}</span></li>)}</ul>
          {row.alternatives?.map(a => <p key={a} className="field-alt">Another explanation recurs: {a}.</p>)}
          <div className="field-actions">
            {craft && onDrone && <button className="field-btn" onClick={() => onDrone(craft.id)}><Video size={13} />{craft.live ? `Watch ${craft.callsign} over patch ${h.rank}` : `Fly with ${craft.callsign}`}</button>}
            {onMap && <button className="field-btn" onClick={() => onMap(h)}><MapPin size={13} />Show on the map</button>}
            {row.latest[0] && <small>Latest look {ago(row.latest[0].t)}</small>}
          </div>
        </div>
      </li>; })}</ol>}

    {waiting.length > 0 && observed.length > 0 && <div className="field-waiting"><h3>Not yet observed</h3><ul>{waiting.map(h => { const row = rows.get(`patch-${h.rank}`); const sig = (h as Hotspot & { signature?: Signature }).signature;
      return <li key={h.rank} id={`r-field-${h.rank}`}><span className={`field-rank ${h.direction}`}>{h.rank}</span><span>{row?.brief.question ?? sig?.question ?? (h.direction === 'increase' ? 'Brighter return' : 'Darker return')}</span>
        {onMap && <button className="field-btn" onClick={() => onMap(h)}><MapPin size={13} />Map</button>}</li>; })}</ul></div>}
    {waiting.length > 0 && observed.length === 0 && <ul className="field-questions">{waiting.map(h => { const sig = (h as Hotspot & { signature?: Signature }).signature;
      return <li key={h.rank} id={`r-field-${h.rank}`}><span className={`field-rank ${h.direction}`}>{h.rank}</span><span><b>{(sig?.consistent_with ?? []).slice(0, 2).join(' or ') || (h.direction === 'increase' ? 'Brighter return' : 'Darker return')}</b>{sig?.question && <> · {sig.question}</>}</span></li>; })}</ul>}

    <p className="caption">{evidence?.method ?? 'Each look inside a patch is judged against the patch’s hypothesis from measured values only.'} Dots on the maps are the ground points the cameras looked at: <span className="key supports">supports</span> <span className="key contradicts">contradicts</span> <span className="key inconclusive">inconclusive</span>.
      {artifactHref && <> <a href={artifactHref} download><Download size={12} /> Patches and observations as GeoJSON</a>.</>}</p>
  </section>;
}
