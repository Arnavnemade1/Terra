import type { ReactNode } from "react";

/** A figure, rendered as itself: no count-up, so it is never shown as 0 (in previews, print or reduced motion). */
function Count({ to, decimals = 0, suffix = "" }: { to: number; decimals?: number; suffix?: string }) {
  const text = to.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).replace(/^-/, "\u2212");
  return (
    <span className="bento-num">
      {text}
      {suffix && <span className="bento-unit">{suffix}</span>}
    </span>
  );
}

function Cell({ className, number, title, art, children }: { className: string; number: ReactNode; title: string; art?: ReactNode; children: ReactNode }) {
  return (
    <article className={`bento-cell ${className}`}>
      {art}
      {number}
      <h3>{title}</h3>
      <p>{children}</p>
    </article>
  );
}

export function Bento() {
  return (
    <div className="bento">
      <Cell className="cell-a" number={<Count to={0.1} decimals={1} suffix=" %" />} title="Chance that speckle alone is called change" art={<img src="/brand/waves.webp" alt="" aria-hidden="true" />}>
        Terra subtracts the earlier radar image from the later one, in decibels, and sets the threshold from that scene's own change histogram (Kittler–Illingworth). Where no clear change class exists, it uses the smallest change that radar speckle alone would exceed only 0.1% of the time, and 3 dB only if neither can be computed. The report says which rule it used.
      </Cell>
      <Cell className="cell-b" number={<Count to={6} />} title="Hotspots per analysis">
        The six strongest patches of change, outlined on the map and named in the report.
      </Cell>
      <Cell className="cell-c" number={<Count to={5} />} title="NASA data sources">
        NISAR, FIRMS, EONET, GIBS and the CMR catalog, all read live.
      </Cell>
      <Cell className="cell-d" number={<Count to={-18} suffix=" dB" />} title="Where land starts to look like water">
        Smooth open water bounces radar away from the satellite, so it returns very little. Pixels darker than −18 dB are reported as water-like area, and pixels brighter than −3 dB as very bright.
      </Cell>
    </div>
  );
}
