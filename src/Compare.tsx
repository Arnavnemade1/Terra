import { useRef, useState } from 'react';
import type { Bounds } from './types';

export type Frame = { src: string; bounds: Bounds; label: string; date?: string; alt: string; pixelated?: boolean };
export type Box = { id: string; bounds: Bounds; label: string; tone: 'up' | 'down' };

/** Ground aspect (east–west km over north–south km) so every image in a frame shares one geometry. */
export const groundAspect = (b: Bounds) => ((b[2] - b[0]) * Math.cos(((b[1] + b[3]) / 2) * Math.PI / 180)) / Math.max(1e-9, b[3] - b[1]);

function place(inner: Bounds, outer: Bounds): React.CSSProperties {
  const w = outer[2] - outer[0], h = outer[3] - outer[1];
  return { left: `${((inner[0] - outer[0]) / w) * 100}%`, top: `${((outer[3] - inner[3]) / h) * 100}%`,
    width: `${((inner[2] - inner[0]) / w) * 100}%`, height: `${((inner[3] - inner[1]) / h) * 100}%` };
}

/** Images placed by their own bounds inside one area of interest, with optional outlined boxes. */
export function GeoFrame({ bbox, frames, boxes, clip, children, className = '' }: {
  bbox: Bounds; frames: Frame[]; boxes?: Box[]; clip?: number; children?: React.ReactNode; className?: string;
}) {
  const aspect = groundAspect(bbox);
  // Fit tall areas on screen: the frame never grows taller than about three quarters of the viewport.
  return <div className={`geo-frame ${className}`} style={{ aspectRatio: String(aspect), maxWidth: `calc(74vh * ${aspect.toFixed(4)})`, marginInline: 'auto' }}>
    {frames.map((f, i) => <img key={f.src} src={f.src} alt={f.alt} draggable={false} className={f.pixelated ? 'pixelated' : ''}
      style={{ ...place(f.bounds, bbox), clipPath: clip !== undefined && i === frames.length - 1 ? `inset(0 ${100 - clip}% 0 0)` : undefined }} />)}
    {boxes?.map(b => <span key={b.id} className={`geo-box ${b.tone}`} style={place(b.bounds, bbox)}><b>{b.label}</b></span>)}
    {children}
  </div>;
}

/** Two dated images of the same area: side by side, or one over the other with a draggable divider. */
export default function Compare({ bbox, before, after, mode, boxes }: { bbox: Bounds; before: Frame; after: Frame; mode: 'side' | 'swipe'; boxes?: Box[] }) {
  const [split, setSplit] = useState(50);
  const frame = useRef<HTMLDivElement>(null);
  const move = (clientX: number) => {
    const r = frame.current?.getBoundingClientRect(); if (!r) return;
    setSplit(Math.min(100, Math.max(0, ((clientX - r.left) / r.width) * 100)));
  };
  if (mode === 'side') return <div className="compare side">
    {[before, after].map(f => <figure key={f.src}>
      <figcaption><b>{f.label}</b>{f.date && <span>{f.date}</span>}</figcaption>
      <GeoFrame bbox={bbox} frames={[f]} boxes={boxes} />
    </figure>)}
  </div>;
  return <div className="compare swipe">
    <div className="swipe-labels"><span><b>{before.label}</b> {before.date}</span><span><b>{after.label}</b> {after.date}</span></div>
    <div ref={frame} className="swipe-area" onPointerDown={e => { (e.target as HTMLElement).setPointerCapture?.(e.pointerId); move(e.clientX); }}
      onPointerMove={e => { if (e.buttons) move(e.clientX); }}>
      <GeoFrame bbox={bbox} frames={[after, before]} clip={split} boxes={boxes}>
        <span className="swipe-handle" style={{ left: `${split}%` }} role="slider" tabIndex={0} aria-label="Divider between the earlier and later image"
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(split)}
          onKeyDown={e => { if (e.key === 'ArrowLeft') setSplit(s => Math.max(0, s - 5)); if (e.key === 'ArrowRight') setSplit(s => Math.min(100, s + 5)); }} />
      </GeoFrame>
    </div>
    <p className="swipe-hint">Drag across the image to move the divider. Left of the line: {before.label.toLowerCase()}; right: {after.label.toLowerCase()}.</p>
  </div>;
}
