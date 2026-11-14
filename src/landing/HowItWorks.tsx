import { memo, useMemo, useState } from "react";
import { AnimatePresence, motion, useMotionValueEvent, useReducedMotion, useScroll } from "motion/react";
import { useRef } from "react";
import { Slider } from "@/components/arc/slider/slider";

/**
 * A scroll-driven walk through one analysis on a synthetic scene (not real data). The "radar" is a smooth random field in dB,
 * with a flooded patch (much darker on the later date) and a burned patch (brighter). The change map and the share of the
 * scene flagged are recomputed from the slider, with the same rule Terra uses: later minus earlier, flagged at |change| >= threshold.
 */
const W = 40, H = 24, CELL = 20;
const rand = (x: number, y: number) => { const s = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return s - Math.floor(s); };
const smooth = (x: number, y: number) => {
  const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
  const a = rand(xi, yi), b = rand(xi + 1, yi), c = rand(xi, yi + 1), d = rand(xi + 1, yi + 1);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
};
const blob = (x: number, y: number, cx: number, cy: number, r: number) => Math.max(0, 1 - Math.hypot(x - cx, y - cy) / r);

const SCENE = (() => {
  const before: number[] = [], after: number[] = [], delta: number[] = [];
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const base = -15 + (smooth(x / 5, y / 5) - 0.5) * 8 + (rand(x, y) - 0.5) * 1.6;
    const flood = blob(x, y, 12, 14, 7), burn = blob(x, y, 30, 7, 5);
    const b = base, a = base + (rand(y, x) - 0.5) * 1.4 - 11 * Math.min(1, flood * 1.6) + 5 * Math.min(1, burn * 1.6);
    before.push(b); after.push(a); delta.push(a - b);
  }
  return { before, after, delta };
})();
const gray = (db: number) => { const t = Math.min(1, Math.max(0, (db + 28) / 26)); const v = Math.round(40 + t * 190); return `rgb(${v},${v - 2},${v - 8})`; };
const change = (d: number) => d <= 0 ? `rgba(255,90,31,${Math.min(1, 0.35 + -d / 14)})` : `rgba(127,176,224,${Math.min(1, 0.35 + d / 10)})`;

const STEPS = [
  { title: "Pick a place and two dates", text: "Choose where to look and when. Terra lists every NISAR pass over the area and keeps only pairs that can be compared fairly.", callout: ["Earlier pass", "Radar brightness on the first date. Dark means little signal came back, bright means a lot."] },
  { title: "Read the later pass", text: "The same place, from the same track and direction, on the later date. Same geometry, so differences are the ground and not the viewing angle.", callout: ["Later pass", "Looks similar at a glance, which is why subtracting the two is the reliable way to see change."] },
  { title: "Subtract and flag", text: "Later minus earlier, in decibels. Anything that moved by the threshold or more is flagged: orange where it got darker, blue where it got brighter.", callout: ["Change map", "Drag the threshold. A lower value flags more of the scene, and more of it is noise."] },
  { title: "Find the hotspots", text: "The strongest patches are outlined and named, then read against optical imagery, land cover and weather before anything is written.", callout: ["Two hotspots", "A large darkening, like a flooded field, and a smaller brightening, like cleared or burned ground."] },
] as const;

const Cells = memo(function Cells({ stage, threshold }: { stage: number; threshold: number }) {
  const rects = useMemo(() => {
    const out: React.ReactNode[] = [];
    for (let i = 0; i < W * H; i++) {
      const x = (i % W) * CELL, y = Math.floor(i / W) * CELL;
      const db = stage === 0 ? SCENE.before[i] : SCENE.after[i];
      const d = SCENE.delta[i];
      const flagged = stage >= 2 && Math.abs(d) >= threshold;
      out.push(<rect key={i} x={x} y={y} width={CELL + 0.5} height={CELL + 0.5} fill={flagged ? change(d) : gray(db)} />);
    }
    return out;
  }, [stage, threshold]);
  return <>{rects}</>;
});

export function HowItWorks() {
  const ref = useRef<HTMLElement>(null);
  const reduced = useReducedMotion();
  const [stage, setStage] = useState(0);
  const [threshold, setThreshold] = useState(3);
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start start", "end end"] });
  useMotionValueEvent(scrollYProgress, "change", (v) => setStage(Math.min(STEPS.length - 1, Math.max(0, Math.floor((v - 0.04) / 0.23)))));
  const flagged = useMemo(() => SCENE.delta.filter((d) => Math.abs(d) >= threshold).length / SCENE.delta.length, [threshold]);
  const [title, body] = STEPS[stage].callout;

  return (
    <section id="how" ref={ref} className="how" aria-labelledby="how-title">
      <div className="how-stick">
        <div className="how-left">
          <h2 id="how-title" className="how-title">How it works</h2>
          <ol className="how-steps">
            {STEPS.map((s, i) => (
              <li key={s.title} className="how-step" data-active={stage === i} aria-current={stage === i ? "step" : undefined}>
                <span className="how-num">{i + 1}</span>
                <div><h3>{s.title}</h3><p>{s.text}</p></div>
              </li>
            ))}
          </ol>
          <p className="how-hint">Scroll to step through one analysis. At step 3, drag the threshold.</p>
        </div>

        <div className="how-demo">
          <div className="how-stage">
            <div className="how-map" style={{ aspectRatio: `${W} / ${H}` }}>
              <svg viewBox={`0 0 ${W * CELL} ${H * CELL}`} className="how-map-svg" preserveAspectRatio="none" role="img"
                aria-label={`A synthetic radar scene, step ${stage + 1} of ${STEPS.length}: ${STEPS[stage].title}`}>
                <Cells stage={stage} threshold={threshold} />
                {stage >= 3 && (
                  <g fill="none" stroke="#ece9df" strokeWidth="3" strokeDasharray="8 6">
                    <circle cx={12 * CELL + 10} cy={14 * CELL + 10} r={7.4 * CELL} />
                    <circle cx={30 * CELL + 10} cy={7 * CELL + 10} r={5.2 * CELL} />
                  </g>
                )}
              </svg>
              <div className="how-callout" aria-live="polite" aria-atomic="true">
                <AnimatePresence mode="wait" initial={false}>
                  <motion.div key={stage} className="how-card" data-tone={stage === 3 ? "red" : undefined}
                    initial={reduced ? false : { opacity: 0, y: 10, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={reduced ? undefined : { opacity: 0, y: -6 }} transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}>
                    <strong>{title}</strong>
                    <span>{body}</span>
                  </motion.div>
                </AnimatePresence>
              </div>
            </div>
            <div className="how-controls">
              <Slider label="Change threshold" value={threshold} onValueChange={setThreshold} min={1} max={8} step={0.5} format={(v) => `${v.toFixed(1)} dB`} />
              <p className="how-readout" aria-live="polite">{stage >= 2 ? <><strong>{(flagged * 100).toFixed(1)}%</strong> of the scene flagged</> : "Reach step 3 to see what gets flagged"}</p>
            </div>
            <div className="how-credit">Synthetic scene for illustration. Not real NISAR data.</div>
          </div>
        </div>
      </div>
    </section>
  );
}
