import type { Hotspot } from './Dossier';
import './polarization.css';

/** One row of `change.polarizations` from the science step (science._polarization_change). */
export type PolRow = {
  polarization: string; valid_pixels?: number; enl_before?: number | null; enl_after?: number | null;
  mean_change_db?: number | null; median_change_db?: number | null; threshold_exceedance_pct?: number | null;
  expected_speckle_sd_db?: number | null; mean_abs_standardized_change?: number | null; standardized_exceedance_pct?: number | null;
};

/** GCOV names the diagonal terms HHHH, HVHV, ...; people say HH, HV. */
export const channel = (p?: string | null) => p ? (p.length === 4 ? p.slice(0, 2) : p) : '';
const KIND: Record<string, string> = { HH: 'co-polarized', VV: 'co-polarized', HV: 'cross-polarized', VH: 'cross-polarized' };
const fmt = (v: number | null | undefined, digits = 1) => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits).replace(/^-/, '−') : '—';
/** Channels within this factor of each other are reported as showing the change about equally. */
const CLEAR = 1.1;
const list = (xs: (string | number)[]) => xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs.at(-1)}`;

/** The channel that carries a patch's change, or null when the channels are within 10% of each other. */
export function patchLead(h: Pick<Hotspot, 'by_polarization'>): { lead: string; z: number; other: string; z2: number } | null {
  const per = Object.entries(h.by_polarization ?? {}).map(([p, v]) => ({ p: channel(p), z: Math.abs(v.standardized_change) })).sort((a, b) => b.z - a.z);
  if (per.length < 2 || per[1].z <= 0 || per[0].z / per[1].z < CLEAR) return null;
  return { lead: per[0].p, z: per[0].z, other: per[1].p, z2: per[1].z };
}

/**
 * "Which polarization shows it": each channel's change in units of the speckle expected at its own look count, so the
 * channels can be compared fairly, for the whole area and for each changed patch. Close calls are called close.
 */
export default function Polarizations({ rows, leading, hotspots = [] }: { rows?: PolRow[]; leading?: string | null; hotspots?: Hotspot[] }) {
  if (!rows?.length) return null;
  const scored = rows.filter(r => typeof r.mean_abs_standardized_change === 'number');
  if (rows.length < 2 || scored.length < 2) {
    const only = rows.map(r => channel(r.polarization)).join('/');
    return <p className="pol-single">Only {only} was recorded on both dates, so this change can’t be split by polarization. Pairs where both dates carry HH and HV can.</p>;
  }
  const order = [...scored].sort((a, b) => b.mean_abs_standardized_change! - a.mean_abs_standardized_change!);
  const top = order[0].mean_abs_standardized_change!;
  const lead = channel(leading) || channel(order[0].polarization);
  const first = order.find(r => channel(r.polarization) === lead) ?? order[0];
  const second = order.find(r => r !== first)!;
  const z1 = first.mean_abs_standardized_change!, z2 = second.mean_abs_standardized_change!;
  const [a, b] = [channel(first.polarization), channel(second.polarization)];

  const area = z1 < 1
    ? <>Across the whole area both channels sit near the speckle level ({a} {fmt(z1)}σ, {b} {fmt(z2)}σ): most of the ground did not change.</>
    : z2 > 0 && z1 / z2 < CLEAR
      ? <>Across the whole area both channels show the change about equally ({a} {fmt(z1)}σ, {b} {fmt(z2)}σ).</>
      : <><b>{a}</b>{KIND[a] ? ` (${KIND[a]})` : ''} shows the change more clearly: {fmt(z1)}σ against {fmt(z2)}σ in {b}.</>;

  const split = hotspots.filter(h => Object.keys(h.by_polarization ?? {}).length > 1);
  const byLead = new Map<string, Hotspot[]>(); const even: number[] = [];
  for (const h of split) { const l = patchLead(h); if (l) byLead.set(l.lead, [...(byLead.get(l.lead) ?? []), h]); else even.push(h.rank); }
  const way = (hs: Hotspot[]) => { const d = new Set(hs.map(h => h.direction)); return d.size === 1 ? (d.has('increase') ? 'brighter' : 'darker') : ''; };
  const clauses = [...byLead.entries()].map(([p, hs]) => `${p} in patch${hs.length > 1 ? 'es' : ''} ${list(hs.map(h => h.rank))}${way(hs) ? ` (${way(hs)})` : ''}`);
  const patches = split.length === 0 ? null : <>
    In the changed patches, {clauses.length ? <>the change is clearer in {list(clauses)}</> : <>both channels show the change about equally</>}
    {clauses.length > 0 && even.length > 0 && <>; patch{even.length > 1 ? 'es' : ''} {list(even)} show{even.length > 1 ? '' : 's'} about equally in both</>}.</>;

  return <div className="pol">
    <span className="label">Which polarization shows it</span>
    <p className="pol-verdict">{area}{patches && <> {patches}</>}</p>
    <ul>{order.map(r => {
      const p = channel(r.polarization); const z = r.mean_abs_standardized_change!;
      return <li key={p} className={p === lead && z1 >= 1 ? 'lead' : ''}>
        <span className="pol-name">{p}<small>{KIND[p] ?? ''}</small></span>
        <span className="pol-bar" aria-hidden="true"><i style={{ width: `${Math.max(3, (z / Math.max(top, 1)) * 100)}%` }} /><s style={{ left: `${100 / Math.max(top, 1)}%` }} /></span>
        <span className="pol-val num">{fmt(z)}σ<small>{fmt(r.standardized_exceedance_pct)}% ≥ 2σ · {fmt(r.mean_change_db, 2)} dB</small></span>
      </li>;
    })}</ul>
    <p className="pol-note">σ: change divided by the speckle expected at each channel’s number of looks. Speckle alone averages below 1σ (the tick); channels within 10% count as equal.
      This ranks the channels; it isn’t a significance test. Cross-polarized return comes mostly from volume scattering (vegetation, rough or blocky surfaces), co-polarized from surfaces and double bounce.</p>
  </div>;
}
