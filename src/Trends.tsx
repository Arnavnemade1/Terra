import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Overlay } from './Globe';
import { Card, KeyValues } from './Panels';
import { api, prettyDate, type Bounds } from './types';
import './trends.css';

type Summary = {
  pixels?: number;
  median_slope_db_per_year?: number | null;
  slope_iqr_db_per_year?: number | null;
  significant_pixels?: number;
  significant_fraction?: number | null;
  increasing_share_of_significant?: number | null;
  decreasing_share_of_significant?: number | null;
  increasing_km2?: number | null;
  decreasing_km2?: number | null;
};
type AgentFit = {
  index: number;
  delta?: number | null;
  cv_mae_db?: Record<string, number> | null;
  pixels_fit?: number | null;
  median_slope?: number | null;
  significant_share?: number | null;
};
type AgentState = { index: number; status?: string | null; delta?: number | null };
type TrendLayer = { id: string; path?: string; bounds?: Bounds; units?: string; min?: number; max?: number };
type TrendResult = {
  id?: string;
  dates?: string[];
  t_span_years?: number | null;
  group?: { track?: number | string | null; direction?: string | null; frame?: number | string | null; candidates?: number | null };
  qubo?: { energy?: number | null; optimum_energy?: number | null; solver?: string | null; chosen?: number | null; candidates?: number | null };
  quantized_dataset?: {
    bytes_uint8?: number | null;
    bytes_float32?: number | null;
    step_db?: number | null;
    max_abs_error_db?: number | null;
    rms_error_db?: number | null;
    effect?: { delta?: number | null; median_abs_slope_diff_db_per_year?: number | null } | null;
  };
  agents?: AgentFit[];
  summary?: Summary;
  layers?: TrendLayer[];
  limitations?: string[];
};
type Snapshot = {
  id?: string;
  status?: string;
  stage?: string;
  message?: string;
  dates?: string[];
  agents?: AgentState[];
  result?: TrendResult | null;
};

const MISSING = 'Not available';
const PASS_CHOICES = [6, 8, 10, 12];
const LAYER_CHOICES: { id: 'trend' | 'trend-significant'; label: string }[] = [
  { id: 'trend', label: 'All pixels' },
  { id: 'trend-significant', label: 'Significant only' },
];
const STAGE: Record<string, string> = {
  planning: 'Planning the dates',
  stacking: 'Building the quantized stack',
  agents: 'Agents fitting the tiles',
  publishing: 'Publishing the layers',
  done: 'Done',
};
const AGENT_LABEL: Record<string, string> = { queued: 'Queued', fitting: 'Fitting', done: 'Done' };
const IDENTIFIER = /^trend-[0-9a-f]{12}$/;

const fmt = (value: unknown, digits = 2) => typeof value === 'number' && Number.isFinite(value)
  ? value.toLocaleString('en', { minimumFractionDigits: digits, maximumFractionDigits: digits }).replace(/^-/, '−')
  : MISSING;
const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value)
  ? Math.round(value).toLocaleString('en')
  : MISSING;
const share = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : MISSING;
const megabytes = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${(value / 1e6).toFixed(1)} MB` : MISSING;
const text = (value: unknown) => typeof value === 'string' && value.trim() ? value : MISSING;

function readSaved(key: string): string | null {
  try {
    const stored = localStorage.getItem(key);
    return stored && IDENTIFIER.test(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeSaved(key: string, id: string | null) {
  try {
    if (id) localStorage.setItem(key, id);
    else localStorage.removeItem(key);
  } catch {
    // Storage can be blocked; the analysis still runs, it is just not remembered.
  }
}

function cvError(agent: AgentFit): number | null {
  const delta = agent.delta;
  if (typeof delta !== 'number') return null;
  const values = agent.cv_mae_db ?? {};
  const match = Object.keys(values).find(key => key !== 'inf' && Math.abs(Number(key) - delta) < 1e-6);
  return match !== undefined && Number.isFinite(values[match]) ? values[match] : null;
}

/**
 * The long-term trend over the area: a robust straight-line fit of radar brightness per pixel, run as one durable
 * coordinator with a child agent per tile. The panel starts an analysis, follows its stages, and shows the result.
 */
export default function Trends({ bbox, name, onLayer }: { bbox: Bounds; name: string; onLayer: (overlay: Overlay | null) => void }) {
  const [id, setId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [passes, setPasses] = useState(10);
  const [show, setShow] = useState(false);
  const [layer, setLayer] = useState<'trend' | 'trend-significant'>('trend');
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const areaKey = `terra.trend.${bbox.map(value => value.toFixed(3)).join(',')}`;

  // A different area forgets the previous analysis and picks up the one remembered for the new area.
  useEffect(() => {
    const saved = readSaved(areaKey);
    setSnapshot(null);
    setShow(false);
    setLayer('trend');
    setError('');
    setId(saved);
  }, [areaKey]);

  // Follow the analysis every 2 s until it is done or has stopped, and clean up on unmount or a change of area.
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    let stopped = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const next = await api<Snapshot>(`/trends/${id}`, { signal: controller.signal });
        if (stopped) return;
        setSnapshot(next);
        setError('');
        if (next.status === 'done' || next.status === 'failed') return;
        timer = window.setTimeout(() => void tick(), 2000);
      } catch (caught) {
        if (stopped || controller.signal.aborted) return;
        const message = caught instanceof Error ? caught.message : 'The analysis did not answer.';
        if (message === 'Unknown trend analysis.') {
          writeSaved(areaKey, null);
          setId(null);
          setSnapshot(null);
          setError('');
          return;
        }
        setError(message);
        timer = window.setTimeout(() => void tick(), 2000);
      }
    };
    void tick();
    return () => {
      stopped = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [id, areaKey]);

  const result = snapshot?.result ?? null;
  const status: 'idle' | 'loading' | 'running' | 'done' | 'failed' = !id
    ? 'idle'
    : !snapshot
      ? 'loading'
      : snapshot.status === 'done'
        ? (result ? 'done' : 'failed')
        : snapshot.status === 'failed' ? 'failed' : 'running';

  // A new result is drawn on the map straight away; the toggle and the layer choice can change it afterwards.
  useEffect(() => {
    if (result) setShow(true);
  }, [result]);

  const dates = result?.dates ?? snapshot?.dates ?? [];
  const activeLayer = useMemo(() => (result?.layers ?? []).find(entry => entry.id === layer), [result, layer]);
  const resultId = result?.id ?? id;
  useEffect(() => {
    if (!resultId || !show || !activeLayer?.bounds) { onLayer(null); return; }
    onLayer({ id: 'trend', url: `/api/trends/${resultId}/files/${layer}.png`, bounds: activeLayer.bounds, alpha: 0.85, show: true });
  }, [resultId, show, activeLayer, layer, onLayer]);
  useEffect(() => () => { onLayer(null); }, [onLayer]);

  const start = useCallback(async () => {
    setError('');
    setStarting(true);
    try {
      const created = await api<{ id?: string }>('/trends', {
        method: 'POST',
        body: JSON.stringify({ name, bbox, max_dates: passes }),
      });
      if (!created.id || !IDENTIFIER.test(created.id)) throw new Error('The analysis did not return an identifier.');
      writeSaved(areaKey, created.id);
      setSnapshot(null);
      setShow(false);
      setId(created.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The analysis could not start.');
    } finally {
      setStarting(false);
    }
  }, [areaKey, bbox, name, passes]);

  const summary = result?.summary ?? {};
  const group = result?.group ?? {};
  const qubo = result?.qubo ?? {};
  const quantized = result?.quantized_dataset ?? {};
  const effect = quantized.effect ?? null;
  const doneAgents = result?.agents ?? [];
  const liveAgents = snapshot?.agents ?? [];
  const limitations = result?.limitations ?? [];
  const firstDate = dates[0];
  const lastDate = dates.length > 1 ? dates[dates.length - 1] : undefined;
  const spanYears = result?.t_span_years;
  const months = typeof spanYears === 'number' && Number.isFinite(spanYears) ? `${(spanYears * 12).toFixed(1)} months` : MISSING;
  const passCount = dates.length === 1 ? '1 pass' : `${count(dates.length)} passes`;
  const passWindow = firstDate && lastDate
    ? `${passCount} · ${prettyDate(firstDate)} to ${prettyDate(lastDate)} · ${months}`
    : firstDate ? `${passCount} · ${prettyDate(firstDate)}` : MISSING;
  const geometry = group.track !== undefined && group.track !== null
    ? `Track ${String(group.track)}${group.direction ? `, ${group.direction.toLowerCase()}` : ''}`
    : MISSING;
  const median = summary.median_slope_db_per_year;
  const iqr = summary.slope_iqr_db_per_year;
  const stageLabel = snapshot?.stage === 'acquiring'
    ? (dates.length ? `Reading ${dates.length} passes` : 'Reading the passes')
    : snapshot?.stage ? (STAGE[snapshot.stage] ?? 'Working') : 'Planning the dates';
  const chosen = typeof qubo.chosen === 'number' ? qubo.chosen : dates.length;
  const candidates = typeof qubo.candidates === 'number' ? qubo.candidates : group.candidates;
  const optimum = qubo.optimum_energy;
  const matches = typeof optimum === 'number' && Number.isFinite(optimum);

  return <Card title="Trend over the past year" sub="NISAR, same viewing geometry">
    <div className="trends">
      {status === 'idle' && <>
        <p className="trend-lede">Fits a straight-line trend in radar brightness through up to {passes} passes of the same orbit, per pixel, and shows where the ground got steadily brighter or darker.</p>
        <div className="trend-start">
          <div className="seg trend-passes" role="group" aria-label="Passes to use">
            {PASS_CHOICES.map(option => <button key={option} type="button" className={option === passes ? 'on' : ''} aria-pressed={option === passes} onClick={() => setPasses(option)}>{option}</button>)}
          </div>
          <button type="button" className="primary" onClick={() => void start()} disabled={starting}>{starting ? 'Starting' : 'Fit the trend'}</button>
        </div>
        <p className="hint">Up to {passes} passes from one orbit, spread over about a year.</p>
        {error && <p className="hint warn">{error}</p>}
      </>}

      {status === 'loading' && <>
        <p className="trend-stage"><span className="trend-dot running" aria-hidden="true" />Getting the analysis</p>
        <p className="hint">The panel checks the durable analysis every 2 s.</p>
        {error && <p className="hint warn">{error}</p>}
      </>}

      {status === 'running' && <>
        <p className="trend-stage"><span className="trend-dot running" aria-hidden="true" />{stageLabel}</p>
        {snapshot?.message && <p className="hint">{snapshot.message}</p>}
        <div className="trend-field">
          <span className="trend-label">Passes</span>
          {dates.length
            ? <ul className="trend-dates">{dates.map(date => <li key={date}><span className="num">{prettyDate(date, true)}</span></li>)}</ul>
            : <p className="hint">Not chosen yet.</p>}
        </div>
        <div className="trend-field">
          <span className="trend-label">Tile agents</span>
          {liveAgents.length
            ? <ul className="trend-agents">{liveAgents.map(agent => {
              const state = agent.status === 'fitting' || agent.status === 'done' ? agent.status : 'queued';
              return <li key={agent.index} className={`trend-agent ${state}`}>
                <b className="num">{agent.index + 1}</b>
                <span>{AGENT_LABEL[state] ?? text(agent.status)}</span>
                <em>{typeof agent.delta === 'number' ? `delta ${fmt(agent.delta, 2)}` : state === 'done' ? 'no data' : ''}</em>
              </li>;
            })}</ul>
            : <p className="hint">The tile agents start once the shared stack is built.</p>}
        </div>
        {error && <p className="hint warn">{error}</p>}
      </>}

      {status === 'failed' && <>
        <div className="alert warn" role="alert"><span>{snapshot?.message || 'The analysis stopped without a reason.'}</span></div>
        <div className="trend-actions">
          <button type="button" className="primary" onClick={() => void start()} disabled={starting}>{starting ? 'Starting' : 'Try again'}</button>
        </div>
        {error && <p className="hint warn">{error}</p>}
      </>}

      {status === 'done' && result && <>
        <div className="trend-head">
          <p className="trend-figure">{typeof median === 'number' && Number.isFinite(median)
            ? <>Median trend <b className="num">{typeof median === 'number' && median > 0 ? '+' : ''}{fmt(median, 2)}</b> dB/yr</>
            : <>Median trend <b>{MISSING}</b></>}</p>
          <p className="trend-iqr">{typeof iqr === 'number' && Number.isFinite(iqr)
            ? <>IQR <span className="num">{fmt(iqr, 2)}</span> dB/yr</>
            : <>IQR {MISSING}</>}</p>
        </div>

        <KeyValues rows={[
          ['Steadily brighter', <>{fmt(summary.increasing_km2, 2)} km² · {share(summary.increasing_share_of_significant)} of significant pixels</>],
          ['Steadily darker', <>{fmt(summary.decreasing_km2, 2)} km² · {share(summary.decreasing_share_of_significant)} of significant pixels</>],
          ['Passes used', passWindow],
          ['Viewing geometry', geometry],
          ['Significance', <>{count(summary.significant_pixels)} pixels ({share(summary.significant_fraction)}), p &lt; 0.05</>],
        ]} />

        <div className="trend-map">
          <button type="button" className={`mini-btn ${show ? 'on' : ''}`} aria-pressed={show} onClick={() => setShow(value => !value)}>{show ? 'Hide from map' : 'Show on map'}</button>
          <div className="seg trend-layer" role="group" aria-label="Trend on the map">
            {LAYER_CHOICES.map(choice => <button key={choice.id} type="button" className={choice.id === layer ? 'on' : ''} aria-pressed={choice.id === layer} onClick={() => setLayer(choice.id)}>{choice.label}</button>)}
          </div>
        </div>
        <p className="hint">{show
          ? (layer === 'trend' ? 'Showing the fitted trend of every pixel.' : 'Showing the trend only where the fit is significant.')
          : 'The trend layer is hidden. It can be shown on the map at any time.'}</p>

        <div className="trend-legend">
          <span className="ramp div" aria-hidden="true" />
          <div className="ticks"><span className="num">−3</span><span>dB/yr</span><span className="num">+3</span></div>
          <div className="trend-words"><span>Darker</span><span>Brighter</span></div>
        </div>

        <details className="trend-method">
          <summary>How this was computed</summary>
          <div className="trend-method-body">
            <section>
              <h4>Linear regression</h4>
              <p>A robust (Huber) straight line is fitted per pixel through {count(dates.length)} passes, giving a slope in dB/yr with a standard error and a t-test. {count(summary.pixels)} pixels were fitted.</p>
            </section>
            <section>
              <h4>Quantized dataset</h4>
              <p>The shared stack is stored as 8-bit codes in {fmt(quantized.step_db, 1)} dB steps: {megabytes(quantized.bytes_uint8)} instead of {megabytes(quantized.bytes_float32)}. Rounding moves a value by at most {fmt(quantized.max_abs_error_db, 1)} dB; the median effect on fitted slopes is {fmt(effect?.median_abs_slope_diff_db_per_year, 2)} dB/yr.</p>
            </section>
            <section>
              <h4>Tuning</h4>
              <p>The Huber threshold was tuned per tile by leave-one-pass-out cross-validation. Each agent&apos;s chosen threshold and its cross-validated error:</p>
              {doneAgents.length
                ? <ul className="trend-tuning">{doneAgents.map(agent => <li key={agent.index}>
                  <span>Agent {agent.index + 1}</span>
                  {typeof agent.delta === 'number'
                    ? <><span className="num">delta {fmt(agent.delta, 2)}</span><span className="num">error {fmt(cvError(agent), 2)} dB</span></>
                    : <span className="trend-empty-tile">No radar data in this tile</span>}
                </li>)}</ul>
                : <p>{MISSING}</p>}
            </section>
            <section>
              <h4>Agent swarm</h4>
              <p>{count(doneAgents.length)} tile agents ran as durable Temporal child workflows under one coordinator, covering {count(summary.pixels)} pixels in all.</p>
            </section>
            <section>
              <h4>Date selection</h4>
              <p>{count(chosen)} of {count(candidates)} passes were selected as a QUBO, the form a quantum annealer solves. Solved here{qubo.solver ? ` by ${qubo.solver}` : ''}; the energy is {fmt(qubo.energy, 2)}. {matches ? 'This matches the exhaustive optimum for these candidates.' : 'An exhaustive check was not run for this many candidates.'} No quantum hardware was used.</p>
            </section>
            <section>
              <h4>Limitations</h4>
              {limitations.length ? <ul className="bullets">{limitations.map(item => <li key={item}>{item}</li>)}</ul> : <p>{MISSING}</p>}
            </section>
          </div>
        </details>

        <div className="trend-actions">
          <button type="button" className="secondary" onClick={() => void start()} disabled={starting}>{starting ? 'Starting' : 'Fit again'}</button>
          <a className="link" href={`/api/trends/${resultId ?? ''}/files/trend.tif`} download>Download the GeoTIFF (trend.tif)</a>
        </div>
        {error && <p className="hint warn">{error}</p>}
      </>}
    </div>
  </Card>;
}
