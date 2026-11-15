import { useEffect, useState } from 'react';
import { api, type Bounds } from './types';

/**
 * The drones' field evidence for an investigation (GET /api/fleet/evidence/{id}): each changed patch's radar
 * hypothesis, the aircraft observations judged against it, and a verdict to weigh. One request loop per
 * investigation serves every view that shows it (the investigation panel, the report, the drone camera and panel).
 */
export type Verdict = 'supports' | 'contradicts' | 'inconclusive';
export type Judged = { note_id: number; t: number; drone_id: string; lon?: number; lat?: number; verdict: Verdict; alternative?: string | null; reasons: string[]; surface_c?: number | null; cover?: string | null };
export type Brief = { rank: number; direction: 'increase' | 'decrease'; hypothesis: string | null; question: string | null; consistent_with: string[]; mechanism: string | null;
  basis: string | null; confidence: string | null; sensors: string[]; bounds: Bounds; mean_change_db: number | null; area_km2: number | null };
export type PatchEvidence = { target_id: string; label: string; lon: number; lat: number; brief: Brief; observations: number; aircraft: string[];
  counts: Record<Verdict, number>; overall: string; latest: Judged[]; alternatives?: string[]; points?: [number, number, Verdict, number][] };
export type Aircraft = { id: string; name: string; origin: 'virtual' | 'sitl' | 'real' | null };
export type FieldEvidence = { investigation_id?: string; patches: PatchEvidence[]; totals: Record<Verdict, number>; aircraft?: Aircraft[]; method?: string;
  recorded_at?: string | null; artifact?: string | null; observations?: number };

export const OVERALL: Record<string, string> = { supported: 'Supported in the field', contradicted: 'Contradicted in the field', mixed: 'Mixed field evidence',
  inconclusive: 'Observed, inconclusive', 'alternative explanation': 'Another explanation fits better', 'not yet observed': 'Not yet observed' };
export const SHORT: Record<string, string> = { supported: 'Supported', contradicted: 'Contradicted', mixed: 'Mixed', inconclusive: 'Inconclusive',
  'alternative explanation': 'Other explanation', 'not yet observed': 'Not yet observed' };
export const VERDICT: Record<Verdict, string> = { supports: 'supports', contradicts: 'contradicts', inconclusive: 'inconclusive' };
/** The changed patch an aircraft's task belongs to (planner job keys look like orbit:patch-3, survey:patch-1). */
export const patchOf = (jobKey: unknown) => { const m = /patch-(\d+)/.exec(String(jobKey ?? '')); return m ? Number(m[1]) : null; };
export const ago = (t: number) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`; };

type Entry = { value: FieldEvidence | null; error: string; listeners: Set<() => void>; timer: ReturnType<typeof setInterval> | null };
const feeds = new Map<string, Entry>();

async function load(id: string) {
  const e = feeds.get(id); if (!e) return;
  try { e.value = await api<FieldEvidence>(`/fleet/evidence/${encodeURIComponent(id)}`); e.error = ''; }
  catch (err) { e.error = err instanceof Error ? err.message : 'Field evidence is unavailable.'; }
  e.listeners.forEach(f => f());
}

/** Fetch now (after a dispatch, say) instead of waiting for the next poll. */
export const refreshEvidence = (id: string) => { if (feeds.get(id)?.listeners.size) void load(id); };

export function useFieldEvidence(id: string | null | undefined, every = 5000): { value: FieldEvidence | null; error: string } {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!id) return;
    let e = feeds.get(id);
    if (!e) { e = { value: null, error: '', listeners: new Set(), timer: null }; feeds.set(id, e); }
    const listener = () => tick(n => n + 1); e.listeners.add(listener);
    if (!e.timer) { void load(id); e.timer = setInterval(() => { void load(id); }, every); }
    return () => { const x = feeds.get(id); if (!x) return; x.listeners.delete(listener); if (!x.listeners.size && x.timer) { clearInterval(x.timer); x.timer = null; } };
  }, [id, every]);
  const e = id ? feeds.get(id) : undefined;
  return { value: e?.value ?? null, error: e?.error ?? '' };
}

/** Send the fleet to verify every changed patch of an investigation. */
export async function dispatchFleet(id: string): Promise<number> {
  const r = await api<{ targets: unknown[] }>('/fleet/dispatch', { method: 'POST', body: JSON.stringify({ investigation_id: id }) });
  refreshEvidence(id); return r.targets.length;
}
