import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUpRight, Download, Printer, X } from 'lucide-react';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { ConditionsGrid, type Conditions } from './Panels';
import Compare, { GeoFrame, type Box, type Frame } from './Compare';
import type { Bounds, Investigation, Layer } from './types';
import { prettyDate, safeUrl } from './types';

type Input = { filename: string; sha256: string; metadata: Record<string, unknown>; catalog?: Record<string, unknown> };
type Narrative = { headline?: string; answer?: string; evidence?: string[]; possible_explanations?: string[]; caveats?: string[]; next_steps?: string[]; model?: string; verification?: { numbers_checked: number; unmatched: string[] } };
export type Optical = { kind: 'before' | 'after' | 'recent'; date: string; sensor: string; resolution_m: number; source_url: string; artifact_name: string; bounds: Bounds;
  quality?: { coverage_pct?: number; obscured_pct?: number; snow_ice_pct?: number }; sha256: string; temporal_offset_days?: number };
export type Hotspot = { rank: number; direction: 'increase' | 'decrease'; pixels: number; area_km2: number | null; mean_change_db: number | null; peak_change_db: number | null; centre: [number, number]; bounds: Bounds;
  by_polarization?: Record<string, { mean_change_db: number; standardized_change: number }>; leading_polarization?: string | null };
type Stats = Record<string, number | null | undefined> & { date?: string; hotspots?: Hotspot[] };
type Full = NonNullable<Investigation['result']> & { narrative?: Narrative | null; optical_evidence?: Optical[]; enrichment_status?: string;
  surface_conditions?:Conditions; artifacts?: { name: string; url: string; size_bytes: number }[]; timings?: Record<string, unknown> };

const fmt = (v: unknown, d = 2, signed = false) => typeof v === 'number' && Number.isFinite(v)
  ? `${signed && v > 0 ? '+' : ''}${v.toLocaleString('en', { maximumFractionDigits: d, minimumFractionDigits: d })}`.replace(/^-/, '−') : '—';
const text = (v: unknown) => typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '—';
const html = (value: string) => ({ __html: DOMPurify.sanitize(marked.parse(value.replace(/(^|[\s(*_~>])-(?=\d)/g, '$1−'), { async: false }) as string) });
export const artifactUrl = (job: Investigation, name: string) => `/api/investigations/${encodeURIComponent(job.id)}/artifacts/${encodeURIComponent(name)}`;
const layerUrl = (job: Investigation, l: Layer) => artifactUrl(job, l.path.split('/').pop() || '');

/** Everything a reader needs to judge one investigation, starting with the images. */
export default function Dossier({ job, onClose, onGlobe }: { job: Investigation; onClose: () => void; onGlobe: (layer: Layer) => void }) {
  const r = job.result as Full;
  const sheet = useRef<HTMLDivElement>(null);
  useEffect(() => { const prev = document.activeElement as HTMLElement | null; sheet.current?.focus({ preventScroll: true }); return () => prev?.focus(); }, []);
  const layers = r.layers || [];
  const radar = layers.filter(l => l.id.startsWith('backscatter-'));
  const delta = layers.find(l => l.id === 'change');
  const inputs = ((r.provenance as { inputs?: Input[] })?.inputs) || [];
  const m = r.measurements as { observations?: Stats[]; change?: Stats };
  const obs = m.observations || [], change = m.change;
  const hotspots = change?.hotspots || [];
  const optical = r.optical_evidence || [];
  const before = optical.find(o => o.kind === 'before'), after = optical.find(o => o.kind === 'after'), recent = optical.find(o => o.kind === 'recent');
  const n = r.narrative;
  const date = (i: number) => text(inputs[i]?.metadata?.date || radar[i]?.date);
  const pair = radar.length === 2;
  const meta0 = inputs[0]?.metadata || {};
  const elapsed = (r.timings as { interpreted_answer_seconds?: number } | undefined)?.interpreted_answer_seconds;
  const views = [
    ...(before && (after || !pair) ? [['optical', 'True colour']] : []),
    ...(radar.length ? [['radar', 'Radar']] : []),
    ...(delta ? [['change', 'Change']] : []),
  ] as [string, string][];
  const [view, setView] = useState(views[0]?.[0] || 'radar');
  const [layout, setLayout] = useState<'side' | 'swipe'>('side');
  const bbox = job.bbox;
  const boxes: Box[] = hotspots.map(h => ({ id: String(h.rank), bounds: h.bounds, label: String(h.rank), tone: h.direction === 'increase' ? 'up' : 'down' }));
  const opticalFrame = (o: Optical, label: string): Frame => ({ src: artifactUrl(job, o.artifact_name), bounds: o.bounds, label, date: prettyDate(o.date), alt: `${label}: Sentinel-2 true-colour image, ${prettyDate(o.date)}` });
  const radarFrame = (l: Layer, i: number): Frame => ({ src: layerUrl(job, l), bounds: l.bounds, label: i === 0 ? 'Before' : 'After', date: prettyDate(date(i)), alt: `Radar backscatter on ${prettyDate(date(i))}`, pixelated: true });
  const span = pair ? `${prettyDate(date(0))} → ${prettyDate(date(1))}` : radar[0] ? prettyDate(date(0)) : '';
  const verification = n?.verification;
  const sections = useMemo(() => [['images', 'Before and after'], ['answer', 'Answer'], ...(hotspots.length ? [['where', 'Where it changed']] : []), ['numbers', 'Measurements'], ['sources', 'Sources'], ['method', 'Method and limits']], [hotspots.length]);

  return <div className="sheet" role="dialog" aria-modal="true" aria-label="Investigation report" ref={sheet} tabIndex={-1} onKeyDown={e => { if (e.key === 'Escape') onClose(); }}>
    <div className="sheet-bar">
      <span className="sheet-title">{job.title.split(' · ')[0]} <span>· Investigation report</span></span>
      <nav className="sheet-nav" aria-label="Report sections">{sections.map(([id, label]) => <button key={id} onClick={() => document.getElementById(`r-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>{label}</button>)}</nav>
      <span className="sheet-actions">
        <button onClick={() => window.print()}><Printer size={14} /> Print</button>
        <a href={artifactUrl(job, 'public-result.json')} download><Download size={14} /> Data</a>
        <button className="icon-btn" onClick={onClose} aria-label="Close report"><X size={16} /></button>
      </span>
    </div>
    <div className="sheet-scroll">
      <article className="report" id="terra-dossier">
        <header className="report-head">
          <p className="report-kicker">{job.title.split(' · ')[0]}{span && <> · {span}</>}</p>
          <h1>{n?.headline || job.question}</h1>
          {n?.headline && <p className="report-question">Question: {job.question}</p>}
          <p className="report-facts">
            NISAR L-band radar{meta0.track !== undefined ? `, track ${text(meta0.track)} ${text(meta0.direction).toLowerCase()}` : ''} · {text(meta0.maturity).toLowerCase()} products
            {verification ? ` · ${verification.unmatched.length ? `${verification.unmatched.length} numbers need review` : `all ${verification.numbers_checked} numbers traced to computed values`}` : ''}
            {typeof elapsed === 'number' ? ` · answered in ${elapsed.toFixed(1)} s` : ''}
          </p>
        </header>

        <section className="report-section" id="r-images">
          <div className="section-head">
            <h2>Before and after</h2>
            <div className="toggles">
              {views.length > 1 && <div className="seg" role="tablist" aria-label="Image type">{views.map(([id, label]) => <button key={id} role="tab" aria-selected={view === id} className={view === id ? 'on' : ''} onClick={() => setView(id)}>{label}</button>)}</div>}
              {view !== 'change' && (view === 'optical' ? Boolean(before && after) : pair) && <div className="seg" aria-label="Layout">{(['side', 'swipe'] as const).map(id => <button key={id} className={layout === id ? 'on' : ''} onClick={() => setLayout(id)}>{id === 'side' ? 'Side by side' : 'Swipe'}</button>)}</div>}
            </div>
          </div>
          {view === 'optical' && before && after && <>
            <Compare bbox={bbox} mode={layout} before={opticalFrame(before, 'Before')} after={opticalFrame(after, 'After')} />
            <p className="caption">Copernicus Sentinel-2 true colour, 10 m, cropped to this area. Cloud and shadow cover {fmt(before.quality?.obscured_pct, 1)}% before and {fmt(after.quality?.obscured_pct, 1)}% after.
              The photos were taken {Math.abs(before.temporal_offset_days || 0).toFixed(0)} and {Math.abs(after.temporal_offset_days || 0).toFixed(0)} days from the radar dates; they give context and are not part of the measurement.
              {' '}<a href={safeUrl(before.source_url)} target="_blank" rel="noreferrer">Before scene</a> · <a href={safeUrl(after.source_url)} target="_blank" rel="noreferrer">after scene</a>.</p>
          </>}
          {view === 'optical' && before && !after && <>
            <figure className="single"><figcaption><b>True colour</b> <span>{prettyDate(before.date)}</span></figcaption><GeoFrame bbox={bbox} frames={[opticalFrame(before, 'True colour')]} /></figure>
            <p className="caption">Copernicus Sentinel-2 true colour, 10 m. Cloud and shadow cover {fmt(before.quality?.obscured_pct, 1)}%.</p>
          </>}
          {view === 'radar' && (pair ? <>
            <Compare bbox={bbox} mode={layout} before={radarFrame(radar[0], 0)} after={radarFrame(radar[1], 1)} />
            <p className="caption">Calibrated radar backscatter ({text(meta0.selected_polarization)}), shown from −30 dB (dark) to 0 dB (bright). Brightness is radar return strength, not colour: open water is dark; buildings, steep slopes and rough surfaces are bright.</p>
          </> : radar[0] && <figure className="single"><figcaption><b>Radar backscatter</b> <span>{prettyDate(date(0))}</span></figcaption><GeoFrame bbox={bbox} frames={[radarFrame(radar[0], 0)]} /></figure>)}
          {view === 'change' && delta && <>
            <figure className="single"><figcaption><b>Change, later minus earlier</b> <span>{span}</span></figcaption>
              <GeoFrame bbox={bbox} frames={[{ src: layerUrl(job, delta), bounds: delta.bounds, label: 'Change', alt: 'Backscatter change map', pixelated: true }]} boxes={boxes} /></figure>
            <div className="ramp-row"><span>−6 dB darker</span><i className="ramp div" /><span>+6 dB brighter</span></div>
            <p className="caption">Numbered boxes are the largest connected patches that changed by at least {fmt(change?.threshold_db, 0)} dB. <button className="link" onClick={() => onGlobe(delta)}>Show this layer on the globe</button></p>
          </>}
          {!views.length && <p className="muted">No images were produced for this investigation.</p>}
          {r.enrichment_status === 'pending' && <p className="muted">Searching the Sentinel-2 archive for clear true-colour images of the same dates…</p>}
          {recent && <p className="caption">Most recent clear view: <a href={artifactUrl(job, recent.artifact_name)} target="_blank" rel="noreferrer">{prettyDate(recent.date)}</a>, {fmt(recent.quality?.obscured_pct, 1)}% cloud.</p>}
        </section>

        <section className="report-section" id="r-answer">
          <h2>Answer</h2>
          <div className="report-columns">
            <div className="prose">{n?.answer ? <div dangerouslySetInnerHTML={html(n.answer)} /> : <p>The written answer is not available; the measurements below are complete.</p>}
              {n?.evidence && n.evidence.length > 0 && <><h3>Evidence</h3><ul>{n.evidence.map((x, i) => <li key={i} dangerouslySetInnerHTML={html(x)} />)}</ul></>}
            </div>
            <aside className="keyfacts">
              {change ? <>
                <div><span>Median change</span><b>{fmt(change.median_db, 2, true)} dB</b></div>
                <div><span>Brighter by ≥ {fmt(change.threshold_db, 0)} dB</span><b>{fmt(change.increasing_km2, 1)} km²</b></div>
                <div><span>Darker by ≥ {fmt(change.threshold_db, 0)} dB</span><b>{fmt(change.decreasing_km2, 1)} km²</b></div>
                <div><span>Water-like area</span><b>{fmt(change.low_return_before_km2, 2)} → {fmt(change.low_return_after_km2, 2)} km²</b></div>
                <div><span>Area measured</span><b>{fmt(change.valid_projected_area_km2, 0)} km²</b></div>
              </> : obs[0] && <>
                <div><span>Median backscatter</span><b>{fmt(obs[0].median_db)} dB</b></div>
                <div><span>Water-like area</span><b>{fmt(obs[0].low_return_km2, 2)} km²</b></div>
                <div><span>Very bright area</span><b>{fmt(obs[0].high_return_km2, 2)} km²</b></div>
                <div><span>Area measured</span><b>{fmt(obs[0].valid_projected_area_km2, 0)} km²</b></div>
              </>}
            </aside>
          </div>
          {n?.possible_explanations && n.possible_explanations.length > 0 && <><h3>What could explain it</h3><ul className="plain">{n.possible_explanations.map((x, i) => <li key={i}>{x}</li>)}</ul></>}
          {n?.next_steps && n.next_steps.length > 0 && <><h3>Worth checking next</h3><ul className="plain">{n.next_steps.map((x, i) => <li key={i}>{x}</li>)}</ul></>}
          <p className="caption">No forecast is made. Two radar dates show what changed between them, not what will happen next.</p>
        </section>

        {hotspots.length > 0 && <section className="report-section" id="r-where">
          <h2>Where it changed</h2>
          <p className="lede">The largest connected patches whose radar return changed by at least {fmt(change?.threshold_db, 0)} dB, after averaging each pixel with its neighbours to suppress speckle.</p>
          <table className="table">
            <thead><tr><th>#</th><th>Direction</th><th className="r">Area</th><th className="r">Mean change</th><th className="r">Strongest</th><th>Centre</th></tr></thead>
            <tbody>{hotspots.map(h => <tr key={h.rank}><td>{h.rank}</td><td><i className={`tone ${h.direction === 'increase' ? 'up' : 'down'}`} />{h.direction === 'increase' ? 'Brighter' : 'Darker'}</td>
              <td className="r num">{fmt(h.area_km2, 2)} km²</td><td className="r num">{fmt(h.mean_change_db, 1, true)} dB</td><td className="r num">{fmt(h.peak_change_db, 1, true)} dB</td>
              <td className="num">{h.centre[0].toFixed(4)}, {h.centre[1].toFixed(4)}</td></tr>)}</tbody>
          </table>
        </section>}

        <section className="report-section" id="r-numbers">
          <h2>Measurements</h2>
          <table className="table">
            <thead><tr><th>Measure</th>{obs.map((o, i) => <th key={i} className="r">{prettyDate(date(i))}</th>)}{change && <th className="r">Change per pixel</th>}</tr></thead>
            <tbody>{([['median_db', 'Median backscatter', 'dB', 2], ['mean_db', 'Mean backscatter', 'dB', 2], ['p05_db', '5th percentile', 'dB', 2], ['p95_db', '95th percentile', 'dB', 2],
              ['low_return_km2', 'Water-like area (below −18 dB)', 'km²', 2], ['high_return_km2', 'Very bright area (above −3 dB)', 'km²', 2],
              ['valid_projected_area_km2', 'Area measured', 'km²', 1], ['coverage_pct', 'Coverage', '%', 1]] as const).map(([key, label, unit, d]) =>
              <tr key={key}><td>{label}</td>{obs.map((o, i) => <td key={i} className="r num">{fmt(o[key], d)} {unit}</td>)}{change && <td className="r num">{key in change ? `${fmt(change[key], d, key.endsWith('_db'))} ${unit}` : '—'}</td>}</tr>)}</tbody>
          </table>
          {change && <p className="caption">The change column is computed pixel by pixel where both dates are valid, so it is not the difference of the date columns. {fmt(change.threshold_exceedance_pct, 1)}% of those pixels changed by at least {fmt(change.threshold_db, 0)} dB; {fmt(change.newly_low_return_km2, 2)} km² became water-like and {fmt(change.no_longer_low_return_km2, 2)} km² stopped being water-like. Thresholds describe magnitude; they are not significance tests.</p>}
        </section>

        {r.surface_conditions&&<section className="report-section" id="r-conditions"><h2>Weather at the radar dates</h2><ConditionsGrid value={r.surface_conditions} waterBefore={(r.measurements as {change?:Record<string,number>})?.change?.low_return_before_km2} waterAfter={(r.measurements as {change?:Record<string,number>})?.change?.low_return_after_km2} /></section>}

        <section className="report-section" id="r-sources">
          <h2>Sources</h2>
          <div className="sources">{inputs.map((input, i) => { const md = input.metadata || {}; const cat = input.catalog || {};
            return <div key={i} className="source">
              <h3>{pair ? (i === 0 ? 'Before' : 'After') : 'Acquisition'} · {prettyDate(date(i))}</h3>
              <dl>
                <div><dt>Product</dt><dd>NISAR {text(md.product)} · {text(md.maturity).toLowerCase()}</dd></div>
                <div><dt>Channel</dt><dd>{text(md.selected_polarization)}, frequency {text(md.frequency)}</dd></div>
                <div><dt>Track and frame</dt><dd>{text(md.track)} / {text(md.frame)}, {text(md.direction).toLowerCase()}</dd></div>
                <div><dt>Pixel spacing</dt><dd>{Array.isArray(md.pixel_spacing) ? `${Math.abs((md.pixel_spacing as number[])[0])} m` : '—'}{Number(md.multilook) > 1 ? ` (averaged ${text(md.multilook)}×${text(md.multilook)})` : ''}</dd></div>
                <div><dt>Release</dt><dd>{text(md.processing_release)} · v{text(md.product_version)}</dd></div>
                <div><dt>SHA-256</dt><dd className="hash">{input.sha256}</dd></div>
              </dl>
              {safeUrl(text(cat.download_url) === '—' ? '' : text(cat.download_url)) && <a href={safeUrl(text(cat.download_url))} target="_blank" rel="noreferrer">Original product at NASA/ASF <ArrowUpRight size={12} /></a>}
            </div>; })}
            {optical.length > 0 && <div className="source"><h3>True-colour context</h3><dl>{optical.map(o => <div key={o.kind}><dt>{o.kind === 'recent' ? 'Latest' : o.kind === 'before' ? 'Before' : 'After'}</dt>
              <dd><a href={safeUrl(o.source_url)} target="_blank" rel="noreferrer">Sentinel-2, {prettyDate(o.date)}</a> · {fmt(o.quality?.obscured_pct, 1)}% cloud</dd></div>)}</dl></div>}
          </div>
        </section>

        <section className="report-section" id="r-method">
          <h2>Method and limits</h2>
          <p className="lede">{text((r.provenance as { formula?: string })?.formula)}. Measurements use the native radar grid after the product’s own quality masks. {r.validation?.independent_validation === 'not_performed' ? 'No independent field validation was performed.' : ''}</p>
          <ul className="plain">{r.validation?.limitations?.map((x, i) => <li key={i}>{x}</li>)}</ul>
          <details><summary>Processing record ({job.events.length} steps)</summary><ol className="log">{job.events.map((x, i) => <li key={i}><time className="num">{new Date(x.at).toISOString().slice(11, 19)}</time>{x.message}</li>)}</ol></details>
          <details><summary>Files ({(r.artifacts?.length || 0) + 1})</summary><ul className="files">{r.artifacts?.map(a => <li key={a.name}><a href={a.url} download>{a.name}</a><span>{a.name.endsWith('.tif') ? 'Georeferenced float raster' : a.name.endsWith('.png') ? 'Preview image' : 'Record'} · {(a.size_bytes / 1048576).toFixed(2)} MB</span></li>)}
            <li><a href={artifactUrl(job, 'public-result.json')} download>public-result.json</a><span>Measurements, sources, checks and the written answer</span></li></ul></details>
        </section>
        <footer className="report-foot">Terra investigation {job.id} · generated {prettyDate(job.updated_at || job.created_at)} from the products listed above.</footer>
      </article>
    </div>
  </div>;
}
