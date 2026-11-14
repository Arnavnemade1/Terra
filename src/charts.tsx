import { useId, useState } from 'react';

/** Small dark-theme charts for the analysis dock. Every value drawn is a measured or computed value. */

const W = 320, H = 96, L = 36, R = 8, T = 8, B = 18;
const fmt = (v: number, d = 0) => v.toLocaleString('en', { maximumFractionDigits: d }).replace(/^-/, '−');
function niceTicks(lo: number, hi: number, count = 3): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (hi === lo) return [lo];
  const raw = (hi - lo) / count; const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
  const out: number[] = []; for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(+v.toFixed(10));
  return out;
}

export function ChartFrame({ title, unit, action, children, empty }: { title: string; unit?: string; action?: React.ReactNode; children?: React.ReactNode; empty?: string }) {
  return <figure className="dock-chart">
    <figcaption><span><b>{title}</b>{unit && <small> ({unit})</small>}</span>{action}</figcaption>
    {empty ? <p className="chart-empty">{empty}</p> : children}
  </figure>;
}

type Series = { values: (number | null)[]; color: string; fill?: boolean; dashed?: boolean; label?: string };

/** Lines or filled areas over a shared x axis (numbers: time in ms or distance), with an optional cursor. */
export function LinePlot({ x, series, xLabel, cursor, onHover, yDigits = 0, markers, zero = true }: {
  x: number[]; series: Series[]; xLabel: (v: number) => string; cursor?: number | null; onHover?: (i: number | null) => void; yDigits?: number; zero?: boolean;
  markers?: { x: number; color: string }[];
}) {
  const id = useId();
  const all = series.flatMap(s => s.values).filter((v): v is number => v !== null && Number.isFinite(v));
  if (!x.length || !all.length) return <p className="chart-empty">No values yet.</p>;
  let lo = Math.min(...all), hi = Math.max(...all);
  if (zero && series.some(s => s.fill) && lo > 0) lo = 0;
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; hi += pad; if (lo !== 0) lo -= pad;
  const x0 = x[0], x1 = x.at(-1)!; const sx = (v: number) => L + ((v - x0) / Math.max(1e-9, x1 - x0)) * (W - L - R);
  const sy = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const path = (vals: (number | null)[]) => vals.map((v, i) => v === null || !Number.isFinite(v) ? '' : `${i === 0 || vals[i - 1] === null ? 'M' : 'L'}${sx(x[i]).toFixed(1)},${sy(v).toFixed(1)}`).join('');
  const yt = niceTicks(lo, hi, 3); const xt = niceTicks(x0, x1, 4).filter(v => v >= x0 && v <= x1);
  const hoverIndex = cursor === undefined || cursor === null ? null : cursor;
  return <svg className="plot" viewBox={`0 0 ${W} ${H}`} role="img"
    onPointerMove={onHover ? e => { const r = e.currentTarget.getBoundingClientRect(); const v = x0 + ((e.clientX - r.left) / r.width * W - L) / (W - L - R) * (x1 - x0);
      let best = 0; x.forEach((xv, i) => { if (Math.abs(xv - v) < Math.abs(x[best] - v)) best = i; }); onHover(best); } : undefined}
    onPointerLeave={onHover ? () => onHover(null) : undefined}>
    <defs>{series.map((s, i) => <linearGradient key={i} id={`${id}-${i}`} x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor={s.color} stopOpacity="0.42" /><stop offset="1" stopColor={s.color} stopOpacity="0.03" /></linearGradient>)}</defs>
    {yt.map(v => <g key={v}><line className="grid" x1={L} x2={W - R} y1={sy(v)} y2={sy(v)} /><text className="tick" x={L - 4} y={sy(v) + 3} textAnchor="end">{fmt(v, yDigits)}</text></g>)}
    {xt.map(v => <text key={v} className="tick" x={sx(v)} y={H - 4} textAnchor="middle">{xLabel(v)}</text>)}
    {series.map((s, i) => s.fill && <path key={`f${i}`} d={`${path(s.values)}L${sx(x.at(-1)!)},${sy(zero ? Math.max(lo, 0) : lo)}L${sx(x[0])},${sy(zero ? Math.max(lo, 0) : lo)}Z`} fill={`url(#${id}-${i})`} />)}
    {series.map((s, i) => <path key={i} d={path(s.values)} fill="none" stroke={s.color} strokeWidth="1.5" strokeDasharray={s.dashed ? '3 3' : undefined} vectorEffect="non-scaling-stroke" />)}
    {markers?.map((m, i) => <circle key={i} cx={sx(m.x)} cy={T + 2} r="2.2" fill={m.color} />)}
    {hoverIndex !== null && x[hoverIndex] !== undefined && <g><line className="cursor" x1={sx(x[hoverIndex])} x2={sx(x[hoverIndex])} y1={T} y2={H - B} />
      {series.map((s, i) => s.values[hoverIndex] !== null && s.values[hoverIndex] !== undefined && <circle key={i} cx={sx(x[hoverIndex])} cy={sy(s.values[hoverIndex] as number)} r="3" fill="#141511" stroke={s.color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />)}</g>}
  </svg>;
}

/** Histogram bars from (edges, counts), coloured by a function of the bin centre, with reference lines. */
export function Bars({ edges, counts, color, lines = [], overlay, xDigits = 0 }: {
  edges: number[]; counts: number[]; color: (centre: number) => string; lines?: { x: number; label?: string; dashed?: boolean }[];
  overlay?: { edges: number[]; counts: number[]; color: string }; xDigits?: number;
}) {
  if (!edges.length || !counts.length) return <p className="chart-empty">No values yet.</p>;
  const peak = Math.max(...counts, ...(overlay?.counts || []), 1);
  const lo = Math.min(edges[0], overlay?.edges[0] ?? Infinity), hi = Math.max(edges.at(-1)!, overlay?.edges.at(-1) ?? -Infinity);
  const sx = (v: number) => L + ((v - lo) / (hi - lo)) * (W - L - R); const sy = (c: number) => T + (1 - c / peak) * (H - T - B);
  const xt = niceTicks(lo, hi, 4);
  const shares = (c: number) => `${fmt(c / counts.reduce((a, b) => a + b, 0) * 100, 1)}%`;
  return <svg className="plot" viewBox={`0 0 ${W} ${H}`} role="img">
    {[0.5, 1].map(f => <g key={f}><line className="grid" x1={L} x2={W - R} y1={sy(peak * f)} y2={sy(peak * f)} /><text className="tick" x={L - 4} y={sy(peak * f) + 3} textAnchor="end">{shares(peak * f)}</text></g>)}
    {counts.map((c, i) => <rect key={i} x={sx(edges[i]) + 0.3} y={sy(c)} width={Math.max(0.4, sx(edges[i + 1]) - sx(edges[i]) - 0.6)} height={H - B - sy(c)} fill={color((edges[i] + edges[i + 1]) / 2)} />)}
    {overlay && <path d={overlay.counts.map((c, i) => `${i ? 'L' : 'M'}${sx(overlay.edges[i]).toFixed(1)},${sy(c).toFixed(1)}L${sx(overlay.edges[i + 1]).toFixed(1)},${sy(c).toFixed(1)}`).join('')} fill="none" stroke={overlay.color} strokeWidth="1.3" vectorEffect="non-scaling-stroke" />}
    {lines.map((l, i) => <g key={i}><line className={l.dashed ? 'ref dashed' : 'ref'} x1={sx(l.x)} x2={sx(l.x)} y1={T} y2={H - B} />{l.label && <text className="tick strong" x={sx(l.x) + 3} y={T + 8}>{l.label}</text>}</g>)}
    <line className="axis" x1={L} x2={W - R} y1={H - B} y2={H - B} />
    {xt.map(v => <text key={v} className="tick" x={sx(v)} y={H - 4} textAnchor="middle">{v > 0 && lo < 0 ? `+${fmt(v, xDigits)}` : fmt(v, xDigits)}</text>)}
  </svg>;
}

/** Vertical bars for categories or days. */
export function Columns({ items, digits = 0 }: { items: { label: string; value: number; color: string; title?: string }[]; digits?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!items.length) return <p className="chart-empty">No values yet.</p>;
  const peak = Math.max(...items.map(i => i.value), 1); const bw = (W - L - R) / items.length;
  const sy = (v: number) => T + (1 - v / peak) * (H - T - B);
  const yt = niceTicks(0, peak, 2);
  const every = Math.ceil(items.length / 7);
  return <svg className="plot" viewBox={`0 0 ${W} ${H}`} role="img" onPointerLeave={() => setHover(null)}>
    {yt.map(v => <g key={v}><line className="grid" x1={L} x2={W - R} y1={sy(v)} y2={sy(v)} /><text className="tick" x={L - 4} y={sy(v) + 3} textAnchor="end">{fmt(v, digits)}</text></g>)}
    {items.map((it, i) => <g key={i} onPointerEnter={() => setHover(i)}>
      <rect x={L + i * bw + bw * 0.18} y={sy(it.value)} width={bw * 0.64} height={H - B - sy(it.value)} fill={it.color} opacity={hover === null || hover === i ? 1 : 0.55}><title>{it.title || `${it.label}: ${fmt(it.value, digits)}`}</title></rect>
      {(i % every === 0 || hover === i) && <text className={`tick ${hover === i ? 'strong' : ''}`} x={L + (i + 0.5) * bw} y={H - 4} textAnchor="middle">{it.label}</text>}
      {hover === i && <text className="tick strong" x={L + (i + 0.5) * bw} y={Math.max(T + 8, sy(it.value) - 3)} textAnchor="middle">{fmt(it.value, digits)}</text>}
    </g>)}
    <line className="axis" x1={L} x2={W - R} y1={H - B} y2={H - B} />
  </svg>;
}
