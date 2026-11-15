import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { ArrowUpRight, BarChart3, Calendar, Check, ChevronDown, ChevronUp, Copy, Expand, Eye, Info, Menu as MenuIcon, Navigation, Plane, Satellite, Settings, Share2, Shrink, X } from 'lucide-react';
import Globe, { type GlobeHandle, type Hover, type Mark, type Overlay, type Telemetry } from './Globe';
import { decodeGrid, type Grid, type ReliefOptions, type TerrainContext } from './relief';
import Search, { boxAround, type Hazard } from './Search';
import Dossier, { artifactUrl, type Hotspot } from './Dossier';
import Compare from './Compare';
import { DISPLAY_MODES, type DisplayMode } from './displayModes';
import { ACCENT, EVENT_STYLE, type FeedState, type LayerKey, type Selection } from './liveLayers';
import { boxAreaKm2, decimal, dmsPretty, mgrs, scaleBar, subPoint } from './geo';
import { api, prettyDate, REGIONS, safeUrl, type Bounds, type Health, type Investigation, type Layer, type Observation, type Region } from './types';
import { ACTIVE, Composer, ContextBlock, DEFAULT_QUESTION, Results, Steps, num, seconds, type Context, type Narrative, type Plan, type Result, type Stats } from './Analysis';
import { Card, ConditionsGrid, HazardsPanel, KeyValues, LayersList, SatellitesPanel, Swatch, hazardColor, type LayerRowSpec, type Profile } from './Panels';
import { EventList, Timeline, stamp, type EventItem, type Marker } from './Dock';
import { Bars, ChartFrame, Columns, LinePlot } from './charts';
import { crossTrack, predictPasses, swathPass, SWATH, trackSeries, useFleet, type Sat } from './fleet';
import SensorView from './SensorView';
import DroneFeed, { type Thermal } from './DroneFeed';
import DronesPanel from './DronesPanel';
import FleetOps, { DeployCard } from './FleetOps';
import Verification, { type SignedSpot } from './Verification';
import { droneStore, type Patch, type Place, type Profile as FleetProfile } from './drones';
import { nextApproach } from './geo';

type Mode = 'investigation' | 'hazards' | 'satellites' | 'drones';
type Tab = 'layers' | 'analysis' | 'evidence' | 'notes';
type LayerId = 'terrain' | 'imagery' | 'contours' | 'landcover' | 'trees' | 'peaks' | 'roads' | 'drones' | 'radar' | 'change' | 'hotspots' | 'optical' | 'nisar' | 'coverage' | 'events' | 'quakes' | 'fires' | 'fleet' | 'labels';
type Setting = { on: boolean; value: number };
type Quake = { id: string; place: string; mag: number; lon: number; lat: number; time: number; url?: string; alert?: string | null };
type Probe = { lon: number; lat: number; values: { layer: string; value: number | null; units: string }[] } | null;
type Note = { id: string; t: number; text: string };

const DAY = 86_400_000;
const INITIAL = new URLSearchParams(location.search);
const coord = (lat: number, lon: number) => decimal(lat, lon, 3);
const capital = (s?: string) => s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
const VIRIDIS = ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'];
const DIVERGING = ['#2d61a5', '#74add1', '#f2f2e8', '#e9965a', '#b44237'];
function ramp(stops: string[], t: number) {
  const x = Math.min(0.999, Math.max(0, t)) * (stops.length - 1); const i = Math.floor(x); const f = x - i;
  const a = stops[i].match(/\w\w/g)!.map(h => parseInt(h, 16)), b = stops[i + 1].match(/\w\w/g)!.map(h => parseInt(h, 16));
  return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(',')})`;
}
function tile(lat: number, lon: number, z: number) {
  const x = Math.floor((lon + 180) / 360 * 2 ** z); const r = lat * Math.PI / 180;
  const y = Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * 2 ** z); return `${z}/${y}/${x}`;
}
const readJSON = <T,>(key: string, fallback: T): T => { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) as T : fallback; } catch { return fallback; } };
const writeJSON = (key: string, value: unknown) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* per-viewer convenience only */ } };
const DEFAULT_LAYERS: Record<LayerId, Setting> = {
  terrain: { on: true, value: 1 }, imagery: { on: true, value: 1 }, contours: { on: true, value: 1 }, landcover: { on: true, value: 1 }, trees: { on: true, value: 1 },
  peaks: { on: true, value: 1 }, roads: { on: true, value: 1 }, radar: { on: false, value: 0.9 }, change: { on: true, value: 0.85 }, hotspots: { on: true, value: 1 },
  optical: { on: false, value: 1 }, nisar: { on: true, value: 1 }, coverage: { on: false, value: 1 }, events: { on: true, value: 1 }, quakes: { on: true, value: 1 },
  fires: { on: false, value: 1 }, fleet: { on: false, value: 1 }, labels: { on: true, value: 0.9 }, drones: { on: true, value: 1 },
};

// Camera telemetry arrives at 4 Hz; keep it out of App's render path.
let telemetryValue: Telemetry | null = null;
const telemetryListeners = new Set<() => void>();
const telemetryStore = {
  set(t: Telemetry) { telemetryValue = t; telemetryListeners.forEach(l => l()); },
  subscribe(l: () => void) { telemetryListeners.add(l); return () => { telemetryListeners.delete(l); }; },
  get: () => telemetryValue,
};
// The terrain under the pointer, also kept out of App's render path.
let hoverValue: Hover | null = null;
const hoverListeners = new Set<() => void>();
const hoverStore = {
  set(h: Hover | null) { hoverValue = h; hoverListeners.forEach(l => l()); },
  subscribe(l: () => void) { hoverListeners.add(l); return () => { hoverListeners.delete(l); }; },
  get: () => hoverValue,
};

export default function App() {
  const globe = useRef<GlobeHandle>(null);
  const [region, setRegion] = useState<Region>(REGIONS[3]);
  const [mode, setMode] = useState<Mode>('investigation');
  const [tab, setTab] = useState<Tab>('analysis');
  const [observations, setObservations] = useState<Observation[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [activeObservation, setActiveObservation] = useState<Observation>();
  const [health, setHealth] = useState<Health>();
  const [loading, setLoading] = useState(true);
  const [catalogError, setCatalogError] = useState('');
  const [error, setError] = useState('');
  const [question, setQuestion] = useState('');
  const [analysis, setAnalysis] = useState<'backscatter' | 'change'>('change');
  const [manual, setManual] = useState(false);
  const [job, setJob] = useState<Investigation>();
  const [jobs, setJobs] = useState<Investigation[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);
  const [flat, setFlat] = useState(false);
  // The shaded relief map is the default look (terrain, contours, land cover, summits); a saved choice wins.
  const [style, setStyle] = useState<MapStyle>(() => { const v = readJSON<MapStyle>('terra.style', 'relief'); return v === 'photo' || v === 'photoreal' ? v : 'relief'; });
  // Google Photorealistic 3D Tiles: offered only when the server has a working Map Tiles API key.
  const [photorealState, setPhotorealState] = useState<{ available: boolean; reason: string | null } | null>(null);
  const [terrain, setTerrain] = useState<{ ctx: TerrainContext; grid: Grid | null } | null>(null);
  const [terrainState, setTerrainState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [drawingProfile, setDrawingProfile] = useState(false);
  const [profileVertices, setProfileVertices] = useState<[number, number][]>([]);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileError, setProfileError] = useState('');
  const [profileHover, setProfileHover] = useState<number | null>(null);
  const [ls, setLs] = useState<Record<LayerId, Setting>>(() => ({ ...DEFAULT_LAYERS, ...readJSON<Partial<Record<LayerId, Setting>>>('terra.layers', {}) }));
  const [display, setDisplay] = useState<DisplayMode>('normal');
  const [quality, setQuality] = useState('balanced');
  const [lighting, setLighting] = useState(false);
  const [footprints, setFootprints] = useState(true);
  const [feeds, setFeeds] = useState<Record<string, FeedState>>({});
  const [selection, setSelection] = useState<Selection | null>(null);
  const [menu, setMenu] = useState<'area' | 'view' | 'analysis' | 'share' | 'more' | 'sources' | 'about' | null>(null);
  const [detail, setDetail] = useState(false);
  const [dossier, setDossier] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [context, setContext] = useState<Context | null>(null);
  const [probe, setProbe] = useState<Probe>(null);
  const [hazards, setHazards] = useState<Hazard[]>([]);
  const [quakes, setQuakes] = useState<Quake[]>([]);
  const [hazardState, setHazardState] = useState<'loading' | 'ready' | 'partial' | 'offline'>('loading');
  const [selectedHazard, setSelectedHazard] = useState<Hazard | null>(null);
  const [focusSpot, setFocusSpot] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [t, setT] = useState(Date.now());
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [notes, setNotes] = useState<Note[]>([]);
  const [noteText, setNoteText] = useState('');
  const [activeEvent, setActiveEvent] = useState<string | null>(null);
  const [selectedSat, setSelectedSat] = useState<number>(65053);
  const [sensor, setSensor] = useState<{ norad: number; at: number } | null>(null);
  const [selectedDrone, setSelectedDrone] = useState<string | null>(null);
  const [feed, setFeed] = useState<{ id: string; expanded: boolean } | null>(null);
  // The drone camera is created once the Drones workspace is first opened and then kept (warm, out of sight) so
  // opening a camera is immediate; it follows the opened aircraft, else the selected one.
  const [firstDrone, setFirstDrone] = useState<string | null>(null);
  useEffect(() => droneStore.subscribe(() => setFirstDrone(droneStore.ids()[0] ?? null)), []);
  const [dockOpen, setDockOpen] = useState<boolean>(() => readJSON('terra.dock', false));
  const [sensorBig, setSensorBig] = useState(false);
  const [copied, setCopied] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const fleet = useFleet();
  const writer = health?.capabilities?.language_agent ? String(health.capabilities.language_model || 'a language model') : null;
  const originals = Boolean(health?.capabilities?.original_products);

  // ---------------------------------------------------------------- data
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setCatalogError(''); setObservations([]); setActiveObservation(undefined); setSelectedIds([]);
    (async () => {
      try {
        const [catalog, local] = await Promise.all([
          api<{ observations: Observation[] }>(`/observations?bbox=${region.bbox.join(',')}&limit=60`, { signal: controller.signal }),
          api<{ observations?: Observation[] }>('/inputs', { signal: controller.signal }).catch(() => ({ observations: [] })),
        ]);
        if (controller.signal.aborted) return;
        const b = region.bbox;
        const inside = (o: Observation) => o.bbox && o.bbox[0] < b[2] && o.bbox[2] > b[0] && o.bbox[1] < b[3] && o.bbox[3] > b[1];
        const ids = new Set(catalog.observations.map(o => o.id));
        const all = [...catalog.observations, ...(local.observations || []).filter(inside).filter(o => !ids.has(o.id)).map(o => ({ ...o, local: true }))];
        setObservations(all);
        const first = all.find(o => !o.local) || all[0];
        if (first) setActiveObservation(first);
      } catch (e) { if (!controller.signal.aborted) setCatalogError(e instanceof Error ? e.message : 'The catalog search failed.'); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    })();
    return () => controller.abort();
  }, [region, refresh]);
  useEffect(() => {
    setContext(null); const controller = new AbortController();
    void api<Context>(`/context?bbox=${region.bbox.join(',')}`, { signal: controller.signal }).then(setContext).catch(() => {});
    return () => controller.abort();
  }, [region]);
  useEffect(() => {
    let alive = true; const update = () => { void api<Health>('/health').then(v => { if (alive) setHealth(v); }).catch(() => { if (alive) setHealth(undefined); }); };
    update(); const timer = setInterval(update, 15000); return () => { alive = false; clearInterval(timer); };
  }, []);
  useEffect(() => { void api<{ investigations: Investigation[] }>('/investigations').then(v => setJobs(v.investigations)).catch(() => {}); }, [job?.status, menu]);
  useEffect(() => {
    const c = new AbortController(); setHazardState('loading');
    void Promise.allSettled([
      api<{ items: Hazard[]; unavailable?: string[] }>('/feeds/events', { signal: c.signal }),
      api<{ items: Quake[] }>('/feeds/earthquakes', { signal: c.signal }),
    ]).then(([natural, quakeFeed]) => {
      if (c.signal.aborted) return;
      const items = natural.status === 'fulfilled' ? natural.value.items : [];
      const all = quakeFeed.status === 'fulfilled' ? quakeFeed.value.items : [];
      setQuakes(all);
      const qs: Hazard[] = all.filter(q => q.mag >= 4.5).map(q => ({ id: q.id, title: `M${q.mag.toFixed(1)} · ${q.place}`, category: 'earthquakes',
        category_title: 'Earthquake', lon: q.lon, lat: q.lat, date: new Date(q.time).toISOString(), source: q.url, provider: 'USGS', alert: q.alert || null }));
      setHazards([...items, ...qs]);
      setHazardState(natural.status === 'rejected' && quakeFeed.status === 'rejected' ? 'offline' : natural.status === 'rejected' || quakeFeed.status === 'rejected' ? 'partial' : 'ready');
    });
    return () => c.abort();
  }, []);
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 15_000); return () => clearInterval(id); }, []);
  useEffect(() => { writeJSON('terra.layers', ls); }, [ls]);
  useEffect(() => { writeJSON('terra.style', style); }, [style]);
  useEffect(() => {
    let alive = true;
    void api<{ available: boolean; reason: string | null }>('/connector/google3d/status')
      .then(v => { if (alive) setPhotorealState(v); })
      .catch(() => { if (alive) setPhotorealState({ available: false, reason: 'The map service did not answer.' }); });
    return () => { alive = false; };
  }, []);
  const photorealReady = Boolean(photorealState?.available);
  // A saved Photoreal choice falls back to terrain and imagery until (and unless) the tiles are available.
  const mapStyle: Exclude<MapStyle, 'photoreal'> | 'photoreal' = style === 'photoreal' && !photorealReady ? 'photo' : style;
  const onPhotorealError = useCallback((message: string) => { setPhotorealState({ available: false, reason: message }); }, []);
  useEffect(() => { writeJSON('terra.dock', dockOpen); }, [dockOpen]);
  // Land cover and named features for the relief map and the pointer readout (areas up to 2 degrees across).
  useEffect(() => {
    setTerrain(null); const b = region.bbox;
    if (b[2] - b[0] > 2 || b[3] - b[1] > 2) { setTerrainState('idle'); return; }
    const c = new AbortController(); setTerrainState('loading');
    void api<TerrainContext>(`/terrain?bbox=${b.join(',')}`, { signal: c.signal }).then(async ctx => {
      const grid = ctx.landcover ? await decodeGrid(ctx.landcover).catch(() => null) : null;
      if (!c.signal.aborted) { setTerrain({ ctx, grid }); setTerrainState(ctx.unavailable.length ? 'error' : 'ready'); }
    }).catch(() => { if (!c.signal.aborted) setTerrainState('error'); });
    return () => c.abort();
  }, [region]);

  const result = job?.result as Result | undefined;
  const running = Boolean(job && ACTIVE.includes(job.status));
  const enriching = job?.status === 'completed' && result?.enrichment_status === 'pending';
  useEffect(() => {
    if (!job || (!running && !enriching)) return;
    const timer = setInterval(() => { void api<Investigation>(`/investigations/${job.id}`).then(setJob).catch(e => setError(e.message)); }, enriching ? 2500 : 800);
    return () => clearInterval(timer);
  }, [job?.id, running, enriching]);
  useEffect(() => { if (!running) return; const id = setInterval(() => setNow(Date.now()), 500); return () => clearInterval(id); }, [running]);

  // Notes and user events belong to the investigation, or to the area before one exists.
  const noteKey = `terra.notes.${job?.id || region.bbox.map(v => v.toFixed(3)).join(',')}`;
  useEffect(() => { setNotes(readJSON<Note[]>(noteKey, [])); }, [noteKey]);
  const saveNotes = (next: Note[]) => { setNotes(next); writeJSON(noteKey, next); };

  // ---------------------------------------------------------------- links and keys
  const linked = useRef(INITIAL.get('investigation'));
  useEffect(() => { const id = linked.current; if (id && /^[\w-]{8,64}$/.test(id)) void openJob({ id } as Investigation); }, []);
  useEffect(() => { if (job?.result && job.id === linked.current && INITIAL.get('dossier') === '1') setDossier(true); }, [job?.id, Boolean(job?.result)]);
  useEffect(() => {
    if (!job && linked.current) return;
    const url = new URL(location.href); const current = url.searchParams.get('investigation');
    if (job?.id && current !== job.id) { url.searchParams.set('investigation', job.id); url.searchParams.delete('dossier'); history.replaceState(null, '', url); }
    if (!job && current) { url.searchParams.delete('investigation'); url.searchParams.delete('dossier'); history.replaceState(null, '', url); }
  }, [job?.id]);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement;
      if (e.key === 'Escape') { setMenu(null); setDetail(false); setPicking(false); setDrawingProfile(false); setSelection(null); setDeploy(null); stopFollowing(); }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === '/') { e.preventDefault(); setMenu('area'); }
      if (e.key === ' ') { e.preventDefault(); setPlaying(p => !p); }
      if (e.key === 'l') { setMode('investigation'); setTab('layers'); }
      if (e.key === 't') setDockOpen(v => !v);
      const k = Number(e.key); if (k >= 1 && k <= DISPLAY_MODES.length) setDisplay(DISPLAY_MODES[k - 1].id);
    };
    window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler);
  }, []);

  // ---------------------------------------------------------------- map callbacks
  const onTelemetry = useCallback((tel: Telemetry) => telemetryStore.set(tel), []);
  const onHover = useCallback((h: Hover | null) => hoverStore.set(h), []);
  const onFeeds = useCallback((s: Record<string, FeedState>) => setFeeds(s), []);
  const onSelect = useCallback((s: Selection | null) => { setSelection(s); if (s) setMenu(null); }, []);
  const jobRef = useRef(job); jobRef.current = job;
  const onProbe = useCallback((lon: number, lat: number) => {
    const j = jobRef.current; if (!j) return;
    void api<NonNullable<Probe>>(`/investigations/${j.id}/sample?lon=${lon}&lat=${lat}`).then(setProbe).catch(() => setProbe({ lon, lat, values: [] }));
  }, []);
  const onPathPoint = useCallback((lon: number, lat: number) => { setProfileVertices(v => { if (v.length !== 1) return [[lon, lat]]; setDrawingProfile(false); return [v[0], [lon, lat]]; }); }, []);
  useEffect(() => {
    if (profileVertices.length !== 2) return; const c = new AbortController(); setProfileLoading(true); setProfileError('');
    void api<Profile>('/profile', { method: 'POST', body: JSON.stringify({ vertices: profileVertices, samples: 100, investigation_id: job?.id }), signal: c.signal })
      .then(v => { if (!c.signal.aborted) setProfile(v); }).catch(e => { if (!c.signal.aborted) setProfileError(e.message); }).finally(() => { if (!c.signal.aborted) setProfileLoading(false); });
    return () => c.abort();
  }, [profileVertices, job?.id]);

  function selectRegion(r: Region, fly: Bounds | null = r.bbox) {
    linked.current = null; setMode('investigation'); setTab('analysis'); setProfile(null); setProfileVertices([]); setDrawingProfile(false);
    setRegion(r); setPicking(false); setJob(undefined); setProbe(null); setDossier(false); setMenu(null);
    if (fly) globe.current?.flyTo(fly);
  }
  function pick(b: Bounds) {
    if (b[2] - b[0] > 10 || b[3] - b[1] > 10) { setError('Draw an area smaller than 10° on each side.'); setPicking(false); return; }
    selectRegion({ name: 'Drawn area', context: coord((b[1] + b[3]) / 2, (b[0] + b[2]) / 2), bbox: b, tag: 'Custom' });
  }
  const quickPick = useCallback((lon: number, lat: number) => {
    linked.current = null; setRegion({ name: 'Marked point', context: coord(lat, lon), bbox: boxAround(lon, lat), tag: 'Custom' });
    setJob(undefined); setSelection(null); setProbe(null); setMode('investigation'); setTab('analysis');
  }, []);
  function toggleObservation(o: Observation) { setActiveObservation(o); setSelectedIds(ids => ids.includes(o.id) ? ids.filter(id => id !== o.id) : analysis === 'change' ? [...ids.slice(-1), o.id] : [o.id]); }
  async function launch(auto: boolean) {
    const q = question.trim() || DEFAULT_QUESTION;
    setSubmitting(true); setError(''); setProbe(null);
    try {
      const body = auto ? { title: region.name, question: q, bbox: region.bbox, auto: true, observation_ids: [] }
        : { title: `${region.name} · ${analysis === 'change' ? 'Backscatter change' : 'Radar backscatter'}`, question: q, bbox: region.bbox, observation_ids: selectedIds, mode: analysis };
      const created = await api<Investigation>('/investigations', { method: 'POST', body: JSON.stringify(body) });
      setJob(created); setNow(Date.now()); if (!question.trim()) setQuestion(q); globe.current?.flyTo(region.bbox);
    } catch (e) { setError(e instanceof Error ? e.message : 'The investigation could not start.'); }
    finally { setSubmitting(false); }
  }
  async function action(name: 'cancel' | 'resume') { if (!job) return; try { setError(''); setJob(await api<Investigation>(`/investigations/${job.id}/${name}`, { method: 'POST' })); } catch (e) { setError(e instanceof Error ? e.message : 'That action failed.'); } }
  async function openJob(j: Investigation) {
    try {
      const value = await api<Investigation>(`/investigations/${j.id}`);
      setMode('investigation'); setTab('analysis'); setJob(value); setMenu(null); setProfile(null); setProfileVertices([]);
      setRegion({ name: value.title.split(' · ')[0], context: 'Saved investigation', bbox: value.bbox, tag: 'Saved' }); globe.current?.flyTo(value.bbox);
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not open that investigation.'); }
  }
  const newQuestion = () => { linked.current = null; setJob(undefined); setProbe(null); setMode('investigation'); setTab('analysis'); setMenu(null); setTimeout(() => questionRef.current?.focus(), 50); };

  // ---------------------------------------------------------------- derived investigation state
  const radar = useMemo(() => (result?.layers || []).filter(l => l.id.startsWith('backscatter-')).sort((a, b) => String(a.date).localeCompare(String(b.date))), [result]);
  const delta = result?.layers?.find(l => l.id === 'change');
  const optical = useMemo(() => [...(result?.optical_evidence || [])].sort((a, b) => a.date.localeCompare(b.date)), [result]);
  const m = result?.measurements as { observations?: Stats[]; change?: Stats } | undefined;
  const change = m?.change; const obs = m?.observations || [];
  const hotspots = change?.hotspots ?? NO_HOTSPOTS;  // a stable empty list, so effects that depend on it do not re-run every render
  const pairTimes = radar.map(l => Date.parse(String(l.date)));
  const url = (l: Layer) => l.url || artifactUrl(job!, l.path.split('/').pop() || '');
  // The timeline chooses which dated image is shown: the latest radar and true-colour view at or before the cursor.
  const radarNow = radar.length ? [...radar].reverse().find(l => Date.parse(String(l.date)) <= t) || radar[0] : undefined;
  const opticalNow = optical.length ? [...optical].reverse().find(o => Date.parse(o.date) <= t) || optical[0] : undefined;
  const overlays: Overlay[] = useMemo(() => {
    if (!job || !result) return [];
    const out: Overlay[] = [];
    if (opticalNow) out.push({ id: 'optical', url: artifactUrl(job, opticalNow.artifact_name), bounds: opticalNow.bounds as Bounds, alpha: ls.optical.value, show: ls.optical.on, smooth: true });
    if (radarNow) out.push({ id: 'radar', url: url(radarNow), bounds: radarNow.bounds, alpha: ls.radar.value, show: ls.radar.on });
    if (delta) out.push({ id: 'change', url: url(delta), bounds: delta.bounds, alpha: ls.change.value, show: ls.change.on });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, result, radarNow?.id, opticalNow?.artifact_name, ls.optical, ls.radar, ls.change]);
  const marks: Mark[] = useMemo(() => ls.hotspots.on ? hotspots.map(h => ({ id: String(h.rank), bounds: h.bounds, centre: h.centre,
    // Full callouts for the three largest patches (and the one in focus); the rest carry their number only, so labels never pile up.
    label: h.rank <= 3 || focusSpot === h.rank ? `${h.rank} · ${h.direction === 'increase' ? 'Brighter' : 'Darker'} ${num(h.mean_change_db ?? NaN, 1, true)} dB` : String(h.rank),
    color: h.direction === 'increase' ? '#f0a35e' : '#7fb0e0', active: focusSpot === h.rank })) : [],
  [hotspots, focusSpot, ls.hotspots.on]);
  const liveLayers = useMemo(() => ({ nisar: ls.nisar.on || mode === 'satellites', fleet: ls.fleet.on || mode === 'satellites', coverage: ls.coverage.on, quakes: ls.quakes.on || mode === 'hazards',
    fires: ls.fires.on, events: ls.events.on || mode === 'hazards' }) as Record<LayerKey, boolean>, [ls, mode]);

  const reliefOptions: ReliefOptions = useMemo(() => ({ contours: ls.contours.on, cover: ls.landcover.on, trees: ls.trees.on, features: ls.peaks.on, roads: ls.roads.on }),
    [ls.contours.on, ls.landcover.on, ls.trees.on, ls.peaks.on, ls.roads.on]);
  const terrainContext = useMemo(() => terrain ? { grid: terrain.grid, features: terrain.ctx.features } : null, [terrain]);

  // Default path for the terrain profile: west to east across the area through the largest changed patch.
  useEffect(() => {
    if (!result || profileVertices.length) return; const [w, , e] = region.bbox;
    const lat = hotspots[0]?.centre[0] ?? (region.bbox[1] + region.bbox[3]) / 2;
    setProfileVertices([[w, lat], [e, lat]]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, Boolean(result)]);

  // ---------------------------------------------------------------- drones
  const patches: Patch[] = useMemo(() => hotspots.map(h => ({ rank: h.rank, centre: h.centre, bounds: h.bounds, label: `change patch ${h.rank}` })), [hotspots]);
  const places: Place[] = useMemo(() => (terrain?.ctx.features || []).map(f => ({ name: f.name, lon: f.lon, lat: f.lat, kind: f.kind, elevation_m: f.elevation_m })), [terrain]);
  // The fleet lives in the server's central database; durable agents coordinate it. Publish this area and its targets
  // (changed patches first, then summits) so the coordinator can plan jobs, and follow the fleet once a second.
  useEffect(() => { droneStore.start(); return () => droneStore.stop(); }, []);
  // Browsing never moves the fleet. The monitoring area changes only when someone asks (Drones panel); when the
  // workspace shows the fleet's own area, new results (patches, summits) refresh its targets automatically.
  const [fleetReady, setFleetReady] = useState(false);
  useEffect(() => droneStore.subscribe(() => setFleetReady(droneStore.ready)), []);
  // The finished investigation for this area: the agents read its measured change for their field notes.
  const fleetInvestigation = job?.result ? job.id : null;
  const sameBox = (a: Bounds | null | undefined, b: Bounds) => Boolean(a) && a!.every((v, i) => Math.abs(v - b[i]) < 1e-6);
  useEffect(() => {
    if (!fleetReady || !droneStore.ready) return; const current = droneStore.info.area?.bbox;
    if (!current || !sameBox(current, region.bbox)) return;
    const id = setTimeout(() => { void droneStore.publishArea(region.name, region.bbox, patches, places, undefined, fleetInvestigation).catch(() => {}); }, 900);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fleetReady, region.bbox.join(','), patches, places]);
  const monitorHere = useCallback(async () => {
    await droneStore.publishArea(region.name, region.bbox, patches, places, undefined, fleetInvestigation);
    await droneStore.command('resume');
  }, [region, patches, places, fleetInvestigation]);
  const onFleetProfile = useCallback((p: FleetProfile) => {
    const a = droneStore.info.area; if (!a) return;
    void droneStore.publishArea(a.name, a.bbox, sameBox(a.bbox, region.bbox) ? patches : [], sameBox(a.bbox, region.bbox) ? places : [], p, sameBox(a.bbox, region.bbox) ? fleetInvestigation : a.investigation_id).catch(e => setError(e instanceof Error ? e.message : 'The flight profile could not be updated.'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [region, patches, places, fleetInvestigation]);
  const onFleetFocus = useCallback((lon: number, lat: number) => globe.current?.flyToPoint(lon, lat, 3500), []);
  // Deploy the fleet anywhere: the next click on the map is the launch site, with a square area around it.
  // Which aircraft the map is flying with (the chase camera), refreshed with the fleet.
  const [following, setFollowing] = useState<string | null>(null);
  useEffect(() => droneStore.subscribe(() => setFollowing(globe.current?.following() ?? null)), []);
  const followDrone = useCallback((id: string) => { globe.current?.followDrone(id); setFollowing(id); }, []);
  const stopFollowing = useCallback(() => { globe.current?.stopFollowing(); setFollowing(null); }, []);
  // Deploying the fleet: draw the perimeter on the map, place the launch pad, confirm.
  const [deploy, setDeploy] = useState<{ stage: 'draw' | 'confirm'; area: Bounds | null; pad: [number, number] | null } | null>(null);
  const onDeployArea = useCallback((b: Bounds) => { stopFollowing(); setDeploy({ stage: 'confirm', area: b, pad: [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2] }); }, [stopFollowing]);
  const onDeployPad = useCallback((lon: number, lat: number) => setDeploy(d => {
    if (!d?.area) return d; const [w, s2, e, n] = d.area; const mx = (e - w) * 0.25, my = (n - s2) * 0.25;
    return lon >= w - mx && lon <= e + mx && lat >= s2 - my && lat <= n + my ? { ...d, pad: [lon, lat] } : d;
  }), []);
  const launchDeployment = useCallback(async (name: string, profile: FleetProfile) => {
    if (!deploy?.area) return; const area = deploy.area;
    await droneStore.publishArea(name, area, [], [], profile, null, deploy.pad ?? undefined);
    await droneStore.command('resume');
    setDeploy(null); globe.current?.flyTo(area);
  }, [deploy]);
  const [fleetCount, setFleetCount] = useState(0); const [liveDrones, setLiveDrones] = useState(0);
  useEffect(() => droneStore.subscribe(() => { const st = droneStore.states(); setFleetCount(st.filter(d => d.status === 'airborne').length); setLiveDrones(st.filter(d => d.source === 'real').length); }), []);
  const onSelectDrone = useCallback((id: string) => { setSelectedDrone(id); setMode('drones'); globe.current?.followDrone(id); }, []);
  // Release the second WebGL scene and its decoded imagery as soon as the camera closes.
  const cameraId = feed?.id ?? null;
  const onDroneSight = useCallback((p: { id: string; lon: number; lat: number } | null) => globe.current?.droneSight(p), []);
  const jobId = job?.id;
  const sampleChange = useCallback(async (lon: number, lat: number) => {
    if (!jobId) return null; const v = await api<{ values: { layer: string; value: number | null }[] }>(`/investigations/${jobId}/sample?lon=${lon}&lat=${lat}`);
    return v.values.find(x => x.layer === 'change')?.value ?? null;
  }, [jobId]);
  // Measured surface temperature for the drone cameras' IR mode, fetched when drones are in use.
  const [thermal, setThermal] = useState<Thermal | null>(null);
  const wantThermal = mode === 'drones' || Boolean(feed);
  // The scene covers the fleet's monitoring area (where the cameras are), or the browsed area before one is set.
  const [fleetBox, setFleetBox] = useState<string>('');
  const [fleetAreaInvestigation, setFleetAreaInvestigation] = useState<string | null>(null);
  useEffect(() => droneStore.subscribe(() => { setFleetBox(droneStore.info.area?.bbox.join(',') ?? ''); setFleetAreaInvestigation(droneStore.info.area?.investigation_id ?? null); }), []);
  const thermalBox = fleetBox || region.bbox.join(',');
  useEffect(() => {
    setThermal(null); const b = thermalBox.split(',').map(Number); if (!wantThermal || b.length !== 4 || b[2] - b[0] > 1 || b[3] - b[1] > 1) return; const c = new AbortController();
    void api<Thermal & { available: boolean }>(`/thermal?bbox=${thermalBox}`, { signal: c.signal }).then(v => { if (!c.signal.aborted && v.available) setThermal(v); }).catch(() => {});
    return () => c.abort();
  }, [thermalBox, wantThermal]);

  // ---------------------------------------------------------------- satellites
  const aoiCentre = { lat: (region.bbox[1] + region.bbox[3]) / 2, lon: (region.bbox[0] + region.bbox[2]) / 2 };
  const keySats = useMemo(() => fleet.sats.filter(s => s.key), [fleet.sats]);
  const passStart = Math.floor(now / 600_000) * 600_000;
  const passes = useMemo(() => mode !== 'hazards' ? predictPasses(keySats, aoiCentre.lat, aoiCentre.lon, passStart, 48, 60) : [],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [keySats, aoiCentre.lat, aoiCentre.lon, passStart, mode === 'satellites']);
  const sat: Sat | null = fleet.sats.find(s => s.norad === selectedSat) || null;
  // NISAR repeats its ground track every 12 days, so its next pass is searched over a full cycle.
  const nisarSat = fleet.sats.find(s => s.norad === 65053);
  // NISAR's radar looks to one side, so "next pass" means the next pass whose swath covers the area.
  const nisarNext = useMemo(() => { if (!nisarSat) return null; const at = swathPass(nisarSat, aoiCentre.lat, aoiCentre.lon, passStart, 13); if (!at) return null;
    return { norad: nisarSat.norad, name: nisarSat.name, group: nisarSat.group, at, km: Math.round(crossTrack(nisarSat, aoiCentre.lat, aoiCentre.lon, at)?.km ?? 0) }; },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nisarSat, aoiCentre.lat, aoiCentre.lon, Math.floor(passStart / 3_600_000)]);

  // ---------------------------------------------------------------- timeline per mode
  const timeline = useMemo(() => {
    if (mode === 'hazards') {
      const end = now, start = now - 7 * DAY;
      const markers: Marker[] = hazards.flatMap(h => { const at = Date.parse(h.date); return at >= start && at <= end ? [{ id: h.id, t: at, kind: 'hazard' as const, color: hazardColor(h), label: h.title, size: h.alert === 'red' || h.alert === 'orange' ? 9 : 6 }] : []; });
      return { range: [start, end] as [number, number], markers, band: null, title: 'Reported in the last 7 days', withTime: true };
    }
    if (mode === 'satellites') {
      const start = passStart, end = passStart + 48 * 3600_000;
      const markers: Marker[] = passes.map(p => ({ id: `${p.norad}-${p.at}`, t: p.at, kind: 'pass' as const, color: p.group === 'Radar' ? ACCENT : p.group === 'Optical land imaging' ? '#7fb0e0' : '#9a978b', label: `${p.name} · ${p.km} km`, size: p.norad === 65053 ? 10 : 6 }));
      return { range: [start, end] as [number, number], markers, band: null, title: 'Passes over your area, next 48 hours', withTime: true };
    }
    const markers: Marker[] = [];
    observations.forEach(o => markers.push({ id: `acq-${o.id}`, t: Date.parse(o.date), kind: 'acquisition', label: `NISAR acquisition · track ${o.track ?? '—'}` }));
    radar.forEach((l, i) => markers.push({ id: `pair-${i}`, t: Date.parse(String(l.date)), kind: 'pair', color: ACCENT, label: i ? 'After · measured radar date' : 'Before · measured radar date', size: 11 }));
    optical.forEach(o => markers.push({ id: `opt-${o.kind}`, t: Date.parse(o.date), kind: 'optical', color: '#7fb0e0', label: `True colour · Sentinel-2 (${o.kind})` }));
    (context?.events || []).forEach((e, i) => { const at = Date.parse(e.last_update); if (Number.isFinite(at)) markers.push({ id: `ctx-${i}`, t: at, kind: 'hazard', color: '#e5634f', label: e.title }); });
    (context?.earthquakes || []).forEach((q, i) => markers.push({ id: `eq-${i}`, t: now - q.days_ago * DAY, kind: 'hazard', color: '#ece6d6', label: `M${q.magnitude.toFixed(1)} earthquake` }));
    notes.forEach(n => markers.push({ id: n.id, t: n.t, kind: 'note', color: '#e8c34a', label: n.text }));
    const times = markers.filter(mk => mk.kind !== 'hazard' || markers.length < 3).map(mk => mk.t).filter(Number.isFinite);
    let start = times.length ? Math.min(...times) : now - 180 * DAY, end = times.length ? Math.max(...times, now) : now;
    if (end - start < 30 * DAY) { start -= 15 * DAY; end += 15 * DAY; }
    const pad = (end - start) * 0.03;
    return { range: [start - pad, Math.min(end + pad, now + 2 * DAY)] as [number, number], markers, band: pairTimes.length === 2 ? [pairTimes[0], pairTimes[1]] as [number, number] : null, title: 'Timeline', withTime: false };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, hazards, passes, observations, radar, optical, context, notes, now]);
  // Put the cursor somewhere meaningful when the context changes.
  useEffect(() => {
    if (mode === 'investigation') setT(pairTimes.length ? pairTimes.at(-1)! : now); else if (mode === 'hazards') setT(now); else setT(passStart);
    setPlaying(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, job?.id, radar.length]);
  useEffect(() => {
    if (!playing) return; const [start, end] = timeline.range; const stepMs = (end - start) / (14 / speed) / 10;
    const id = setInterval(() => setT(v => { const next = v + stepMs; if (next >= end) { setPlaying(false); return end; } return next; }), 100);
    return () => clearInterval(id);
  }, [playing, speed, timeline.range]);
  const stepNext = () => { const next = timeline.markers.map(mk => mk.t).filter(x => x > t + 1000).sort((a, b) => a - b)[0]; if (next !== undefined) setT(next); };
  const satTime = mode === 'satellites' && Math.abs(t - Date.now()) > 120_000 ? t : null;

  // ---------------------------------------------------------------- dock content
  const fmtDay = (v: number) => new Date(v).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  const fmtHour = (v: number) => new Date(v).toISOString().slice(11, 16);
  let charts: React.ReactNode = null; let side: React.ReactNode = null;
  if (mode === 'investigation' || mode === 'drones') {
    const h0 = obs[0]?.histogram, h1 = obs[1]?.histogram, hc = change?.histogram;
    const afterNow = radarNow && radar.length === 2 && radarNow === radar[1];
    const elev = profile?.points.map(p => p.elevation_m) || [];
    const dist = profile?.points.map(p => p.distance_km) || [];
    const thr = typeof change?.threshold_db === 'number' ? change.threshold_db : 3;
    const flags = profile?.points.flatMap(p => { const v = p.radar?.change; return typeof v === 'number' && Math.abs(v) >= thr ? [{ x: p.distance_km, color: v > 0 ? '#f0a35e' : '#7fb0e0' }] : []; }) || [];
    const hp = profileHover !== null ? profile?.points[profileHover] : null;
    charts = <>
      <ChartFrame title="Radar backscatter" unit="dB" empty={h0 ? undefined : running ? 'Measuring…' : 'Run an investigation to see the distribution of radar returns.'}
        action={h1 && <span className="legend-mini"><i style={{ background: '#5ec962' }} />{afterNow ? 'after' : 'before'}<i className="line" />{afterNow ? 'before' : 'after'}</span>}>
        {h0 && <Bars edges={(afterNow && h1 ? h1 : h0).edges} counts={(afterNow && h1 ? h1 : h0).counts} color={c => ramp(VIRIDIS, (c + 30) / 30)} overlay={h1 ? { ...(afterNow ? h0 : h1), color: '#d8d4c8' } : undefined}
          lines={[{ x: -18, label: 'water-like', dashed: true }]} />}
      </ChartFrame>
      <ChartFrame title="Change per pixel" unit="dB, later minus earlier" empty={hc ? undefined : running ? 'Measuring…' : 'Appears for two-date comparisons.'}>
        {hc && <Bars edges={hc.edges} counts={hc.counts} color={c => ramp(DIVERGING, (c + 6) / 12)} lines={[{ x: change?.median_db as number, label: `median ${num(change?.median_db, 2, true)}` }, { x: -thr, dashed: true }, { x: thr, dashed: true }]} />}
      </ChartFrame>
      <ChartFrame title="Terrain profile" unit="selected path" action={<button className={`mini-btn ${drawingProfile ? 'on' : ''}`} onClick={() => { setDrawingProfile(v => !v); setPicking(false); setProfileVertices([]); setProfile(null); setProfileError(''); }}>{drawingProfile ? 'Click two points…' : 'Draw path'}</button>}
        empty={profileLoading ? 'Reading elevation and radar along the path…' : profileError || (!profile ? (drawingProfile ? 'Click a start and an end point on the map.' : 'Draw a path to compare terrain and radar change.') : undefined)}>
        {profile && <><LinePlot x={dist} zero={false} series={[{ values: elev, color: '#8fa77a', fill: true }]} xLabel={v => `${v.toFixed(v < 10 ? 1 : 0)} km`} markers={flags} cursor={profileHover} onHover={setProfileHover} />
          <p className="chart-read">{hp ? `${hp.distance_km.toFixed(2)} km · ${num(hp.elevation_m, 0)} m${typeof hp.radar?.change === 'number' ? ` · change ${num(hp.radar.change, 2, true)} dB` : ''}` : `${num(Math.min(...elev.filter((v): v is number => v !== null)), 0)}–${num(Math.max(...elev.filter((v): v is number => v !== null)), 0)} m · dots mark change ≥ ${thr} dB`}</p></>}
      </ChartFrame>
    </>;
    const items: EventItem[] = timeline.markers.filter(mk => mk.kind !== 'acquisition').map(mk => ({ id: mk.id, t: mk.t, title: mk.label.split(' · ')[0], sub: mk.label.split(' · ').slice(1).join(' · ') || undefined,
      color: mk.color || '#9a978b', shape: (mk.kind === 'pair' ? 'tri' : mk.kind === 'optical' ? 'square' : mk.kind === 'note' ? 'ring' : 'dot') as EventItem['shape'], active: activeEvent === mk.id })).sort((a, b) => a.t - b.t);
    side = <EventList title="Key events" items={items} empty="Dated evidence appears here: radar dates, true-colour images, nearby hazards and your notes."
      action={{ label: 'Add', onClick: () => { const text = window.prompt(`Note at ${stamp(t, false)}`); if (text?.trim()) saveNotes([...notes, { id: `note-${Date.now()}`, t, text: text.trim().slice(0, 120) }]); } }}
      onSelect={it => { setActiveEvent(it.id); setT(it.t); }} />;
  } else if (mode === 'hazards') {
    const days = Array.from({ length: 7 }, (_, i) => { const d0 = Math.floor(now / DAY) * DAY - (6 - i) * DAY; return { d0, list: quakes.filter(q => q.time >= d0 && q.time < d0 + DAY) }; });
    const counts = Object.entries(hazards.reduce<Record<string, number>>((a, h) => { a[h.category] = (a[h.category] || 0) + 1; return a; }, {})).sort((a, b) => b[1] - a[1]);
    charts = <>
      <ChartFrame title="Earthquakes per day" unit="M2.5+, USGS"><Columns items={days.map(d => ({ label: fmtDay(d.d0), value: d.list.length, color: '#d8d4c8' }))} /></ChartFrame>
      <ChartFrame title="Largest earthquake per day" unit="magnitude"><Columns digits={1} items={days.map(d => { const mx = Math.max(0, ...d.list.map(q => q.mag)); return { label: fmtDay(d.d0), value: mx, color: mx >= 6 ? '#e5634f' : mx >= 5 ? '#f0a35e' : '#9a978b' }; })} /></ChartFrame>
      <ChartFrame title="Reports by type" unit="current"><Columns items={counts.map(([k, v]) => ({ label: (EVENT_STYLE[k]?.code || k).split(' ')[0], value: v, color: EVENT_STYLE[k]?.color || '#9a978b', title: `${EVENT_STYLE[k]?.code || k}: ${v}` }))} /></ChartFrame>
    </>;
    const items: EventItem[] = [...hazards].filter(h => h.date).sort((a, b) => b.date.localeCompare(a.date)).slice(0, 60).map(h => ({ id: h.id, t: Date.parse(h.date), title: h.title, sub: [h.alert && h.alert !== 'green' ? `${capital(h.alert)} alert` : null, h.provider].filter(Boolean).join(' · '),
      color: hazardColor(h), shape: (h.category === 'earthquakes' ? 'ring' : h.category === 'volcanoes' ? 'tri' : 'dot') as EventItem['shape'], active: selectedHazard?.id === h.id }));
    side = <EventList title="Hazard log" count={hazards.length} items={items} onSelect={it => { const h = hazards.find(x => x.id === it.id); if (h) { setSelectedHazard(h); globe.current?.flyToPoint(h.lon, h.lat, 1_200_000); } }} />;
  } else {
    const series = sat ? trackSeries(sat, aoiCentre.lat, aoiCentre.lon, passStart, 24 * 60, 120) : null;
    const alt3 = sat ? trackSeries(sat, aoiCentre.lat, aoiCentre.lon, passStart, 180, 60) : null;
    const byGroup = Object.entries(passes.reduce<Record<string, number>>((a, p) => { a[p.name] = (a[p.name] || 0) + 1; return a; }, {})).sort((a, b) => b[1] - a[1]).slice(0, 8);
    charts = <>
      <ChartFrame title={`Altitude${sat ? ` · ${sat.name}` : ''}`} unit="km, next 3 h" empty={alt3 ? undefined : 'Choose a satellite.'}>{alt3 && <LinePlot x={alt3.x} series={[{ values: alt3.alt, color: '#d8d4c8' }]} xLabel={fmtHour} cursor={null} />}</ChartFrame>
      <ChartFrame title="Distance to your area" unit="km, next 24 h" empty={series ? undefined : 'Choose a satellite.'}>{series && <LinePlot x={series.x} series={[{ values: series.dist, color: ACCENT, fill: true }]} xLabel={fmtHour} markers={passes.filter(p => p.norad === sat?.norad).map(p => ({ x: p.at, color: '#8fbf6a' }))} />}</ChartFrame>
      <ChartFrame title="Passes over your area" unit="48 h, by satellite" empty={byGroup.length ? undefined : 'No passes predicted yet.'}><Columns items={byGroup.map(([name, v]) => ({ label: name.replace(/Sentinel-/, 'S-').replace(/Landsat /, 'L'), value: v, color: name === 'NISAR' ? ACCENT : '#9a978b', title: `${name}: ${v}` }))} /></ChartFrame>
    </>;
    const items: EventItem[] = passes.filter(p => p.at >= Date.now() - 600_000).slice(0, 60).map(p => ({ id: `${p.norad}-${p.at}`, t: p.at, title: p.name, sub: `${fmtHour(p.at)} UTC · within ${p.km} km`, color: p.group === 'Radar' ? ACCENT : p.group === 'Optical land imaging' ? '#7fb0e0' : '#9a978b', shape: 'tri', active: activeEvent === `${p.norad}-${p.at}` }));
    side = <EventList title="Pass log" count={passes.length} items={items} empty="Loading orbital elements…" onSelect={it => { setActiveEvent(it.id); setT(it.t); setSelectedSat(Number(it.id.split('-')[0])); }} />;
  }

  // ---------------------------------------------------------------- layers tab rows
  const z = 10, tileAt = tile(aoiCentre.lat, aoiCentre.lon, z);
  const set = (id: LayerId, patch: Partial<Setting>) => setLs(s => ({ ...s, [id]: { ...s[id], ...patch } }));
  const layerRows: LayerRowSpec[] = [
    { id: 'terrain', name: 'Terrain (3D)', sub: 'Exaggeration', thumb: `/api/connector/maps/relief-dark/${tile(aoiCentre.lat, aoiCentre.lon, 9)}`, on: ls.terrain.on, value: ls.terrain.value, max: 3, disabled: flat },
    { id: 'imagery', name: 'Satellite imagery', note: mapStyle === 'relief' ? 'Replaced by the relief map' : mapStyle === 'photoreal' ? 'Esri World Imagery above 300 km; Google 3D tiles below, hidden while a radar layer shows' : 'Esri World Imagery', thumb: `/api/connector/maps/imagery/${tileAt}`, on: ls.imagery.on, value: ls.imagery.value, disabled: flat || mapStyle === 'relief' },
    { id: 'contours', name: 'Contours', note: 'Relief map · spacing follows the zoom', swatch: Swatch.contours(), on: ls.contours.on, disabled: flat || style !== 'relief' },
    { id: 'landcover', name: 'Land cover', note: terrain?.ctx.landcover ? `ESA WorldCover 2021 · ${terrain.ctx.landcover.resolution_m} m cells` : terrainState === 'loading' ? 'Reading ESA WorldCover…' : 'ESA WorldCover 2021', swatch: Swatch.cover(), on: ls.landcover.on, disabled: flat || style !== 'relief' || !terrain?.grid },
    { id: 'trees', name: 'Trees and shrubs', note: 'Placed where land cover has them', swatch: Swatch.trees(), on: ls.trees.on, disabled: flat || style !== 'relief' || !terrain?.grid },
    { id: 'peaks', name: 'Summits and towns', note: terrain ? `${terrain.ctx.features.length} named · Wikidata` : 'Wikidata', swatch: Swatch.peak(), on: ls.peaks.on, disabled: flat || style !== 'relief' },
    { id: 'roads', name: 'Roads', note: 'Esri World Transportation', swatch: Swatch.road(), on: ls.roads.on, disabled: flat || style !== 'relief' },
    { id: 'radar', name: 'Radar backscatter', note: radarNow ? `NISAR · ${prettyDate(String(radarNow.date))} (follows timeline)` : 'Appears after an investigation', thumb: radarNow && job ? url(radarNow) : undefined, swatch: Swatch.text('dB'), on: ls.radar.on, value: ls.radar.value, disabled: !radarNow },
    { id: 'change', name: 'Backscatter change', note: delta ? 'Later minus earlier, ±6 dB' : 'Two-date comparisons only', thumb: delta && job ? url(delta) : undefined, swatch: Swatch.text('Δ'), on: ls.change.on, value: ls.change.value, disabled: !delta },
    { id: 'hotspots', name: 'Change hotspots', note: hotspots.length ? `${hotspots.length} largest patches` : 'Two-date comparisons only', swatch: Swatch.patches(), on: ls.hotspots.on, disabled: !hotspots.length },
    { id: 'optical', name: 'True colour (Sentinel-2)', note: opticalNow ? `${prettyDate(opticalNow.date)} (follows timeline)` : enriching ? 'Searching the archive…' : 'Appears after an investigation', thumb: opticalNow && job ? artifactUrl(job, opticalNow.artifact_name) : undefined, swatch: Swatch.text('RGB'), on: ls.optical.on, value: ls.optical.value, disabled: !opticalNow },
    { id: 'nisar', name: 'NISAR orbit and track', swatch: Swatch.outline(ACCENT), on: ls.nisar.on },
    { id: 'coverage', name: 'NISAR coverage, 4 days', swatch: Swatch.outline('#f0a35e'), on: ls.coverage.on },
    { id: 'events', name: 'Hazards', sub: 'USGS · GDACS · NASA EONET', swatch: Swatch.diamond('#e5634f'), on: ls.events.on },
    { id: 'quakes', name: 'Earthquakes, M2.5+', swatch: Swatch.ring('#ece6d6'), on: ls.quakes.on },
    { id: 'fires', name: 'Active fires, 24 h', swatch: Swatch.heat('#f6c24a'), on: ls.fires.on },
    { id: 'fleet', name: 'Earth-observation satellites', swatch: Swatch.ring('#d8d4c8'), on: ls.fleet.on },
    { id: 'labels', name: 'Places and borders', swatch: Swatch.text('Aa'), on: ls.labels.on, value: ls.labels.value },
    { id: 'drones', name: 'Drones', note: `${fleetCount} airborne · virtual${liveDrones ? ` and ${liveDrones} real` : ''}`, swatch: Swatch.drone(), on: ls.drones.on, disabled: flat },
  ];

  // ---------------------------------------------------------------- sizes and blockers
  const serviceDown = !health ? 'Connecting to the investigation service…' : !health.temporal.connected ? 'The investigation service is offline.' : '';
  const area = boxAreaKm2(region.bbox);
  const sizeKm = { w: (region.bbox[2] - region.bbox[0]) * 111.32 * Math.cos(aoiCentre.lat * Math.PI / 180), h: (region.bbox[3] - region.bbox[1]) * 110.57 };
  const tooLarge = sizeKm.w > 70 || sizeKm.h > 70 ? `This area is ${Math.round(sizeKm.w)} × ${Math.round(sizeKm.h)} km; investigations cover up to about 70 km across. Draw a smaller area or right-click a point.` : '';
  const averaged = !tooLarge && (sizeKm.w > 20 || sizeKm.h > 20) ? 'Over about 20 km across, radar pixels may be averaged to fit; the report says so if they are.' : '';
  const need = analysis === 'change' ? 2 : 1;
  const manualBlocker = serviceDown || tooLarge || (selectedIds.length !== need ? `Select ${need === 1 ? 'one date' : 'two dates'}.` : '');
  const plan = (job as (Investigation & { plan?: Plan }) | undefined)?.plan;
  const dates = radar.length ? radar.map(l => prettyDate(String(l.date))).join(' – ') : observations.length ? `${prettyDate([...observations].sort((a, b) => a.date.localeCompare(b.date))[0].date)} – ${prettyDate([...observations].sort((a, b) => b.date.localeCompare(a.date))[0].date)}` : 'Searching the radar archive…';
  const liveCount = Object.values(feeds).filter(f => f.state === 'live').length, feedCount = Math.max(Object.keys(feeds).length, 5);
  const alertCount = hazards.filter(h => h.alert === 'red' || h.alert === 'orange').length;
  const conditions = result?.surface_conditions;
  const meta0 = ((result?.provenance as { inputs?: { metadata?: Record<string, unknown> }[] } | undefined)?.inputs || [])[0]?.metadata || {};

  // Menu contents, shared by the desktop menus and the phone's single More sheet.
  const viewItems = <>
          <span className="menu-label">Map</span>
          <button className={!flat && mapStyle === 'photo' ? 'on' : ''} onClick={() => { setFlat(false); setStyle('photo'); }}>3D terrain and imagery</button>
          <button className={!flat && mapStyle === 'relief' ? 'on' : ''} onClick={() => { setFlat(false); setStyle('relief'); }}>3D relief map</button>
          <button className={!flat && mapStyle === 'photoreal' ? 'on' : ''} disabled={!photorealReady} title={photorealReady ? 'Google Photorealistic 3D Tiles below 300 km' : photorealState?.reason || 'Checking the map service…'}
            onClick={() => { setFlat(false); setStyle('photoreal'); }}>Photorealistic 3D (Google){!photorealReady && <small className="menu-note">{photorealState ? 'Unavailable' : 'Checking…'}</small>}</button>
          <button className={flat ? 'on' : ''} onClick={() => setFlat(true)}>2D relief map</button>
          <button disabled={!nisarSat} onClick={() => nisarSat && openSensor(nisarSat)}>Sensor view from NISAR’s next pass</button>
          <button className={lighting ? 'on' : ''} onClick={() => setLighting(v => !v)}>Day and night, live</button>
          <button className={footprints ? 'on' : ''} onClick={() => setFootprints(v => !v)}>Radar acquisition outlines</button>
          <span className="menu-label">Display filter</span>
          <div className="menu-seg">{DISPLAY_MODES.map(dm => <button key={dm.id} className={display === dm.id ? 'on' : ''} onClick={() => setDisplay(dm.id)} title={dm.hint}>{dm.id === 'normal' ? 'True' : dm.label}</button>)}</div>
          <span className="menu-label">Detail</span>
          <div className="menu-seg">{['low', 'balanced', 'high'].map(q => <button key={q} className={quality === q ? 'on' : ''} onClick={() => setQuality(q)}>{capital(q)}</button>)}</div>
  </>;
  const analysisItems = <>
          <button onClick={newQuestion}>New question for this area</button>
          <button onClick={() => { setMode('investigation'); setTab('analysis'); setJob(undefined); setManual(true); setMenu(null); }}>Choose radar dates yourself</button>
          <button onClick={() => { setPicking(true); setMenu(null); }}>Draw a new area</button>
          <button onClick={() => { setDrawingProfile(true); setProfileVertices([]); setProfile(null); setMenu(null); }}>Draw a terrain profile</button>
          {jobs.length > 0 && <span className="menu-label">Saved investigations</span>}
          {jobs.slice(0, 8).map(j => <button key={j.id} onClick={() => void openJob(j)} className="menu-job"><b>{j.question || j.title}</b><small>{j.title.split(' · ')[0]} · {prettyDate(j.created_at)}</small></button>)}
  </>;
  const shareItems = <>
          <button disabled={!job} onClick={() => { void navigator.clipboard?.writeText(location.href); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>{copied ? 'Link copied' : 'Copy link to this investigation'}</button>
          <button disabled={!result} onClick={() => { setDossier(true); setMenu(null); }}>Open the report</button>
          {job && result ? <a href={artifactUrl(job, 'public-result.json')} download>Download measurements and sources</a> : <button disabled>Download measurements and sources</button>}
          <button onClick={() => { globe.current?.screenshot(); setMenu(null); }}>Save an image of the map</button>
  </>;
  const sourceItems = <>{Object.entries({ satellites: 'CelesTrak orbital elements', nisar: 'NASA / ASF catalog', earthquakes: 'USGS earthquakes', events: 'USGS volcanoes · GDACS · NASA EONET', fires: 'NASA FIRMS' }).map(([k, name]) => { const f = feeds[k]; return <li key={k}>
          <i className={`led ${f?.state === 'live' ? 'ok' : f?.state === 'stale' ? 'warn' : f?.state === 'offline' ? 'bad' : ''}`} />
          <span>{name}<small>{f ? `${f.count.toLocaleString('en')} records${f.fetchedAt ? ` · updated ${new Date(f.fetchedAt).toISOString().slice(11, 16)} UTC` : ''}` : 'connecting'}</small></span></li>; })}
          <li><i className={`led ${health?.temporal.connected ? 'ok' : 'bad'}`} /><span>Investigation service<small>{health?.temporal.connected ? 'Durable workflows connected' : 'Offline'}</small></span></li>
          <li><i className={`led ${originals ? 'ok' : ''}`} /><span>NASA Earthdata<small>{originals ? 'Original NISAR products available' : 'No credential set'}</small></span></li>
          <li><i className={`led ${writer ? 'ok' : ''}`} /><span>Language model<small>{writer ? `${writer} plans and writes; every number is checked` : 'Not configured'}</small></span></li>
          <li><i className={`led ${photorealReady ? 'ok' : ''}`} /><span>Google Map Tiles<small>{photorealReady ? 'Photorealistic 3D available (View menu)' : photorealState?.reason || 'Checking'}</small></span></li></>;
  return <div className={`app workspace mode-${mode} ${dockOpen ? 'dock-open' : ''}`} onClick={() => menu && setMenu(null)}>
    {/* ------------------------------------------------ top bar */}
    <header className="topbar" onClick={e => e.stopPropagation()}>
      <button className="brand" onClick={() => globe.current?.home()} aria-label="Terra: show the whole Earth">
        <img src="/favicon.svg" alt="" width="24" height="24" />
        <b>Terra</b></button>
      <span className="product">Surface Change Analysis</span>
      <div className="area-picker">
        <button className={`area-button ${menu === 'area' ? 'on' : ''}`} onClick={() => setMenu(v => v === 'area' ? null : 'area')} aria-haspopup="dialog"><b>{region.name}</b><ChevronDown size={14} /></button>
        <span className="area-context">{region.context !== region.name ? region.context : coord(aoiCentre.lat, aoiCentre.lon)}</span>
        {menu === 'area' && <div className="menu area-menu" role="dialog" aria-label="Find a place"><Search inline inputRef={searchRef} hazards={hazards} jobs={jobs} hazardColor={hazardColor} onRegion={selectRegion} onJob={j => void openJob(j)} /></div>}
      </div>
      <span className="daterange"><Calendar size={14} />{dates}</span>
      <nav className="modes" aria-label="Workspace">
        {([['investigation', 'Investigation', job ? (running ? '…' : '') : ''], ['drones', 'Drones', String(fleetCount || '')], ['hazards', 'Hazards', alertCount ? String(alertCount) : ''], ['satellites', 'Satellites', passes.length ? String(passes.length) : '']] as const).map(([id, label, badge]) =>
          <button key={id} className={mode === id ? 'on' : ''} onClick={() => { setMode(id); setSelection(null);
            // Each mode opens on the view that suits it: the whole planet for hazards, a wide orbit view for satellites.
            if (id === 'hazards' && mode !== 'hazards') globe.current?.home();
            else if (id === 'satellites' && mode !== 'satellites') globe.current?.flyToPoint(aoiCentre.lon, aoiCentre.lat, 9_000_000);
            else if ((id === 'investigation' || id === 'drones') && (mode === 'hazards' || mode === 'satellites')) globe.current?.flyTo(region.bbox);
            if (id === 'drones' && !selectedDrone) setSelectedDrone(droneStore.ids()[0] ?? null); }} aria-current={mode === id ? 'page' : undefined}>
            {id === 'investigation' ? <BarChart3 size={14} /> : id === 'drones' ? <Plane size={14} /> : id === 'hazards' ? <Info size={14} /> : <Navigation size={14} />}{label}{badge && <span className="badge">{badge}</span>}</button>)}
      </nav>
      <div className="menus">
        <MenuButton id="view" label="View" icon={<Eye size={14} />} menu={menu} setMenu={setMenu}>
          {viewItems}
        </MenuButton>
        <MenuButton id="analysis" label="Analysis" icon={<BarChart3 size={14} />} menu={menu} setMenu={setMenu}>
          {analysisItems}
        </MenuButton>
        <MenuButton id="share" label="Share" icon={<Share2 size={14} />} menu={menu} setMenu={setMenu}>
          {shareItems}
        </MenuButton>
      </div>
      <MenuButton id="more" label="More" icon={<MenuIcon size={15} />} menu={menu} setMenu={setMenu} className="more-wrap">
        <span className="menu-label">Analysis</span>
        {analysisItems}
        <span className="menu-label">Share</span>
        {shareItems}
        {viewItems}
        <span className="menu-label">Sources</span>
        <ul className="source-list">{sourceItems}</ul>
        <p className="card-note">Every external request goes through Terra’s server. Keys never reach the browser.</p>
        <p className="card-note">Imagery © Esri and partners; photorealistic 3D © Google Maps and its data providers; contains modified Copernicus Sentinel-2 data. Data from NASA, ASF, ISRO, USGS, GDACS, CelesTrak, Open-Meteo and OpenStreetMap contributors.</p>
      </MenuButton>
      <button className={`sources-btn ${menu === 'sources' ? 'on' : ''}`} onClick={() => setMenu(v => v === 'sources' ? null : 'sources')}><i className={`led ${liveCount >= feedCount - 1 && health?.temporal.connected ? 'ok' : 'warn'}`} />{liveCount}/{feedCount} live</button>
      <button className={`icon-btn about-btn ${menu === 'about' ? 'on' : ''}`} onClick={() => setMenu(v => v === 'about' ? null : 'about')} aria-label="About and settings"><Settings size={15} /></button>
      {menu === 'sources' && <div className="menu sources-menu" role="dialog" aria-label="Data sources">
        <ul className="source-list">{sourceItems}</ul>
        <p className="card-note">Every external request goes through Terra’s server. Keys never reach the browser.</p></div>}
      {menu === 'about' && <div className="menu about-menu" role="dialog" aria-label="About Terra"><div className="prose">
        <p>Terra answers questions about how the ground changes, using radar from NISAR, the joint NASA–ISRO mission. Radar sees through cloud and darkness, so every pass counts.</p>
        <p className="small"><kbd>/</kbd> find a place · <kbd>Space</kbd> play the timeline · <kbd>L</kbd> layers · <kbd>1</kbd>–<kbd>5</kbd> display filters · right-click marks an area · <kbd>Esc</kbd> closes</p>
        <p className="small quiet">Imagery © Esri and partners; photorealistic 3D © Google Maps and its data providers (shown on the map when in view); contains modified Copernicus Sentinel-2 data. Data from NASA, ASF, ISRO, USGS, GDACS, CelesTrak, Open-Meteo and OpenStreetMap contributors.</p></div></div>}
    </header>

    {/* ------------------------------------------------ map */}
    <main className="map-area">
      <Globe ref={globe} region={region} observations={observations} selected={activeObservation} footprints={footprints && !job} terrain={ls.terrain.on} lighting={lighting}
        labels={ls.labels.on} photoreal={mapStyle === 'photoreal'} onPhotorealError={onPhotorealError} quality={quality} picking={picking} overlays={overlays} imageryAlpha={ls.imagery.on ? ls.imagery.value : 0} labelAlpha={ls.labels.value}
        exaggeration={ls.terrain.on ? Math.max(0.01, ls.terrain.value) : 1} mode={display} layers={liveLayers} marks={marks} flat={flat} profiling={drawingProfile} profileVertices={profileVertices}
        deploy={deploy} onDeployArea={onDeployArea} onDeployPad={onDeployPad}
        satTime={satTime} satFocus={mode === 'satellites' ? selectedSat : null} onPathPoint={onPathPoint}
        relief={mapStyle === 'relief'} reliefOptions={reliefOptions} terrainContext={terrainContext} onHover={onHover}
        drones={ls.drones.on || mode === 'drones'} selectedDrone={selectedDrone} onSelectDrone={onSelectDrone}
        onPick={pick} onQuickPick={quickPick} onSelect={onSelect} onProbe={onProbe} onTelemetry={onTelemetry} onFeeds={onFeeds} onReady={() => {}} />
      <MapChrome flat={flat} setFlat={setFlat} style={mapStyle} setStyle={setStyle} photoreal={photorealReady} globe={globe} bbox={region.bbox} />
      <HoverCard jobId={result ? job?.id : undefined} hasPatches={hotspots.length > 0} changeBounds={delta?.bounds} />
      {!sensor && !feed && nisarSat && !flat && mode !== 'drones' && <button className="map-tool" onClick={() => openSensor(nisarSat)} title="A simulated view of this area from NISAR at its next pass"><Satellite size={14} />NISAR view</button>}
      {sensor && (() => { const s = fleet.sats.find(x => x.norad === sensor.norad); if (!s) return null;
        const afterRadar = radar.at(-1); const afterOptical = optical.at(-1);
        return <SensorView key={`${sensor.norad}-${sensor.at}`} sat={s} at={sensor.at} target={aoiCentre} bbox={region.bbox} expanded={sensorBig} onExpand={() => setSensorBig(v => !v)} onClose={() => { setSensor(null); setSensorBig(false); }}
          eo={job && afterOptical ? { url: artifactUrl(job, afterOptical.artifact_name), bounds: afterOptical.bounds as Bounds, date: afterOptical.date } : undefined}
          sar={job && afterRadar ? { url: url(afterRadar), bounds: afterRadar.bounds, date: String(afterRadar.date) } : undefined}
          change={job && delta ? { url: url(delta), bounds: delta.bounds } : undefined} />; })()}
      {cameraId && <DroneFeed id={cameraId} visible={Boolean(feed)} store={droneStore} terrain={terrainContext} relief={mapStyle === 'relief'} reliefOptions={reliefOptions}
        change={job && delta ? { url: url(delta), bounds: delta.bounds } : undefined} thermal={thermal} expanded={Boolean(feed?.expanded)}
        onExpand={() => setFeed(f => f && { ...f, expanded: !f.expanded })} onClose={() => setFeed(null)} onSight={onDroneSight} onSample={job && result ? sampleChange : undefined} />}
      {!dockOpen && <button className="dock-toggle" aria-expanded={false} aria-controls="timeline-drawer" onClick={() => setDockOpen(true)} title="Timeline and charts (T)"><ChevronUp size={14} />Timeline</button>}
      {following && !deploy && <div className="follow-chip" role="status"><i aria-hidden="true" />Flying with <b>{droneStore.state(following)?.callsign ?? following}</b>
        <span>drag to orbit · scroll to zoom</span><button onClick={stopFollowing}>Stop</button></div>}
      {deploy?.stage === 'draw' && <div className="map-hint">Drag on the map to draw the fleet's perimeter (or click two corners). Anywhere on Earth.<button onClick={() => setDeploy(null)}>Cancel</button></div>}
      {deploy?.stage === 'confirm' && deploy.area && <DeployCard area={deploy.area} pad={deploy.pad} profile={droneStore.info.area?.profile ?? 'change'}
        onLaunch={launchDeployment} onRedraw={() => setDeploy({ stage: 'draw', area: null, pad: null })} onCancel={() => setDeploy(null)} />}
      {(picking || drawingProfile) && <div className="map-hint">{picking ? 'Click two opposite corners of the new area. Right-click anywhere marks a 30 km area.' : 'Click the start and end of the profile path.'}<button onClick={() => { setPicking(false); setDrawingProfile(false); }}>Cancel</button></div>}
      {(ls.change.on && delta || ls.radar.on && radarNow) && mode === 'investigation' && <div className="map-legend">
        <b>{ls.change.on && delta ? 'Backscatter change' : `Radar · ${prettyDate(String(radarNow?.date))}`}</b>
        <i className={`ramp ${ls.change.on && delta ? 'div' : ''}`} />
        <span className="ticks num"><span>{ls.change.on && delta ? '−6' : '−30'}</span><span>dB</span><span>{ls.change.on && delta ? '+6' : '0'}</span></span>
        <span className="probe">{probe ? (() => { const v = probe.values.find(x => (ls.change.on && delta ? x.layer === 'change' : x.layer === radarNow?.id)) || probe.values[0];
          return <><b className="num">{v && v.value !== null ? `${num(v.value, 2, v.layer === 'change')} dB` : 'No valid data'}</b> at {coord(probe.lat, probe.lon)}</>; })() : 'Click the map to read a value'}</span>
      </div>}
      {selection && <div className="map-card" style={{ ['--sel' as string]: selection.color }} role="dialog" aria-label={selection.title} onClick={e => e.stopPropagation()}>
        <header><span className="kicker"><i className="dot" style={{ background: selection.color }} />{selection.kind === 'frame' ? 'NISAR acquisition' : selection.kind === 'quake' ? 'Earthquake' : selection.kind === 'event' ? 'Hazard' : selection.kind === 'fire' ? 'Fire detection' : 'Satellite'}</span>
          <button className="icon-btn" onClick={() => setSelection(null)} aria-label="Close"><X size={14} /></button></header>
        <h3>{selection.title}</h3>{selection.subtitle && <p className="meta break">{selection.subtitle}</p>}
        <KeyValues rows={[...selection.fields, ['Location', coord(selection.lat, selection.lon)]]} />
        <div className="detail-actions">
          <button className="primary" onClick={() => { selectRegion({ name: selection.title.slice(0, 60), context: coord(selection.lat, selection.lon), bbox: boxAround(selection.lon, selection.lat, selection.kind === 'frame' ? 0.25 : 0.15), tag: 'From map' }); setSelection(null); }}>Investigate this area</button>
          {safeUrl(selection.url) && <a className="secondary" href={safeUrl(selection.url)} target="_blank" rel="noreferrer">Source <ArrowUpRight size={12} /></a>}
        </div>
      </div>}
    </main>

    {/* ------------------------------------------------ right panel */}
    <aside className="sidebar" aria-label={mode === 'investigation' ? 'Investigation' : mode === 'hazards' ? 'Hazards' : 'Satellites'}>
      {error && <div className="alert" role="alert"><span>{error}</span><button className="icon-btn" onClick={() => setError('')} aria-label="Dismiss"><X size={13} /></button></div>}
      {mode === 'investigation' && <>
        <nav className="side-tabs" role="tablist">{(['layers', 'analysis', 'evidence', 'notes'] as const).map(id => <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{capital(id)}</button>)}</nav>
        <div className="side-scroll">
          {tab === 'layers' && <>
            <LayersList rows={layerRows} onToggle={(id, on) => set(id as LayerId, { on })} onValue={(id, value) => set(id as LayerId, { value })} />
            <Card title="Investigation overview" action={result ? <button className="mini-btn" onClick={() => setDossier(true)}>Report <ArrowUpRight size={11} /></button> : undefined}>
              <KeyValues rows={[['Area', region.name], ['Location', coord(aoiCentre.lat, aoiCentre.lon)], ['Size', `${Math.round(sizeKm.w)} × ${Math.round(sizeKm.h)} km · ${area >= 100 ? Math.round(area).toLocaleString('en') : area.toFixed(1)} km²`],
                ...(job ? [['Investigation', <span className="num" key="id">{job.id.slice(0, 8).toUpperCase()}</span>] as [string, React.ReactNode]] : []),
                ['Before', radar[0] ? prettyDate(String(radar[0].date)) : '—'], ['After', radar[1] ? prettyDate(String(radar[1].date)) : '—'],
                ...(pairTimes.length === 2 ? [['Interval', `${Math.round((pairTimes[1] - pairTimes[0]) / DAY)} days`] as [string, string]] : []),
                ['Product', meta0.product ? `NISAR ${String(meta0.product)} · ${String(meta0.maturity || '').toLowerCase()}` : `NISAR GCOV · ${observations.length} in archive`],
                ...(meta0.track !== undefined ? [['Track, frame', `${String(meta0.track)} ${String(meta0.direction || '').toLowerCase()}, ${String(meta0.frame ?? '—')}`] as [string, string]] : []),
                ['Status', <span key="s" className={`state ${job?.status || 'idle'}`}>{!job ? 'Not yet investigated' : job.status === 'completed' ? 'Complete' : running ? 'Running' : capital(job.status)}</span>]]} />
            </Card>
            <Card title="Weather and surface" sub="at the radar dates">
              <ConditionsGrid value={conditions} waterBefore={change ? change.low_return_before_km2 as number : obs[0]?.low_return_km2 as number | undefined} waterAfter={change ? change.low_return_after_km2 as number : undefined} pending={running || enriching} />
            </Card>
            <Card title="Land cover" sub="share of the area">
              {terrain?.ctx.landcover ? <><ul className="cover-bars">{terrain.ctx.landcover.shares.slice(0, 6).map(c => <li key={c.code}>
                <i style={{ background: terrain.ctx.landcover!.classes[String(c.code)]?.color }} /><span>{c.name}</span><em className="num">{c.pct.toFixed(1)}%</em>
                <b style={{ width: `${Math.max(2, c.pct)}%`, background: terrain.ctx.landcover!.classes[String(c.code)]?.color }} /></li>)}</ul>
                <p className="card-note">ESA WorldCover 2021, a 10 m classification. Forest scatters L-band radar strongly; bare rock and water return little.</p></>
                : <p className="card-note">{terrainState === 'loading' ? 'Reading land cover…' : terrainState === 'error' ? 'Land cover is unavailable for this area right now.' : 'Land cover is shown for areas up to 2° across.'}</p>}
            </Card>
          </>}
          {tab === 'analysis' && <>
            <section className="place">
              <h1>{region.name}</h1>
              <p className="meta">{region.context !== region.name && <>{region.context} · </>}{Math.round(sizeKm.w)} × {Math.round(sizeKm.h)} km</p>
              <div className="links"><button onClick={() => globe.current?.flyTo(region.bbox)}>Zoom to area</button><button className={picking ? 'on' : ''} onClick={() => setPicking(!picking)}>{picking ? 'Cancel drawing' : 'Draw a new area'}</button><button onClick={() => setMenu('area')}>Find a place</button></div>
            </section>
            {!job && <>
              <Composer question={question} setQuestion={setQuestion} questionRef={questionRef} onLaunch={auto => void launch(auto)} submitting={submitting}
                blocker={serviceDown || (tooLarge ? 'Area too large' : '')} warning={tooLarge || averaged} manual={manual}
                setManual={v => { setManual(v); if (v && !selectedIds.length && activeObservation) setSelectedIds([activeObservation.id]); }}
                observations={observations} loading={loading} catalogError={catalogError} onRetry={() => setRefresh(r => r + 1)} selectedIds={selectedIds} onToggle={toggleObservation}
                analysis={analysis} setAnalysis={a => { setAnalysis(a); if (a === 'backscatter') setSelectedIds(ids => ids.slice(0, 1)); }} manualBlocker={manualBlocker} onDetail={o => { setActiveObservation(o); setDetail(true); }} />
              <ContextBlock context={context} />
            </>}
            {job && <section className="job">
              <div className="job-head">
                <span className={`state ${job.status}`}>{job.status === 'completed' ? `Answered${result?.timings?.interpreted_answer_seconds ? ` in ${result.timings.interpreted_answer_seconds.toFixed(1)} s` : ''}`
                  : job.status === 'blocked' ? 'Waiting for data' : running ? `Working · ${seconds(now - Date.parse(job.created_at))}` : capital(job.status)}</span>
                <button className="link" onClick={newQuestion}>New question</button>
              </div>
              <p className="asked">{job.question}</p>
              {running && <Steps job={job} now={now} />}
              {plan && running && <p className="hint">{plan.selection}</p>}
              {job.blocker && <div className="alert warn"><span>{job.blocker}</span></div>}
              {job.status === 'blocked' && <button className="secondary" onClick={() => void action('resume')}>Check again and continue</button>}
              {running && <button className="link danger" onClick={() => void action('cancel')}>Cancel</button>}
              {result && <Results job={job} result={result} writer={writer} plan={plan} focusSpot={focusSpot}
                onRewritten={(nv: Narrative) => setJob(j => j && j.result ? { ...j, result: { ...j.result, narrative: nv, narrative_status: 'ready', narrative_note: null } as Investigation['result'] } : j)}
                onSpot={(h: Hotspot) => { setFocusSpot(h.rank); set('hotspots', { on: true }); const [w, s, e, n] = h.bounds; const pad = Math.max(e - w, n - s) * 1.5 + 0.01; globe.current?.flyTo([w - pad, s - pad, e + pad, n + pad]); setProfileVertices([[region.bbox[0], h.centre[0]], [region.bbox[2], h.centre[0]]]); }}
                onReport={() => setDossier(true)} />}
              {result && job && hotspots.length > 0 && <Verification jobId={job.id} spots={hotspots as unknown as SignedSpot[]} fleetArea={fleetAreaInvestigation}
                onSpot={h => { setFocusSpot(h.rank); set('hotspots', { on: true }); const [w, s2, e, n] = h.bounds; const pad = Math.max(e - w, n - s2) * 1.5 + 0.01; globe.current?.flyTo([w - pad, s2 - pad, e + pad, n + pad]); }}
                onWatch={() => { setMode('drones'); const first = droneStore.states().find(d => d.task?.params && String((d.task as { job_key?: string }).job_key ?? '').startsWith('orbit:patch')) ?? droneStore.states()[0]; if (first) { setSelectedDrone(first.id); followDrone(first.id); } }} />}
            </section>}
          </>}
          {tab === 'evidence' && <section className="evidence">
            {!result ? <p className="hint">Before-and-after images, sources and the full report appear here after an investigation.</p> : <>
              {optical.length >= 2 || radar.length === 2 ? <Compare bbox={job!.bbox} mode="swipe" before={optical.length >= 2 && optical[0].kind !== 'recent' ? { src: artifactUrl(job!, optical[0].artifact_name), bounds: optical[0].bounds as Bounds, label: 'Before', date: prettyDate(optical[0].date), alt: 'Earlier true-colour image' } : { src: url(radar[0]), bounds: radar[0].bounds, label: 'Before', date: prettyDate(String(radar[0].date)), alt: 'Earlier radar image', pixelated: true }}
                after={optical.length >= 2 && optical[0].kind !== 'recent' ? { src: artifactUrl(job!, optical[1].artifact_name), bounds: optical[1].bounds as Bounds, label: 'After', date: prettyDate(optical[1].date), alt: 'Later true-colour image' } : { src: url(radar[1]), bounds: radar[1].bounds, label: 'After', date: prettyDate(String(radar[1].date)), alt: 'Later radar image', pixelated: true }} /> : <p className="hint">This investigation has one date, so there is nothing to compare.</p>}
              <button className="primary wide" onClick={() => setDossier(true)}>Open the full report</button>
              <Card title="Sources"><KeyValues rows={[...radar.map((l, i) => [i ? 'After (radar)' : 'Before (radar)', `NISAR GCOV · ${prettyDate(String(l.date))}`] as [string, string]),
                ...optical.map(o => [`True colour, ${o.kind}`, <a key={o.kind} href={safeUrl(o.source_url)} target="_blank" rel="noreferrer">Sentinel-2 · {prettyDate(o.date)}</a>] as [string, React.ReactNode])]} /></Card>
              <Card title="Limitations"><ul className="bullets">{result.validation?.limitations?.map((l, i) => <li key={i}>{l}</li>)}</ul></Card>
            </>}
          </section>}
          {tab === 'notes' && <section className="notes">
            <p className="hint">Notes are kept in this browser and appear as events on the timeline at the time you choose.</p>
            <textarea className="field area" value={noteText} onChange={e => setNoteText(e.target.value)} rows={3} maxLength={300} placeholder={`Note at ${stamp(t, false)}`} />
            <button className="secondary" disabled={!noteText.trim()} onClick={() => { saveNotes([...notes, { id: `note-${Date.now()}`, t, text: noteText.trim() }]); setNoteText(''); }}>Add note at {stamp(t, false)}</button>
            <ul className="note-list">{[...notes].sort((a, b) => a.t - b.t).map(n => <li key={n.id}><button onClick={() => setT(n.t)}><time>{stamp(n.t, false)}</time>{n.text}</button><button className="icon-btn" onClick={() => saveNotes(notes.filter(x => x.id !== n.id))} aria-label="Delete note"><X size={12} /></button></li>)}</ul>
            {job && <Card title={`Processing record (${job.events.length})`}><ol className="log">{job.events.map((e, i) => <li key={i}><span className="num">{new Date(e.at).toISOString().slice(11, 19)}</span>{e.message}</li>)}</ol></Card>}
          </section>}
        </div>
      </>}
      {mode === 'hazards' && <HazardsPanel hazards={hazards} state={hazardState} selected={selectedHazard}
        onSelect={h => { setSelectedHazard(h); globe.current?.flyToPoint(h.lon, h.lat, 900_000); }}
        onInvestigate={h => selectRegion({ name: h.title.slice(0, 60), context: [h.category_title, h.provider].filter(Boolean).join(' · '), bbox: boxAround(h.lon, h.lat, 0.15), tag: h.category_title })} />}
      {mode === 'satellites' && <SatellitesPanel sats={fleet.sats} passes={passes} now={satTime ?? Date.now()} selected={sat} onSelect={s => setSelectedSat(s.norad)} source={fleet.source} nisarNext={nisarNext} error={fleet.error}
        onFly={flyToSat} onSensor={openSensor} />}
      {mode === 'drones' && <DronesPanel store={droneStore} selected={selectedDrone} patches={patches} places={places} regionName={region.name} regionBox={region.bbox} onMonitorHere={monitorHere}
        onProfile={onFleetProfile} onFocus={onFleetFocus}
        ops={<FleetOps store={droneStore} regionName={region.name} onLaunchHere={monitorHere} deploying={Boolean(deploy)} onDeploy={() => { setDeploy(d => d ? null : { stage: 'draw', area: null, pad: null }); setPicking(false); setDrawingProfile(false); stopFollowing(); }} />}
        onSelect={id => { setSelectedDrone(id); followDrone(id); }}
        onCamera={id => { setSelectedDrone(id); setSensor(null); setFeed({ id, expanded: false }); }}
        onFull={id => { setSelectedDrone(id); setSensor(null); setFeed({ id, expanded: true }); }} />}
    </aside>

    {/* ------------------------------------------------ dock */}
    <section className={`dock ${dockOpen ? 'open' : ''}`} id="timeline-drawer" aria-label="Timeline and charts" aria-hidden={!dockOpen} inert={!dockOpen}>
      <button className="dock-close icon-btn" onClick={() => setDockOpen(false)} aria-label="Hide the timeline and charts" title="Hide (T)"><ChevronDown size={14} /></button>
      <div className="dock-main">
        <Timeline range={timeline.range} t={t} onTime={v => { setT(v); setPlaying(false); }} markers={timeline.markers} band={timeline.band} playing={playing} onPlay={() => setPlaying(p => !p)}
          speed={speed} onSpeed={setSpeed} onStep={stepNext} title={timeline.title} withTime={timeline.withTime} />
        <div className="dock-charts">{charts}</div>
      </div>
      {side}
    </section>

    {detail && activeObservation && <Modal title="Product details" onClose={() => setDetail(false)}>
      {safeUrl(activeObservation.browse_url) && <figure className="browse"><img src={safeUrl(activeObservation.browse_url)} alt="NISAR browse preview" /><figcaption>Browse preview, not calibrated data</figcaption></figure>}
      <KeyValues rows={[['Acquired', activeObservation.date?.replace('T', ' ').replace('Z', ' UTC')], ['Maturity', activeObservation.maturity], ['Polarisation', activeObservation.polarizations?.join(', ')], ['Track / frame', `${activeObservation.track ?? '—'} / ${activeObservation.frame ?? '—'}`], ['Direction', activeObservation.direction || '—'], ['Granule', <span className="num break" key="g">{activeObservation.id}</span>]]} />
      {safeUrl(activeObservation.download_url) && <a className="link" href={safeUrl(activeObservation.download_url)} target="_blank" rel="noreferrer">Original product at ASF <ArrowUpRight size={12} /></a>}
    </Modal>}
    {dossier && job?.result && <Dossier job={job} onClose={() => setDossier(false)} onGlobe={(l: Layer) => { set(l.id === 'change' ? 'change' : 'radar', { on: true }); setDossier(false); globe.current?.flyTo(l.bounds); }} />}
  </div>;

  /** Open the simulated sensor view for a satellite's next pass over the area (searching ahead up to 13 days). */
  function openSensor(s: Sat) {
    // Side-looking radars with a known swath: the next pass that actually images the area, not merely the closest overpass.
    if (SWATH[s.norad]) {
      const at = (s.norad === nisarNext?.norad && nisarNext.at >= Date.now() - 600_000 ? nisarNext.at : null) ?? swathPass(s, aoiCentre.lat, aoiCentre.lon, Date.now() - 600_000, 13);
      if (!at) { setError(`${s.name}'s radar swath does not cover this area in the next 13 days.`); return; }
      setSensor({ norad: s.norad, at }); setFeed(null); setMenu(null); return;
    }
    const known = passes.find(p => p.norad === s.norad && p.at >= Date.now() - 600_000);
    const at = known?.at ?? nextApproach(s.orbit, aoiCentre.lat, aoiCentre.lon, s.reachKm, new Date(), 13)?.at.getTime();
    if (!at) { setError(`${s.name} does not pass within ${s.reachKm} km of this area in the next 13 days.`); return; }
    setSensor({ norad: s.norad, at }); setFeed(null); setMenu(null);
  }

  function flyToSat(s: Sat) {
    const p = subPoint(s.orbit, new Date(satTime ?? Date.now()));
    if (p) globe.current?.flyToPoint(p.lon, p.lat, Math.max(2_000_000, p.altKm * 1000 + 1_500_000));
  }
}

function MenuButton({ id, label, icon, menu, setMenu, children, className = '' }: { id: 'view' | 'analysis' | 'share' | 'more'; label: string; icon: React.ReactNode; menu: string | null; setMenu: (m: 'view' | 'analysis' | 'share' | 'more' | null) => void; children: React.ReactNode; className?: string }) {
  return <div className={`menu-wrap ${className}`}>
    <button className={`menu-btn ${menu === id ? 'on' : ''}`} onClick={() => setMenu(menu === id ? null : id)} aria-haspopup="menu" aria-expanded={menu === id}>{icon}{label}<ChevronDown size={12} /></button>
    {menu === id && <div className={`menu ${id === 'more' ? 'more-menu' : ''}`} role="menu" onClick={e => { if ((e.target as HTMLElement).closest('a')) setMenu(null); }}>{children}</div>}
  </div>;
}

/** Compass, zoom presets, 2D/3D, scale bar, coordinate readout and 2D graticule tags. Telemetry-driven, so it re-renders on its own. */
type MapStyle = 'photo' | 'relief' | 'photoreal';
const NO_HOTSPOTS: never[] = [];

function MapChrome({ flat, setFlat, style, setStyle, photoreal, globe, bbox }: { flat: boolean; setFlat: (v: boolean) => void; style: MapStyle; setStyle: (v: MapStyle) => void; photoreal: boolean; globe: React.RefObject<GlobeHandle | null>; bbox: Bounds }) {
  const tel = useSyncExternalStore(telemetryStore.subscribe, telemetryStore.get);
  const [format, setFormat] = useState<'decimal' | 'dms' | 'mgrs'>('decimal');
  const [full, setFull] = useState(false);
  const [copied, setCopied] = useState(false);
  const half = Math.hypot((bbox[2] - bbox[0]) * 111_320 * Math.cos((bbox[1] + bbox[3]) / 2 * Math.PI / 180), (bbox[3] - bbox[1]) * 110_570) / 2;
  const factor = tel ? Math.max(0.1, (half * 2.74) / Math.max(1, tel.alt)) : 1;
  const scale = tel?.metersPerPixel ? scaleBar(tel.metersPerPixel, 90) : null;
  const lat = tel?.centerLat ?? null, lon = tel?.centerLon ?? null;
  const text = lat === null || lon === null ? 'Point at the Earth' : format === 'decimal' ? decimal(lat, lon) : format === 'dms' ? dmsPretty(lat, lon) : mgrs(lat, lon);
  const view = flat && tel?.view && tel.view[2] > tel.view[0] ? tel.view : null;
  const grid = (lo: number, hi: number) => { const span = hi - lo; const step = [0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30].find(s => span / s <= 7) || 45; const out: number[] = []; for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(+v.toFixed(4)); return out; };
  const dm = (v: number, pos: string, neg: string) => { const a = Math.abs(v); const d = Math.floor(a); const mm = Math.round((a - d) * 60); return `${mm === 60 ? d + 1 : d}°${String(mm === 60 ? 0 : mm).padStart(2, '0')}′${v >= 0 ? pos : neg}`; };
  return <>
    <button className="compass" onClick={() => globe.current?.north()} aria-label="Face north" title="Face north"><span>N</span><svg viewBox="0 0 24 24" style={{ transform: `rotate(${-(tel?.heading ?? 0)}deg)` }}><path d="M12 3 L17 19 L12 15.5 L7 19 Z" fill="currentColor" /></svg></button>
    {view && <div className="graticule" aria-hidden="true">
      {grid(view[0], view[2]).map(v => <span key={`x${v}`} className="gx" style={{ left: `${(v - view[0]) / (view[2] - view[0]) * 100}%` }}>{dm(v, 'E', 'W')}</span>)}
      {grid(view[1], view[3]).map(v => <span key={`y${v}`} className="gy" style={{ top: `${(1 - (v - view[1]) / (view[3] - view[1])) * 100}%` }}>{dm(v, 'N', 'S')}</span>)}
    </div>}
    <div className="coord-readout">
      <button className="num" onClick={() => setFormat(f => f === 'decimal' ? 'dms' : f === 'dms' ? 'mgrs' : 'decimal')} title="Centre of the view. Click to change the format.">{text}</button>
      {lat !== null && lon !== null && <button className="icon-btn" aria-label="Copy coordinates" onClick={() => { void navigator.clipboard?.writeText(format === 'decimal' ? `${lat.toFixed(5)}, ${lon.toFixed(5)}` : text); setCopied(true); setTimeout(() => setCopied(false), 1200); }}>{copied ? <Check size={11} /> : <Copy size={11} />}</button>}
      {tel && <span className="quiet">· eye {tel.alt >= 10_000 ? `${Math.round(tel.alt / 1000).toLocaleString('en')} km` : `${Math.round(tel.alt).toLocaleString('en')} m`}</span>}
    </div>
    <div className="map-controls">
      {scale && <div className="scale"><span className="scale-labels"><i>0</i><i>{scale.label}</i></span><span className="scale-bar" style={{ width: `${scale.px}px` }} /></div>}
      <div className="seg-map" role="group" aria-label="Map style"><button className={!flat && style === 'photo' ? 'on' : ''} onClick={() => { setStyle('photo'); setFlat(false); }} title="3D terrain with satellite imagery">Photo</button>
        <button className={!flat && style === 'relief' ? 'on' : ''} onClick={() => { setStyle('relief'); setFlat(false); }} title="3D relief map: shaded terrain, contours, land cover, summits">Relief</button>
        {photoreal && <button className={!flat && style === 'photoreal' ? 'on' : ''} onClick={() => { setStyle('photoreal'); setFlat(false); }} title="Google Photorealistic 3D Tiles below 300 km. Radar layers stay on the terrain globe.">Photoreal</button>}</div>
      <div className="seg-map"><button className={flat ? 'on' : ''} onClick={() => setFlat(true)}>2D</button><button className={!flat ? 'on' : ''} onClick={() => setFlat(false)}>3D</button></div>
      <label className="zoom-select"><select value="" onChange={e => { if (e.target.value) globe.current?.frame(Number(e.target.value)); }} aria-label="Zoom relative to the area">
        <option value="" hidden>{factor >= 10 ? Math.round(factor) : factor.toFixed(1)}×</option>{[0.5, 1, 2, 4, 8].map(f => <option key={f} value={f}>{f}×</option>)}</select><ChevronDown size={11} /></label>
      <button className="map-btn" onClick={() => { const doc = document as Document & { webkitFullscreenElement?: Element }; if (doc.fullscreenElement) void doc.exitFullscreen(); else void document.documentElement.requestFullscreen?.(); setFull(v => !v); }} aria-label="Full screen">{full ? <Shrink size={14} /> : <Expand size={14} />}</button>
    </div>
  </>;
}

/** What is under the pointer in 3D: height, slope and facing, the changed patch (if any), land cover and position. */
function HoverCard({ jobId, hasPatches, changeBounds }: { jobId?: string; hasPatches: boolean; changeBounds?: Bounds }) {
  const h = useSyncExternalStore(hoverStore.subscribe, hoverStore.get);
  const [change, setChange] = useState<{ key: string; value: number | null } | null>(null);
  const key = h ? `${h.lon.toFixed(4)},${h.lat.toFixed(4)}` : '';
  const inside = Boolean(h && changeBounds && h.lon >= changeBounds[0] && h.lon <= changeBounds[2] && h.lat >= changeBounds[1] && h.lat <= changeBounds[3]);
  useEffect(() => {
    if (!h || !jobId || !inside) return; const c = new AbortController();
    // Read the measured change once the pointer rests, not on every movement.
    const id = setTimeout(() => { void api<{ values: { layer: string; value: number | null }[] }>(`/investigations/${jobId}/sample?lon=${h.lon}&lat=${h.lat}`, { signal: c.signal })
      .then(v => setChange({ key, value: v.values.find(x => x.layer === 'change')?.value ?? null })).catch(() => {}); }, 280);
    return () => { clearTimeout(id); c.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, jobId, inside]);
  if (!h || h.elevation === null) return null;
  const value = change?.key === key ? change.value : undefined;
  const status = h.mark ? { colour: h.mark.color, text: `Inside patch ${h.mark.id}` } : hasPatches ? { colour: '#8d8a7e', text: 'Outside the changed patches' } : null;
  return <div className="hover-card" aria-live="off">
    <b className="num">{Math.round(h.elevation).toLocaleString('en')} m</b>
    {h.slope !== null && <span> · {Math.round(h.slope)}° slope{h.facing && h.facing !== 'level' ? ` · ${h.facing}-facing` : ''}</span>}
    {status && <div className="hc-row"><i style={{ background: status.colour }} />{status.text}{typeof value === 'number' && <span className="num"> · {value >= 0 ? '+' : '−'}{Math.abs(value).toFixed(1)} dB</span>}</div>}
    {!status && typeof value === 'number' && <div className="hc-row"><i style={{ background: value >= 0 ? '#f0a35e' : '#7fb0e0' }} />Change <span className="num">{value >= 0 ? '+' : '−'}{Math.abs(value).toFixed(1)} dB</span></div>}
    {h.cover && <div className="hc-sub">{h.cover}</div>}
    <div className="hc-coord num">{dmsPretty(h.lat, h.lon)}</div>
  </div>;
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { const prev = document.activeElement as HTMLElement | null; box.current?.querySelector<HTMLElement>('button')?.focus(); return () => prev?.focus(); }, []);
  return <div className="scrim" onClick={onClose}><div className="modal" ref={box} role="dialog" aria-modal="true" aria-label={title} onClick={e => e.stopPropagation()}>
    <div className="pop-head"><span>{title}</span><button className="icon-btn" onClick={onClose} aria-label="Close"><X size={14} /></button></div>
    <div className="modal-body">{children}</div>
  </div></div>;
}
