export type Bounds = [number, number, number, number];
export type Region = { name: string; context: string; bbox: Bounds; tag: string };
export type Observation = {
  id: string; title: string; date: string; product: string; maturity: string;
  polarizations: string[]; track?: number | string; frame?: number | string;
  direction?: string; bbox: Bounds; browse_url?: string; download_url?: string;
  size_mb?: number; collection?: string; local?: boolean; [key: string]: unknown;
};
export type Layer = { id: string; title: string; path: string; url?: string; date?: string; bounds: Bounds; units: string; min: number; max: number; colormap: string };
export type Investigation = {
  id: string; title: string; question: string; bbox: Bounds; observation_ids: string[];
  mode: string; status: string; stage: string; progress: number; blocker?: string;
  created_at: string; updated_at: string;
  events: {stage: string; status: string; message: string; at: string}[];
  result?: { measurements: Record<string, unknown>; validation: {status: string; independent_validation?: string; checks: unknown[]; limitations: string[]}; layers: Layer[]; provenance: unknown; report: unknown };
};
export type Health = { status: string; temporal: {connected: boolean}; capabilities: Record<string, boolean | string>; inputs_count: number };
export type GeoResult = { name: string; context: string; kind?: string; lon: number; lat: number; bbox: Bounds | null };
/** Featured sites where surface change is well documented. Search reaches anywhere on Earth. */
export const REGIONS: Region[] = [
  { name: 'Pacaya–Samiria', context: 'Peruvian Amazon', bbox: [-74.800,-5.540,-74.500,-5.260], tag: 'Wetlands' },
  { name: 'Mexico City', context: 'Mexico', bbox: [-99.225,19.285,-98.925,19.565], tag: 'Subsidence' },
  { name: 'Jakarta', context: 'Java, Indonesia', bbox: [106.700,-6.300,107.000,-6.050], tag: 'Coastal subsidence' },
  { name: 'Kīlauea', context: 'Hawaiʻi, United States', bbox: [-155.400,19.300,-155.100,19.500], tag: 'Volcano' },
  { name: 'Mount Etna', context: 'Sicily, Italy', bbox: [14.850,37.600,15.150,37.850], tag: 'Volcano' },
  { name: 'Sermeq Kujalleq', context: 'Ilulissat, Greenland', bbox: [-50.000,69.080,-49.700,69.250], tag: 'Calving glacier' },
  { name: 'Skeiðarárjökull', context: 'Vatnajökull, Iceland', bbox: [-17.425,63.950,-17.125,64.150], tag: 'Outlet glacier' },
  { name: 'Brahmaputra floodplain', context: 'Assam, India', bbox: [91.625,26.150,91.925,26.420], tag: 'Floodplain' },
  { name: 'Corcoran, San Joaquin Valley', context: 'California, United States', bbox: [-119.725,36.000,-119.425,36.250], tag: 'Groundwater subsidence' },
  { name: 'Mississippi Delta', context: 'Louisiana, United States', bbox: [-89.425,29.100,-89.125,29.380], tag: 'Land loss' },
  { name: 'Aral Sea, eastern basin', context: 'Kazakhstan and Uzbekistan', bbox: [59.675,45.010,59.975,45.290], tag: 'Drying sea' },
  { name: 'Rondônia frontier', context: 'Brazil', bbox: [-63.175,-10.090,-62.875,-9.810], tag: 'Deforestation' },
];
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { ...options, headers: {'Content-Type':'application/json', ...options?.headers} });
  if (!res.ok) { const value = await res.json().catch(() => ({})); const detail=value.detail; throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `Request failed (${res.status})`); }
  return res.json();
}
// Acquisition times are UTC; showing them in local time would shift dates across midnight.
export const prettyDate = (s: string, short = false) => new Date(s).toLocaleDateString('en-GB', short ? {day:'2-digit', month:'short', timeZone: 'UTC'} : {day:'2-digit',month:'short',year:'numeric', timeZone: 'UTC'});
export function safeUrl(url?: string): string | undefined { if (!url) return; try { const u = new URL(url, location.origin); return ['http:','https:'].includes(u.protocol) ? u.href : undefined; } catch { return; } }
