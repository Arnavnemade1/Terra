import { useState } from 'react';
import { ArrowUpRight, Check, ChevronRight, LoaderCircle } from 'lucide-react';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { artifactUrl, type Hotspot, type Optical } from './Dossier';
import type { Conditions } from './Panels';
import Polarizations, { channel, patchLead, type PolRow } from './Polarizations';
import { api, prettyDate, safeUrl, type Investigation, type Observation } from './types';

export const STAGES = ['discover', 'prepare', 'analyze', 'verify', 'synthesize'];
const STAGE_NAMES = ['Plan and choose radar dates', 'Read the original radar data', 'Measure', 'Check the results', 'Write and check the answer'];
export const ACTIVE = ['queued', 'running', 'discovering', 'preparing', 'analyzing', 'verifying', 'synthesizing'];
export const DEFAULT_QUESTION = 'What changed here between the earliest and latest matching radar dates, and where is the change concentrated?';
export const LENSES = [
  { name: 'Flooding and water', hint: 'Water-like area before and after', question: 'Has open water or flooding changed here? Compare the water-like area between matching dates and show where it changed.' },
  { name: 'Vegetation and clearing', hint: 'Forest, crops and bare ground', question: 'Has the vegetation or land surface changed here? Show where the radar return changed and what could explain it.' },
  { name: 'Volcanic surface', hint: 'New lava, ash and crater change', question: 'Has the volcanic surface changed recently? Show where the radar return changed around the summit and flanks.' },
  { name: 'Urban growth', hint: 'Bright returns from structures', question: 'Has the built-up area grown or changed? Compare very bright radar returns between matching dates and show where.' },
  { name: 'Ice, snow and coast', hint: 'Melt, calving and shoreline', question: 'How has the ice, snow or coastline changed between matching dates, and where is the change concentrated?' },
  { name: 'After a storm or fire', hint: 'Surface change after a hazard', question: 'What surface changes are visible after the most recent hazard here? Show where the radar return changed most.' },
];

export type Narrative = { headline: string; answer: string; evidence: string[]; possible_explanations: string[]; caveats: string[]; next_steps: string[]; model?: string; verification: { numbers_checked: number; unmatched: string[]; status: string } };
export type Plan = { mode: string; interpretation: string; can_answer: string; cannot_answer: string; selection: string; planner: string; model?: string };
type Followup = { question: string; answer: string; verification: Narrative['verification']; model?: string };
export type Context = { radius_km: number; earthquakes?: { magnitude: number; place: string; distance_km: number; direction: string; days_ago: number; url?: string }[];
  events?: { title: string; category: string; distance_km: number; direction: string; last_update: string; url?: string; alert?: string | null; provider?: string }[];
  fires_24h?: { count: number; max_frp_mw: number; nearest_km: number | null }; nisar_frames_4d?: number; unavailable: string[] };
export type Stats = Record<string, number | null | undefined> & { date?: string; histogram?: { edges: number[]; counts: number[] }; hotspots?: Hotspot[] };
export type Result = NonNullable<Investigation['result']> & { narrative?: Narrative | null; narrative_note?: string; narrative_status?: string; enrichment_status?: string;
  optical_evidence?: Optical[]; surface_conditions?: Conditions; timings?: { first_result_seconds?: number; interpreted_answer_seconds?: number } };

// True minus sign (U+2212), as typeset figures use, rather than a hyphen.
export const num = (v: unknown, digits = 2, signed = false) => typeof v === 'number' && Number.isFinite(v)
  ? `${signed && v > 0 ? '+' : ''}${v.toLocaleString('en', { maximumFractionDigits: digits, minimumFractionDigits: digits })}`.replace(/^-/, '−') : '—';
export const minus = (s: string) => s.replace(/(^|[\s(*_~>])-(?=\d)/g, '$1−');
const md = (text: string) => ({ __html: DOMPurify.sanitize(marked.parse(minus(text), { async: false }) as string) });
export const seconds = (ms: number) => ms < 60_000 ? `${Math.max(0, Math.round(ms / 1000))} s` : `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;

export function Steps({ job, now }: { job: Investigation; now: number }) {
  const at = (s: string) => job.events.find(e => e.stage === s)?.at;
  return <ol className="steps">{STAGES.map((s, i) => {
    const started = at(s); const next = STAGES.slice(i + 1).map(at).find(Boolean);
    const done = Boolean(next) || STAGES.indexOf(job.stage) > i; const current = !done && job.stage === s;
    const ms = started ? (next ? Date.parse(next) : now) - Date.parse(started) : 0;
    return <li key={s} className={`${done ? 'done' : ''} ${current ? 'now' : ''}`}>
      <span className="tick">{done ? <Check size={10} strokeWidth={3} /> : current ? <span className="pulse" /> : null}</span>
      <span className="step-name">{STAGE_NAMES[i]}{current && <small>{job.events.filter(e => e.stage === s).at(-1)?.message}</small>}</span>
      <span className="step-time num">{started && (done || current) ? `${(ms / 1000).toFixed(1)} s` : ''}</span>
    </li>;
  })}</ol>;
}

export function ContextBlock({ context }: { context: Context | null }) {
  if (!context) return <section className="block"><span className="label">Around this area</span><p className="hint">Checking live sources…</p></section>;
  const rows: { text: string; sub: string; url?: string; color?: string }[] = [];
  for (const q of (context.earthquakes || []).filter(q => q.distance_km <= context.radius_km * 2).slice(0, 3))
    rows.push({ text: `M${q.magnitude.toFixed(1)} earthquake, ${q.distance_km.toLocaleString('en')} km ${q.direction}`, sub: `${q.place} · ${q.days_ago < 1 ? 'today' : `${Math.round(q.days_ago)} days ago`}`, url: q.url, color: '#ece6d6' });
  for (const e of (context.events || []).slice(0, 4))
    rows.push({ text: e.title, sub: `${e.category}${e.alert && e.alert !== 'green' ? ` · ${e.alert} alert` : ''} · ${e.distance_km.toLocaleString('en')} km ${e.direction} · ${e.last_update}`, url: e.url, color: '#e5634f' });
  const f = context.fires_24h;
  return <section className="block">
    <span className="label">Around this area</span>
    <ul className="context">
      <li><b>{context.nisar_frames_4d ? `NISAR imaged this area ${context.nisar_frames_4d} time${context.nisar_frames_4d > 1 ? 's' : ''} in the last 4 days` : 'No NISAR pass over this area in the last 4 days'}</b></li>
      {f && <li><b>{f.count ? `${f.count.toLocaleString('en')} active fire detection${f.count > 1 ? 's' : ''} within ${context.radius_km} km today` : `No active fires within ${context.radius_km} km today`}</b>{f.count > 0 && <span>Nearest {f.nearest_km} km · strongest {f.max_frp_mw} MW</span>}</li>}
      {rows.map((l, i) => <li key={i}><b>{l.color && <i className="dot" style={{ background: l.color }} />}{l.text}</b><span>{l.sub}{l.url && safeUrl(l.url) && <> · <a className="link" href={safeUrl(l.url)} target="_blank" rel="noreferrer">source</a></>}</span></li>)}
      {!rows.length && <li><span>No hazards or M2.5+ earthquakes reported nearby this week.</span></li>}
    </ul>
    {context.unavailable.length > 0 && <p className="hint">Unavailable right now: {context.unavailable.join(', ')}.</p>}
  </section>;
}

/** The question composer, lenses and the optional manual choice of radar dates. */
export function Composer({ question, setQuestion, questionRef, onLaunch, submitting, blocker, warning, manual, setManual, observations, loading, catalogError, onRetry, selectedIds, onToggle, analysis, setAnalysis, manualBlocker, onDetail }: {
  question: string; setQuestion: (q: string) => void; questionRef: React.RefObject<HTMLTextAreaElement | null>; onLaunch: (auto: boolean) => void; submitting: boolean;
  blocker: string; warning: string; manual: boolean; setManual: (v: boolean) => void; observations: Observation[]; loading: boolean; catalogError: string; onRetry: () => void;
  selectedIds: string[]; onToggle: (o: Observation) => void; analysis: 'change' | 'backscatter'; setAnalysis: (a: 'change' | 'backscatter') => void; manualBlocker: string; onDetail: (o: Observation) => void;
}) {
  return <>
    <section className="compose">
      <label htmlFor="question">Ask about this area</label>
      <div className="composer">
        <textarea id="question" ref={questionRef} value={question} onChange={e => setQuestion(e.target.value)} rows={3} maxLength={2000}
          placeholder="What changed here, and where?" onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !blocker) onLaunch(!manual); }} />
        {!manual && <div className="composer-bar">
          <span className="hint">{blocker || '⌘ Enter'}</span>
          <button className="primary" disabled={Boolean(blocker) || submitting} onClick={() => onLaunch(true)}>{submitting ? <><LoaderCircle size={14} className="spin" /> Starting</> : 'Investigate'}</button>
        </div>}
      </div>
      {warning && <p className={`hint ${blocker ? 'warn' : ''}`}>{warning}</p>}
      <ul className="lenses" aria-label="Start from a lens">{LENSES.map(l => <li key={l.name}>
        <button className={question === l.question ? 'on' : ''} onClick={() => { setQuestion(l.question); questionRef.current?.focus(); }}><b>{l.name}</b><span>{l.hint}</span></button></li>)}</ul>
      <p className="hint">Terra picks matching NISAR radar dates, reads only this area from the original products, measures the change and writes an answer whose every number is checked; usually 10–20 seconds.</p>
    </section>
    <section className="block">
      <button className="disclose" onClick={() => setManual(!manual)} aria-expanded={manual}>
        <span>Choose the radar dates yourself</span><span className="quiet">{loading ? 'Searching…' : `${observations.length} available`}</span><ChevronRight size={14} className={manual ? 'turned' : ''} /></button>
      {manual && <div className="manual">
        <div className="seg wide">
          <button className={analysis === 'change' ? 'on' : ''} onClick={() => setAnalysis('change')}>Compare two dates</button>
          <button className={analysis === 'backscatter' ? 'on' : ''} onClick={() => setAnalysis('backscatter')}>One date</button>
        </div>
        {loading ? <p className="hint">Searching the NASA/ASF archive for this area…</p>
          : catalogError ? <p className="hint warn">{catalogError} <button className="link" onClick={onRetry}>Try again</button></p>
          : !observations.length ? <p className="hint">NISAR has no GCOV products here yet. Try a nearby area or a featured site.</p>
          : <ul className="acq">{[...observations].sort((a, b) => b.date.localeCompare(a.date)).map(o => {
              const on = selectedIds.includes(o.id);
              return <li key={o.id} className={on ? 'on' : ''}>
                <button className="acq-main" onClick={() => onToggle(o)} aria-pressed={on}>
                  <span className="box">{on && <Check size={11} strokeWidth={3} />}</span>
                  <span className="num">{new Date(o.date).toISOString().slice(0, 10)}</span>
                  <span className="acq-meta">Track {o.track ?? '—'} {(o.direction || '').toLowerCase().slice(0, 4)}. · {(o.polarizations || []).join('/') || '—'}</span>
                  <span className="acq-tag">{o.local ? 'Local beta' : (o.maturity || '').toLowerCase()}</span>
                </button>
                <button className="icon-btn" onClick={() => onDetail(o)} aria-label="Product details" title="Product details"><ArrowUpRight size={13} /></button>
              </li>;
            })}</ul>}
        <p className="hint">{analysis === 'change' ? 'Pick two dates on the same track, direction and frame. Mismatched pairs are refused rather than resampled.' : 'Pick one date.'}</p>
        <button className="primary" disabled={Boolean(manualBlocker) || submitting} onClick={() => onLaunch(false)}>{submitting ? 'Starting…' : 'Investigate these dates'}</button>
        {manualBlocker && <p className="hint">{manualBlocker}</p>}
      </div>}
    </section>
  </>;
}

/** The written answer, key figures, the hotspot list and follow-up questions. */
export function Results({ job, result: r, writer, plan, focusSpot, onRewritten, onSpot, onReport }: {
  job: Investigation; result: Result; writer: string | null; plan?: Plan; focusSpot: number | null;
  onRewritten: (n: Narrative) => void; onSpot: (h: Hotspot) => void; onReport: () => void;
}) {
  const m = r.measurements as { observations?: Stats[]; change?: Stats };
  const change = m.change; const obs = m.observations || [];
  const n = r.narrative;
  const [followups, setFollowups] = useState<Followup[]>((job as Investigation & { followups?: Followup[] }).followups || []);
  const [ask, setAsk] = useState(''); const [asking, setAsking] = useState(false); const [askError, setAskError] = useState('');
  const [writing, setWriting] = useState(false); const [writeError, setWriteError] = useState('');
  const fallback = !n || n.model === 'deterministic computed summary' || r.narrative_status === 'computed_summary';
  const optical = r.optical_evidence || [];
  const before = optical.find(o => o.kind === 'before'), after = optical.find(o => o.kind === 'after');
  const radar = (r.layers || []).filter(l => l.id.startsWith('backscatter-'));
  const pending = r.narrative_status === 'pending';
  async function rewrite() {
    setWriting(true); setWriteError('');
    try { onRewritten(await api<Narrative>(`/investigations/${job.id}/narrate`, { method: 'POST' })); }
    catch (e) { setWriteError(e instanceof Error ? e.message : 'The answer could not be written.'); } finally { setWriting(false); }
  }
  async function submit() {
    if (ask.trim().length < 3) return; setAsking(true); setAskError('');
    try { const a = await api<Followup>(`/investigations/${job.id}/ask`, { method: 'POST', body: JSON.stringify({ question: ask.trim() }) }); setFollowups(f => [...f, a]); setAsk(''); }
    catch (e) { setAskError(e instanceof Error ? e.message : 'The question could not be answered.'); } finally { setAsking(false); }
  }
  const traced = (v: Narrative['verification']) => v.unmatched.length === 0 ? `All ${v.numbers_checked} numbers traced to computed values` : `${v.unmatched.length} of ${v.numbers_checked} numbers could not be traced`;
  const thumbs = before && after ? [[before.artifact_name, 'Before', before.date], [after.artifact_name, 'After', after.date]]
    : radar.length === 2 ? radar.map((l, i) => [l.path.split('/').pop()!, i ? 'After' : 'Before', l.date || '']) : [];

  return <div className="results">
    {pending && !n && <p className="writing"><LoaderCircle size={13} className="spin" /> Measurements are ready; the answer is being written and checked…</p>}
    {n && <div className="answer">
      <h2>{minus(n.headline)}</h2>
      <div className="prose" dangerouslySetInnerHTML={md(n.answer)} />
      <p className={`trace ${n.verification.unmatched.length ? 'warn' : ''}`}><Check size={12} /> {traced(n.verification)}{n.model && n.model !== 'deterministic computed summary' ? ` · written by ${n.model.replace(/-\d{4}-\d{2}-\d{2}$/, '')}` : ''}</p>
    </div>}
    {!n && !pending && r.narrative_note && <p className="hint">{r.narrative_note}</p>}
    {fallback && writer && !pending && <div className="rewrite">
      <p className="hint">This summary comes straight from the measurements. {writer} can write a fuller explanation; every number and date is checked first.</p>
      <button className="secondary" disabled={writing} onClick={() => void rewrite()}>{writing ? 'Writing…' : 'Write a fuller answer'}</button>
      {writeError && <p className="hint warn">{writeError}</p>}
    </div>}

    {thumbs.length === 2 && <button className="pair" onClick={onReport} aria-label="Open the before and after comparison">
      {thumbs.map(([name, label, date]) => <figure key={label}><img src={artifactUrl(job, name)} alt={`${label}, ${prettyDate(date)}`} className={before ? '' : 'pixelated'} /><figcaption><b>{label}</b> {prettyDate(date)}</figcaption></figure>)}
      <span className="pair-cta">{before ? 'True colour' : 'Radar'} · compare in the report <ArrowUpRight size={12} /></span>
    </button>}
    {!before && r.enrichment_status === 'pending' && <p className="hint">Looking for clear true-colour images of the same dates…</p>}

    {change ? <div className="figures">
      <div><span>Median change</span><b className="num">{num(change.median_db, 2, true)} dB</b></div>
      <div><span>Brighter ≥ {num(change.threshold_db, 0)} dB</span><b className="num">{num(change.increasing_km2, 1)} km²</b></div>
      <div><span>Darker ≥ {num(change.threshold_db, 0)} dB</span><b className="num">{num(change.decreasing_km2, 1)} km²</b></div>
      <div><span>Water-like area</span><b className="num">{num(change.low_return_before_km2, 1)} → {num(change.low_return_after_km2, 1)} km²</b></div>
    </div> : obs[0] && <div className="figures">
      <div><span>Median backscatter</span><b className="num">{num(obs[0].median_db)} dB</b></div>
      <div><span>Water-like area</span><b className="num">{num(obs[0].low_return_km2, 1)} km²</b></div>
      <div><span>Very bright area</span><b className="num">{num(obs[0].high_return_km2, 1)} km²</b></div>
      <div><span>Area measured</span><b className="num">{num(obs[0].valid_projected_area_km2, 0)} km²</b></div>
    </div>}

    {change && <Polarizations rows={(change as unknown as { polarizations?: PolRow[] }).polarizations} leading={(change as unknown as { leading_polarization?: string | null }).leading_polarization} hotspots={change.hotspots} />}

    {(change?.hotspots?.length || 0) > 0 && <div className="spots">
      <span className="label">Where it changed most</span>
      <ol>{change!.hotspots!.map(h => <li key={h.rank}><button className={focusSpot === h.rank ? 'on' : ''} onClick={() => onSpot(h)}>
        <span className={`rank ${h.direction === 'increase' ? 'up' : 'down'}`}>{h.rank}</span>
        <span className="spot-text"><b>{h.direction === 'increase' ? 'Brighter' : 'Darker'} by {num(Math.abs(h.mean_change_db ?? NaN), 1)} dB on average</b>
          <span className="num">{num(h.area_km2, 2)} km² · {h.centre[0].toFixed(3)}, {h.centre[1].toFixed(3)}{(l => l ? ` · clearer in ${l.lead} (${l.z.toFixed(1)}σ vs ${l.z2.toFixed(1)}σ)` : Object.keys(h.by_polarization ?? {}).length > 1 ? ` · ${Object.keys(h.by_polarization!).map(channel).join(' ≈ ')}` : '')(patchLead(h))}</span></span>
      </button></li>)}</ol>
    </div>}

    {n && n.possible_explanations.length > 0 && <><span className="label">What could explain it</span><ul className="bullets">{n.possible_explanations.map((x, i) => <li key={i}>{minus(x)}</li>)}</ul></>}
    {n && n.next_steps.length > 0 && <><span className="label">Worth checking next</span><ul className="bullets">{n.next_steps.map((x, i) => <li key={i}>{minus(x)}</li>)}</ul></>}

    {n && <div className="followups">
      <span className="label">Ask a follow-up</span>
      {followups.map((f, i) => <div key={i} className="qa"><p className="q">{f.question}</p><div className="prose" dangerouslySetInnerHTML={md(f.answer)} />
        <p className={`trace ${f.verification.unmatched.length ? 'warn' : ''}`}><Check size={12} /> {traced(f.verification)}</p></div>)}
      <div className="ask-row"><input className="field" value={ask} onChange={e => setAsk(e.target.value)} placeholder="e.g. What could make patch 1 brighter?" onKeyDown={e => { if (e.key === 'Enter') void submit(); }} maxLength={600} />
        <button className="secondary" disabled={asking || ask.trim().length < 3} onClick={() => void submit()}>{asking ? <LoaderCircle size={13} className="spin" /> : 'Ask'}</button></div>
      {askError && <p className="hint warn">{askError}</p>}
    </div>}

    {plan && <details className="more"><summary>How this was planned</summary><p>{plan.interpretation} {plan.selection}</p><p className="quiet">Can answer: {plan.can_answer} Cannot: {plan.cannot_answer}</p></details>}
    <div className="result-actions">
      <button className="primary" onClick={onReport}>Open the report</button>
      <a className="link" href={`/api/investigations/${job.id}/artifacts/public-result.json`} download>Download data</a>
    </div>
  </div>;
}
