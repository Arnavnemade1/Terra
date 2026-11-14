import { useId } from "react";

/** Illustrative only: where typical surfaces fall on the backscatter scale. Not analysis output. */
const MIN = -30, MAX = 0;
const pos = (db: number) => ((db - MIN) / (MAX - MIN)) * 100;
const bands = [
  { name: "Open water", range: "darker than −18 dB", note: "Radar bounces away from the satellite, so almost nothing comes back.", db: -23, tone: "var(--brand-red)" },
  { name: "Forest and fields", range: "about −16 to −6 dB", note: "Leaves, stems and soil scatter part of the signal back.", db: -11, tone: "var(--brand-green)" },
  { name: "Buildings and bare rock", range: "brighter than −3 dB", note: "Hard corners and rough ground return a lot of the signal.", db: -1.5, tone: "var(--foreground)" },
];

export function MarginGraphic() {
  const id = useId();
  return (
    <figure className="mg" aria-labelledby={`${id}-cap`}>
      <svg viewBox="0 0 560 150" className="mg-route" role="img" aria-label="A scale of radar brightness from minus 30 to 0 decibels, with water below minus 18 and very bright surfaces above minus 3">
        <rect x="20" y="58" width="520" height="14" rx="7" fill="var(--surface-muted)" />
        <rect x={20 + pos(-30) * 5.2} y="58" width={(pos(-18) - pos(-30)) * 5.2} height="14" rx="7" fill="var(--brand-red)" opacity=".85" />
        <rect x={20 + pos(-18) * 5.2} y="58" width={(pos(-3) - pos(-18)) * 5.2} height="14" fill="var(--brand-green)" opacity=".7" />
        <rect x={20 + pos(-3) * 5.2} y="58" width={(pos(0) - pos(-3)) * 5.2} height="14" rx="7" fill="var(--foreground)" />
        {[-30, -18, -3, 0].map((v) => (
          <g key={v}>
            <line x1={20 + pos(v) * 5.2} x2={20 + pos(v) * 5.2} y1="52" y2="78" stroke="var(--border-strong)" />
            <text x={20 + pos(v) * 5.2} y="98" textAnchor="middle">{v} dB</text>
          </g>
        ))}
        <text x={20 + pos(-24) * 5.2} y="40" textAnchor="middle" className="mg-dash-label">Water-like</text>
        <text x={20 + pos(-10.5) * 5.2} y="40" textAnchor="middle">Typical land</text>
        <text x={20 + pos(-1.5) * 5.2} y="40" textAnchor="end">Very bright</text>
        <text x="280" y="132" textAnchor="middle" className="mg-dash-label">A change of 3 dB or more between dates is flagged</text>
      </svg>
      <ul className="mg-gauges">
        {bands.map((b) => (
          <li key={b.name}>
            <div className="mg-row"><span>{b.name}</span><span className="mg-pct">{b.range}</span></div>
            <div className="mg-track" role="img" aria-label={`${b.name}: ${b.range}`}>
              <div className="mg-fill" style={{ width: `${pos(b.db)}%`, background: b.tone }} />
            </div>
            <div className="mg-note" style={{ color: "var(--text-secondary)" }}>{b.note}</div>
          </li>
        ))}
      </ul>
      <figcaption id={`${id}-cap`}>
        A rough guide to the backscatter scale. Real values depend on the radar's polarization, the angle it looks from, moisture and roughness, which is why Terra always compares the same place, from the same track, on two dates.
      </figcaption>
    </figure>
  );
}
