import { useRef, useState, useEffect, type ReactNode } from "react";
import { useInView } from "motion/react";
import { SlotText } from "@/components/arc/slot-text/slot-text";

/** Rolls from 0 to the value once the cell scrolls into view. */
function Count({ to, decimals = 0, suffix = "" }: { to: number; decimals?: number; suffix?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const seen = useInView(ref, { once: true, margin: "0px 0px -15% 0px" });
  const [v, setV] = useState(0);
  useEffect(() => { if (seen) setV(to); }, [seen, to]);
  return (
    <span ref={ref} className="bento-num">
      <SlotText value={v} format={(n) => n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })} />
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
      <Cell className="cell-a" number={<Count to={3} suffix=" dB" />} title="The smallest change we call change" art={<img src="/brand/waves-transparent.png" alt="" aria-hidden="true" />}>
        Terra subtracts the earlier radar image from the later one, in decibels. A pixel counts as changed when it moved by 3 dB or more. The threshold is fixed and descriptive, so you can always see what was measured.
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
