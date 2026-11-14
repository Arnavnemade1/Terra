import { useRef } from 'react';
import { ChevronDown, Pause, Play, Plus, SkipForward } from 'lucide-react';

export type Marker = { id: string; t: number; kind: 'acquisition' | 'pair' | 'optical' | 'hazard' | 'note' | 'pass' | 'now'; color?: string; label: string; size?: number };
export type EventItem = { id: string; t: number; title: string; sub?: string; color: string; shape?: 'dot' | 'tri' | 'square' | 'ring'; active?: boolean };

const DAY = 86_400_000;
const month = (t: number) => new Date(t).toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' });
const day = (t: number) => new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
const hm = (t: number) => new Date(t).toISOString().slice(11, 16);
export const stamp = (t: number, withTime = true) => `${new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })}${withTime ? `, ${hm(t)}` : ''}`;

/** Tick positions and two-line labels suited to the span: hours, days, months or years. */
function ticks(start: number, end: number): { t: number; top: string; bottom?: string }[] {
  const span = end - start; const out: { t: number; top: string; bottom?: string }[] = [];
  if (span <= 4 * DAY) {
    const step = span <= 2 * DAY ? 6 * 3600_000 : 12 * 3600_000;
    for (let t = Math.ceil(start / step) * step; t <= end; t += step) out.push({ t, top: new Date(t).getUTCHours() === 0 || out.length === 0 ? day(t) : '', bottom: hm(t) });
  } else if (span <= 75 * DAY) {
    const step = span <= 21 * DAY ? DAY : 7 * DAY;
    for (let t = Math.ceil(start / DAY) * DAY; t <= end; t += step) out.push({ t, top: day(t) });
  } else if (span <= 3 * 365 * DAY) {
    const d = new Date(start); d.setUTCDate(1); d.setUTCHours(0, 0, 0, 0); d.setUTCMonth(d.getUTCMonth() + 1);
    const every = span > 18 * 30 * DAY ? 3 : span > 8 * 30 * DAY ? 2 : 1;
    for (let i = 0; d.getTime() <= end; i++) { if (d.getUTCMonth() % every === 0) out.push({ t: d.getTime(), top: month(d.getTime()), bottom: d.getUTCMonth() < every ? String(d.getUTCFullYear()) : '' }); d.setUTCMonth(d.getUTCMonth() + 1); }
  } else {
    const d = new Date(Date.UTC(new Date(start).getUTCFullYear() + 1, 0, 1));
    for (; d.getTime() <= end; d.setUTCFullYear(d.getUTCFullYear() + 1)) out.push({ t: d.getTime(), top: String(d.getUTCFullYear()) });
  }
  return out;
}

/** The scrubbable timeline: markers for dated evidence, a highlighted band and a current-time handle. */
export function Timeline({ range, t, onTime, markers, band, playing, onPlay, speed, onSpeed, onStep, title = 'Timeline', withTime = false }: {
  range: [number, number]; t: number; onTime: (t: number) => void; markers: Marker[]; band?: [number, number] | null;
  playing: boolean; onPlay: () => void; speed: number; onSpeed: (s: number) => void; onStep: () => void; title?: string; withTime?: boolean;
}) {
  const track = useRef<HTMLDivElement>(null);
  const [start, end] = range; const span = Math.max(1, end - start);
  const x = (v: number) => `${Math.min(100, Math.max(0, (v - start) / span * 100))}%`;
  const fromPointer = (clientX: number) => { const r = track.current?.getBoundingClientRect(); if (!r) return; onTime(start + Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * span); };
  return <div className="timeline">
    <div className="timeline-head"><b>{title}</b><span className="timeline-range">{stamp(start, withTime)} – {stamp(end, withTime)}</span>
      <span className="timeline-count">{markers.length} dated {markers.length === 1 ? 'item' : 'items'}</span></div>
    <div className="timeline-body">
      <div className="transport">
        <button onClick={onPlay} aria-label={playing ? 'Pause' : 'Play'} className={playing ? 'on' : ''}>{playing ? <Pause size={13} /> : <Play size={13} />}</button>
        <button onClick={onStep} aria-label="Next event"><SkipForward size={13} /></button>
        <label className="speed"><select value={speed} onChange={e => onSpeed(Number(e.target.value))} aria-label="Playback speed">{[0.5, 1, 2, 4].map(s => <option key={s} value={s}>{s}×</option>)}</select><ChevronDown size={11} /></label>
      </div>
      <div className="scrubber">
        <div className="scrub-track" ref={track} onPointerDown={e => { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); fromPointer(e.clientX); }}
          onPointerMove={e => { if (e.buttons) fromPointer(e.clientX); }} role="slider" tabIndex={0} aria-label="Time" aria-valuemin={start} aria-valuemax={end} aria-valuenow={Math.round(t)}
          aria-valuetext={stamp(t, withTime)} onKeyDown={e => { if (e.key === 'ArrowRight') onTime(Math.min(end, t + span / 100)); if (e.key === 'ArrowLeft') onTime(Math.max(start, t - span / 100)); }}>
          <i className="scrub-line" />
          {band && <i className="scrub-band" style={{ left: x(band[0]), width: `calc(${x(band[1])} - ${x(band[0])})` }} />}
          {markers.map(m => <span key={m.id} className={`mk mk-${m.kind}`} style={{ left: x(m.t), ['--mk' as string]: m.color || undefined, ['--sz' as string]: m.size ? `${m.size}px` : undefined }} title={`${m.label} · ${stamp(m.t, withTime)}`}
            onPointerDown={e => { e.stopPropagation(); onTime(m.t); }} />)}
          <span className="scrub-handle" style={{ left: x(t), ['--shift' as string]: (t - start) / span < 0.08 ? '-8%' : (t - start) / span > 0.92 ? '-92%' : '-50%' }}><em>{stamp(t, withTime)}</em></span>
        </div>
        <div className="scrub-ticks">{ticks(start, end).map((k, i) => <span key={i} style={{ left: x(k.t) }}><b>{k.top}</b>{k.bottom && <small>{k.bottom}</small>}</span>)}</div>
      </div>
    </div>
  </div>;
}

/** The right-hand list in the dock (key events, hazard log, pass log). */
export function EventList({ title, count, action, items, onSelect, empty }: {
  title: string; count?: number; action?: { label: string; onClick: () => void }; items: EventItem[]; onSelect: (item: EventItem) => void; empty?: string;
}) {
  return <aside className="dock-side">
    <div className="dock-side-head"><b>{title}</b>{count !== undefined && <span className="badge">{count}</span>}
      {action && <button className="mini-btn" onClick={action.onClick}><Plus size={11} /> {action.label}</button>}</div>
    {items.length ? <ul className="event-list">{items.map(it => <li key={it.id}><button className={it.active ? 'on' : ''} onClick={() => onSelect(it)}>
      <i className={`shape ${it.shape || 'dot'}`} style={{ ['--c' as string]: it.color }} />
      <span className="ev-text"><b>{it.title}</b>{it.sub && <small>{it.sub}</small>}</span>
      <time>{stamp(it.t, false).replace(/ \d{4}$/, '')}</time>
    </button></li>)}</ul> : <p className="chart-empty">{empty || 'Nothing to show yet.'}</p>}
  </aside>;
}
