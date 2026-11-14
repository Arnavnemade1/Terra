import * as C from 'cesium';
import * as sat from 'satellite.js';
import { ago, groundTrack, nextApproach, orbiter, periodMinutes, subPoint, type Orbiter, type SubPoint, type Tle } from './geo';

export type LayerKey = 'nisar' | 'fleet' | 'coverage' | 'quakes' | 'fires' | 'events';
export type FeedState = { state: 'loading' | 'live' | 'stale' | 'offline'; count: number; source?: string; fetchedAt?: string };
export type Selection = {
  kind: 'quake' | 'event' | 'fire' | 'frame' | 'satellite'; title: string; subtitle: string; lon: number; lat: number;
  fields: [string, string][]; url?: string; color: string;
};
type Quake = { id: string; mag: number; place: string; time: number; lon: number; lat: number; depth: number | null; url?: string; tsunami: boolean; alert?: string };
type EoEvent = { id: string; title: string; category: string; category_title: string; lon: number; lat: number; date: string; source?: string;
  provider?: string; alert?: string | null; severity?: string | null; country?: string | null; summary?: string | null; current?: boolean };
export const ALERT_COLOR: Record<string, string> = { red: '#e5634f', orange: '#f08a3c', yellow: '#e8c34a', green: '#8fbf6a' };
type Frame = { id: string; start: string; direction?: string; track?: number; frame?: number; config?: string; ring: [number, number][] };
type Fire = [number, number, number, number, string];

const SANS = '"Archivo Variable", "Helvetica Neue", Arial, sans-serif';
export const ACCENT = '#ff5a1f';
const HALO = C.Color.fromCssColorString('rgba(8, 9, 7, 0.88)');
const css = (v: string, a = 1) => C.Color.fromCssColorString(v).withAlpha(a);

export const EVENT_STYLE: Record<string, { color: string; code: string }> = {
  wildfires: { color: '#f2c14a', code: 'Wildfire' }, volcanoes: { color: '#e5634f', code: 'Volcano' }, severeStorms: { color: '#9fb7d4', code: 'Storm' },
  floods: { color: '#6f9ad8', code: 'Flood' }, seaLakeIce: { color: '#dce6ea', code: 'Sea & lake ice' }, drought: { color: '#c9a86a', code: 'Drought' },
  dustHaze: { color: '#bfae8c', code: 'Dust & haze' }, landslides: { color: '#a9b86a', code: 'Landslide' }, earthquakes: { color: '#e8e2d2', code: 'Earthquake' },
  snow: { color: '#eef1f2', code: 'Snow' }, tempExtremes: { color: '#d98a7a', code: 'Temperature' }, manmade: { color: '#b9a3c9', code: 'Human-made' },
  waterColor: { color: '#7fb5a8', code: 'Water colour' },
};

const glyphCache = new Map<string, HTMLCanvasElement>();
function glyph(kind: 'diamond' | 'sat' | 'ring', color: string, size = 30): HTMLCanvasElement {
  const key = `${kind}:${color}:${size}`; const hit = glyphCache.get(key); if (hit) return hit;
  const c = document.createElement('canvas'); c.width = c.height = size * 2; const g = c.getContext('2d')!; g.scale(2, 2);
  g.strokeStyle = color; g.fillStyle = color; g.lineWidth = 1.4; g.lineCap = 'square';
  const s = size, k = s * 0.28, m = 2;
  void k; void m;
  if (kind === 'diamond') {
    const h = s * 0.26; g.save(); g.translate(s / 2, s / 2); g.rotate(Math.PI / 4);
    g.fillStyle = 'rgba(8,9,7,0.85)'; g.fillRect(-h - 1.5, -h - 1.5, 2 * h + 3, 2 * h + 3);
    g.fillStyle = color; g.fillRect(-h, -h, 2 * h, 2 * h); g.restore();
  } else if (kind === 'sat') {
    g.save(); g.translate(s / 2, s / 2); g.rotate(Math.PI / 4);
    g.strokeRect(-3.5, -3.5, 7, 7); g.fillRect(-1.5, -1.5, 3, 3);
    g.beginPath(); g.moveTo(-4, 0); g.lineTo(-7, 0); g.moveTo(4, 0); g.lineTo(7, 0); g.stroke();
    g.strokeRect(-13, -3, 6, 6); g.strokeRect(7, -3, 6, 6); g.restore();
  } else {
    g.strokeStyle = 'rgba(8,9,7,0.7)'; g.lineWidth = 3.2; g.beginPath(); g.arc(s / 2, s / 2, s / 2 - 3, 0, Math.PI * 2); g.stroke();
    g.strokeStyle = color; g.lineWidth = 1.5; g.beginPath(); g.arc(s / 2, s / 2, s / 2 - 3, 0, Math.PI * 2); g.stroke();
  }
  glyphCache.set(key, c); return c;
}

/** Cartographic labels: sans, proper case, a dark halo instead of a box. */
function labelStyle(color: string, size = 11, weight = 500) {
  return {
    font: `${weight} ${size}px ${SANS}`, fillColor: css(color), outlineColor: HALO, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE,
    horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.CENTER, pixelOffset: new C.Cartesian2(10, 0),
  };
}

export class LiveLayers {
  private viewer: C.Viewer;
  private onStatus: (s: Record<string, FeedState>) => void;
  private status: Record<string, FeedState> = {};
  private visible: Record<LayerKey, boolean> = { nisar: true, fleet: true, coverage: true, quakes: true, fires: true, events: true };
  private timers: ReturnType<typeof setInterval>[] = [];
  private disposed = false;
  // collections
  private quakePoints = new C.PointPrimitiveCollection(); private quakeRings: C.BillboardCollection; private quakeLabels: C.LabelCollection;
  private firePoints = new C.PointPrimitiveCollection(); private fireGlow = new C.PointPrimitiveCollection();
  private eventIcons: C.BillboardCollection; private eventLabels: C.LabelCollection;
  private frameFill: C.GroundPrimitive | null = null; private frameLine: C.GroundPolylinePrimitive | null = null; private frameLabels: C.LabelCollection;
  private satIcons: C.BillboardCollection; private satLabels: C.LabelCollection; private satLines = new C.PolylineCollection(); private fleetPoints = new C.PointPrimitiveCollection();
  // orbit state
  nisar: Orbiter | null = null; private fleet: Orbiter[] = [];
  private nisarIcon?: C.Billboard; private nisarLabel?: C.Label; private nadir?: C.Polyline; private trackPast?: C.Polyline; private trackNext?: C.Polyline; private orbit3d?: C.Polyline;
  private fleetItems: { o: Orbiter; point: C.PointPrimitive; label: C.Label }[] = [];
  nisarNow: SubPoint | null = null;
  private lastTrack = 0;
  /** Satellite clock: null follows real time; a number shows positions at that moment (timeline scrubbing). */
  private simTime: number | null = null;
  private focus: number | null = null;
  private aoi: { lon: number; lat: number } | null = null;
  approach: { at: Date; km: number } | null = null;
  counts = { quakes: 0, fires: 0, events: 0, frames: 0, fleet: 0 };
  private quakes: Quake[] = []; private events: EoEvent[] = []; private frames: Frame[] = [];

  constructor(viewer: C.Viewer, onStatus: (s: Record<string, FeedState>) => void) {
    this.viewer = viewer; this.onStatus = onStatus;
    const scene = viewer.scene;
    this.quakeRings = new C.BillboardCollection({ scene }); this.quakeLabels = new C.LabelCollection({ scene });
    this.eventIcons = new C.BillboardCollection({ scene }); this.eventLabels = new C.LabelCollection({ scene });
    this.frameLabels = new C.LabelCollection({ scene });
    this.satIcons = new C.BillboardCollection({ scene }); this.satLabels = new C.LabelCollection({ scene });
    for (const p of [this.fireGlow, this.firePoints, this.quakePoints, this.quakeRings, this.quakeLabels, this.eventIcons, this.eventLabels, this.frameLabels, this.satLines, this.fleetPoints, this.satIcons, this.satLabels]) scene.primitives.add(p);
    void this.loadSatellites(); void this.loadQuakes(); void this.loadEvents(); void this.loadFires(); void this.loadFrames();
    this.timers.push(setInterval(() => void this.loadQuakes(), 5 * 60_000), setInterval(() => void this.loadEvents(), 15 * 60_000),
      setInterval(() => void this.loadFires(), 30 * 60_000), setInterval(() => void this.loadFrames(), 20 * 60_000),
      setInterval(() => void this.loadSatellites(), 6 * 3600_000), setInterval(() => this.tickSatellites(), 1000));
  }

  private setStatus(key: string, value: FeedState) { this.status = { ...this.status, [key]: value }; this.onStatus(this.status); }
  private async fetchFeed<T>(key: string, url: string): Promise<T | null> {
    if (!this.status[key]) this.setStatus(key, { state: 'loading', count: 0 });
    try {
      const res = await fetch(url); if (!res.ok) throw new Error(String(res.status));
      return await res.json() as T;
    } catch { if (!this.disposed) this.setStatus(key, { ...(this.status[key] || { count: 0 }), state: this.status[key]?.count ? 'stale' : 'offline' }); return null; }
  }
  private render() { if (!this.disposed && !this.viewer.isDestroyed()) this.viewer.scene.requestRender(); }

  // ---------- satellites ----------
  private async loadSatellites() {
    const data = await this.fetchFeed<{ nisar: Tle; fleet: Tle[]; status: string; source: string; fetched_at: string }>('satellites', '/api/feeds/satellites');
    if (!data || this.disposed) return;
    this.nisar = orbiter(data.nisar);
    this.fleet = data.fleet.map(orbiter).filter((o): o is Orbiter => Boolean(o));
    this.counts.fleet = this.fleet.length;
    this.buildSatellites();
    this.setStatus('satellites', { state: data.status === 'stale' ? 'stale' : 'live', count: this.fleet.length + 1, source: data.source, fetchedAt: data.fetched_at });
    if (this.aoi) this.computeApproach();
  }
  private buildSatellites() {
    this.satIcons.removeAll(); this.satLabels.removeAll(); this.satLines.removeAll(); this.fleetPoints.removeAll(); this.fleetItems = [];
    const now = new Date();
    // Primitives are created at their real positions: a zero vector breaks Cesium's normalisation.
    const at = (o: Orbiter) => { const p = subPoint(o, now); return p ? C.Cartesian3.fromDegrees(p.lon, p.lat, p.altKm * 1000) : null; };
    const nisarPos = this.nisar ? at(this.nisar) : null;
    if (this.nisar && nisarPos) {
      const id = { kind: 'satellite', key: 'nisar' };
      this.nisarIcon = this.satIcons.add({ image: glyph('sat', ACCENT, 34), position: nisarPos, id, scale: 0.9 });
      this.nisarLabel = this.satLabels.add({ ...labelStyle(ACCENT, 12, 650), text: 'NISAR', position: nisarPos, id, pixelOffset: new C.Cartesian2(18, 0) });
      const dashed = C.Material.fromType('PolylineDash', { color: css(ACCENT, 0.9), dashLength: 12 });
      this.trackNext = this.satLines.add({ positions: [], width: 1.6, material: dashed });
      this.trackPast = this.satLines.add({ positions: [], width: 1.4, material: C.Material.fromType('Color', { color: css(ACCENT, 0.32) }) });
      this.orbit3d = this.satLines.add({ positions: [], width: 1, material: C.Material.fromType('Color', { color: css(ACCENT, 0.18) }) });
      this.nadir = this.satLines.add({ positions: [], width: 1, material: C.Material.fromType('Color', { color: css(ACCENT, 0.35) }) });
    }
    for (const o of this.fleet) {
      const position = at(o); if (!position) continue;
      const id = { kind: 'satellite', key: String(o.tle.norad) };
      const point = this.fleetPoints.add({ position, pixelSize: 3, color: css('#d8d4c8', 0.9), outlineColor: css('#080907', 0.7), outlineWidth: 1, id });
      const label = this.satLabels.add({ ...labelStyle('#b3afa3', 10, 450), text: o.tle.name.replace(/\s+/g, ' ').slice(0, 22), position, id,
        pixelOffset: new C.Cartesian2(7, 0), translucencyByDistance: new C.NearFarScalar(2e6, 0.95, 1.1e7, 0.35), distanceDisplayCondition: new C.DistanceDisplayCondition(0, 1.1e7) });
      this.fleetItems.push({ o, point, label });
    }
    if (this.focus !== null) this.setFocus(this.focus);
    this.lastTrack = 0; this.tickSatellites(now); this.applyVisibility();
  }
  setTime(t: number | null) { this.simTime = t; this.lastTrack = 0; this.tickSatellites(); }
  setFocus(norad: number | null) {
    this.focus = norad;
    for (const item of this.fleetItems) {
      const on = item.o.tle.norad === norad;
      item.point.pixelSize = on ? 7 : 3; item.point.color = on ? css(ACCENT) : css('#d8d4c8', 0.9);
      item.label.fillColor = on ? css(ACCENT) : css('#b3afa3'); item.label.font = `${on ? 650 : 450} ${on ? 12 : 10}px ${SANS}`;
      item.label.distanceDisplayCondition = new C.DistanceDisplayCondition(0, on ? 4e7 : 1.1e7);
    }
    this.render();
  }
  private tickSatellites(now = new Date(this.simTime ?? Date.now())) {
    if (this.disposed) return;
    if (this.nisar && this.nisarIcon) {
      const p = subPoint(this.nisar, now);
      if (p) {
        this.nisarNow = p;
        const pos = C.Cartesian3.fromDegrees(p.lon, p.lat, p.altKm * 1000);
        this.nisarIcon.position = pos; this.nisarLabel!.position = pos;
        this.nisarLabel!.text = `NISAR\n${p.altKm.toFixed(0)} km · ${p.speedKms.toFixed(2)} km/s`;
        this.nadir!.positions = [pos, C.Cartesian3.fromDegrees(p.lon, p.lat, 0)];
      }
      if (now.getTime() - this.lastTrack > 30_000) { this.lastTrack = now.getTime(); this.updateTracks(now); }
    }
    if (this.visible.fleet) for (const item of this.fleetItems) {
      const p = subPoint(item.o, now); if (!p) continue; // keep the last valid position
      const pos = C.Cartesian3.fromDegrees(p.lon, p.lat, p.altKm * 1000); item.point.position = pos; item.label.position = pos;
    }
    if (this.visible.nisar || this.visible.fleet) this.render();
  }
  private updateTracks(now: Date) {
    if (!this.nisar) return;
    const period = periodMinutes(this.nisar);
    const toPositions = (pts: SubPoint[], h = 2500) => pts.map(p => C.Cartesian3.fromDegrees(p.lon, p.lat, h));
    this.trackNext!.positions = toPositions(groundTrack(this.nisar, now, 0, period * 0.55, 20));
    this.trackPast!.positions = toPositions(groundTrack(this.nisar, now, -period * 0.45, 0, 20));
    // The orbit itself, drawn in Earth-fixed coordinates at its true altitude.
    const ring: C.Cartesian3[] = [];
    for (let t = -period * 0.5 * 60; t <= period * 0.5 * 60; t += 30) {
      const d = new Date(now.getTime() + t * 1000); const pv = sat.propagate(this.nisar.rec, d);
      if (!pv.position || typeof pv.position === 'boolean') continue;
      const ecf = sat.eciToEcf(pv.position, sat.gstime(d));
      if ([ecf.x, ecf.y, ecf.z].every(Number.isFinite)) ring.push(new C.Cartesian3(ecf.x * 1000, ecf.y * 1000, ecf.z * 1000));
    }
    this.orbit3d!.positions = ring;
  }
  setAoi(lon: number, lat: number) { this.aoi = { lon, lat }; this.computeApproach(); }
  private computeApproach() { if (this.nisar && this.aoi) this.approach = nextApproach(this.nisar, this.aoi.lat, this.aoi.lon, 350); }

  // ---------- earthquakes ----------
  private async loadQuakes() {
    const data = await this.fetchFeed<{ items: Quake[]; status: string; source: string; fetched_at: string }>('earthquakes', '/api/feeds/earthquakes');
    if (!data || this.disposed) return;
    this.quakes = data.items; this.quakePoints.removeAll(); this.quakeRings.removeAll(); this.quakeLabels.removeAll();
    const now = Date.now();
    data.items.forEach((q, i) => {
      const age = now - q.time; const color = age < 86400000 ? '#ef5b45' : '#ece6d6';
      const position = C.Cartesian3.fromDegrees(q.lon, q.lat, 1500); const id = { kind: 'quake', index: i };
      this.quakePoints.add({ position, id, pixelSize: Math.max(3.5, 2.5 + (q.mag - 2.5) * 2.4), color: css(color, age < 3 * 86400000 ? 0.95 : 0.55), outlineColor: css('#080907', 0.75), outlineWidth: 1 });
      if (q.mag >= 5) this.quakeRings.add({ position, id, image: glyph('ring', color, 40), scale: 0.45 + (q.mag - 5) * 0.3, color: css('#fff', 0.8), scaleByDistance: new C.NearFarScalar(1e6, 1.2, 2.2e7, 0.6) });
      if (q.mag >= 4.5) {
        this.quakeLabels.add({ ...labelStyle(color, 11), position, id, text: `M${q.mag.toFixed(1)} · ${ago(q.time).toLowerCase()}\n${q.place.slice(0, 40)}`,
          distanceDisplayCondition: new C.DistanceDisplayCondition(0, q.mag >= 5.5 ? 4.5e7 : 9e6) });
      }
    });
    this.counts.quakes = data.items.length; this.applyVisibility();
    this.setStatus('earthquakes', { state: data.status === 'stale' ? 'stale' : 'live', count: data.items.length, source: data.source, fetchedAt: data.fetched_at });
  }

  // ---------- Natural hazards: USGS volcano alerts, GDACS and NASA EONET ----------
  private async loadEvents() {
    const data = await this.fetchFeed<{ items: EoEvent[]; status: string; source: string; fetched_at: string }>('events', '/api/feeds/events');
    if (!data || this.disposed) return;
    this.events = data.items; this.eventIcons.removeAll(); this.eventLabels.removeAll();
    data.items.forEach((e, i) => {
      const st = EVENT_STYLE[e.category] || { color: '#cfcabd', code: 'Event' };
      const position = C.Cartesian3.fromDegrees(e.lon, e.lat, 1500); const id = { kind: 'event', index: i };
      // Orange and red alerts are drawn larger and labelled from farther away.
      const major = e.alert === 'red' || e.alert === 'orange';
      this.eventIcons.add({ position, id, image: glyph('diamond', st.color, 30), scale: major ? 0.6 : 0.42, scaleByDistance: new C.NearFarScalar(2e5, 1.15, 2.2e7, 0.5) });
      this.eventLabels.add({ ...labelStyle(st.color, 11), position, id, text: e.title.slice(0, 44),
        distanceDisplayCondition: new C.DistanceDisplayCondition(0, major ? 1.2e7 : e.category === 'wildfires' ? 2.5e6 : 6e6) });
    });
    this.counts.events = data.items.length; this.applyVisibility();
    this.setStatus('events', { state: data.status === 'stale' ? 'stale' : 'live', count: data.items.length, source: data.source, fetchedAt: data.fetched_at });
  }

  // ---------- NASA FIRMS active fires ----------
  private async loadFires() {
    const data = await this.fetchFeed<{ items: Fire[]; status: string; source: string; fetched_at: string }>('fires', '/api/feeds/fires');
    if (!data || this.disposed) return;
    this.firePoints.removeAll(); this.fireGlow.removeAll();
    const glowScale = new C.NearFarScalar(1e5, 1.6, 2e7, 0.32); const pointScale = new C.NearFarScalar(1e5, 1.3, 2e7, 0.6);
    data.items.forEach((f, i) => {
      const [lon, lat, frp] = f; const position = C.Cartesian3.fromDegrees(lon, lat, 1200); const id = { kind: 'fire', index: i, item: f };
      const hot = frp >= 50 ? '#ffb02e' : frp >= 12 ? '#f6c24a' : '#efd27a';
      this.fireGlow.add({ position, pixelSize: 7 + Math.min(12, Math.log2(1 + frp) * 2), color: css(hot, 0.1), scaleByDistance: glowScale });
      this.firePoints.add({ position, id, pixelSize: 2.5 + Math.min(4, Math.log2(1 + frp) * 0.6), color: css(hot, 0.95), scaleByDistance: pointScale });
    });
    this.counts.fires = data.items.length; this.applyVisibility();
    this.setStatus('fires', { state: data.status === 'stale' ? 'stale' : 'live', count: data.items.length, source: data.source, fetchedAt: data.fetched_at });
  }

  // ---------- NISAR GCOV coverage (ASF catalog) ----------
  private async loadFrames() {
    const data = await this.fetchFeed<{ items: Frame[]; status: string; source: string; fetched_at: string }>('nisar', '/api/feeds/nisar');
    if (!data || this.disposed) return;
    this.frames = data.items;
    const scene = this.viewer.scene;
    if (this.frameFill) scene.groundPrimitives.remove(this.frameFill); if (this.frameLine) scene.groundPrimitives.remove(this.frameLine);
    this.frameLabels.removeAll();
    const now = Date.now(); const fills: C.GeometryInstance[] = []; const lines: C.GeometryInstance[] = [];
    data.items.forEach((f, i) => {
      // Published rings repeat the first vertex; a looped ground polyline needs distinct vertices.
      const ring = f.ring.filter((c, k, all) => k === 0 || Math.abs(c[0] - all[k - 1][0]) + Math.abs(c[1] - all[k - 1][1]) > 1e-6);
      if (ring.length > 1 && Math.abs(ring[0][0] - ring.at(-1)![0]) + Math.abs(ring[0][1] - ring.at(-1)![1]) < 1e-6) ring.pop();
      if (ring.length < 3) return;
      const positions = ring.map(([lon, lat]) => C.Cartesian3.fromDegrees(lon, lat));
      const fresh = now - Date.parse(f.start) < 86400000; const id = { kind: 'frame', index: i };
      fills.push(new C.GeometryInstance({ id, geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(positions) }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(css(ACCENT, fresh ? 0.1 : 0.045)) } }));
      lines.push(new C.GeometryInstance({ id, geometry: new C.GroundPolylineGeometry({ positions, width: fresh ? 1.6 : 1, loop: true }),
        attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(css(ACCENT, fresh ? 0.9 : 0.5)) } }));
      const lon = ring.reduce((s, c) => s + c[0], 0) / ring.length; const lat = ring.reduce((s, c) => s + c[1], 0) / ring.length;
      this.frameLabels.add({ ...labelStyle('#ffb08f', 10), position: C.Cartesian3.fromDegrees(lon, lat, 1500), id,
        text: `Track ${f.track ?? '—'} · frame ${f.frame ?? '—'}\n${(f.direction || '').toLowerCase()} · ${ago(Date.parse(f.start)).toLowerCase()}`,
        horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.CENTER, pixelOffset: C.Cartesian2.ZERO,
        distanceDisplayCondition: new C.DistanceDisplayCondition(0, 3.2e6) });
    });
    this.frameFill = fills.length ? scene.groundPrimitives.add(new C.GroundPrimitive({ geometryInstances: fills, appearance: new C.PerInstanceColorAppearance({ flat: true, translucent: true }), asynchronous: true })) : null;
    this.frameLine = lines.length ? scene.groundPrimitives.add(new C.GroundPolylinePrimitive({ geometryInstances: lines, appearance: new C.PolylineColorAppearance(), asynchronous: true })) : null;
    this.counts.frames = data.items.length; this.applyVisibility();
    this.setStatus('nisar', { state: data.status === 'stale' ? 'stale' : 'live', count: data.items.length, source: data.source, fetchedAt: data.fetched_at });
  }

  // ---------- visibility & picking ----------
  setVisible(key: LayerKey, on: boolean) { this.visible[key] = on; this.applyVisibility(); if (on && (key === 'fleet' || key === 'nisar')) this.tickSatellites(); }
  private applyVisibility() {
    const v = this.visible;
    this.quakePoints.show = this.quakeRings.show = this.quakeLabels.show = v.quakes;
    this.firePoints.show = this.fireGlow.show = v.fires;
    this.eventIcons.show = this.eventLabels.show = v.events;
    if (this.frameFill) this.frameFill.show = v.coverage; if (this.frameLine) this.frameLine.show = v.coverage; this.frameLabels.show = v.coverage;
    this.fleetPoints.show = v.fleet;
    for (const item of this.fleetItems) item.label.show = v.fleet;
    if (this.nisarIcon) { this.nisarIcon.show = v.nisar; this.nisarLabel!.show = v.nisar; }
    for (const line of [this.trackNext, this.trackPast, this.orbit3d, this.nadir]) if (line) line.show = v.nisar;
    this.render();
  }

  describe(id: unknown): Selection | null {
    const ref = id as { kind?: string; index?: number; key?: string; item?: Fire } | undefined;
    if (!ref?.kind) return null;
    if (ref.kind === 'quake' && ref.index !== undefined) {
      const q = this.quakes[ref.index]; if (!q) return null;
      return { kind: 'quake', color: '#ece6d6', title: `M${q.mag.toFixed(1)} earthquake`, subtitle: q.place, lon: q.lon, lat: q.lat, url: q.url,
        fields: [['Magnitude', q.mag.toFixed(1)], ['Depth', q.depth != null ? `${q.depth.toFixed(1)} km` : '—'], ['Time (UTC)', new Date(q.time).toISOString().replace('T', ' ').slice(0, 19)], ['Tsunami flag', q.tsunami ? 'Yes' : 'No'], ['Source', 'USGS']] };
    }
    if (ref.kind === 'event' && ref.index !== undefined) {
      const e = this.events[ref.index]; if (!e) return null;
      return { kind: 'event', color: EVENT_STYLE[e.category]?.color || '#cbd5e1', title: e.title, subtitle: e.category_title, lon: e.lon, lat: e.lat, url: e.source,
        fields: [['Type', e.category_title], ...(e.alert ? [['Alert level', `${e.alert[0].toUpperCase()}${e.alert.slice(1)}`] as [string, string]] : []),
          ...(e.severity ? [['Severity', e.severity] as [string, string]] : []), ...(e.country ? [['Country', e.country] as [string, string]] : []),
          ['Last update', e.date ? e.date.replace('T', ' ').slice(0, 16) : '—'], ['Source', e.provider || 'NASA EONET']] };
    }
    if (ref.kind === 'fire' && ref.item) {
      const [lon, lat, frp, conf, acq] = ref.item;
      return { kind: 'fire', color: '#f6c24a', title: 'Active fire detection', subtitle: `${lat.toFixed(3)}°, ${lon.toFixed(3)}°`, lon, lat,
        fields: [['Radiative power', `${frp} MW`], ['Confidence', `${conf}%`], ['Acquired (UTC)', acq], ['Sensor', 'MODIS · NASA FIRMS']] };
    }
    if (ref.kind === 'frame' && ref.index !== undefined) {
      const f = this.frames[ref.index]; if (!f) return null;
      const lon = f.ring[0][0], lat = f.ring[0][1];
      const lons = f.ring.map(c => c[0]), lats = f.ring.map(c => c[1]);
      return { kind: 'frame', color: ACCENT, title: `NISAR GCOV · track ${f.track} frame ${f.frame}`, subtitle: f.id, lon: (Math.min(...lons) + Math.max(...lons)) / 2 || lon, lat: (Math.min(...lats) + Math.max(...lats)) / 2 || lat,
        fields: [['Acquired (UTC)', f.start?.replace('T', ' ').replace('Z', '')], ['Direction', f.direction || '—'], ['Production', f.config || '—'], ['Source', 'NASA / ASF catalog']] };
    }
    if (ref.kind === 'satellite') {
      const o = ref.key === 'nisar' ? this.nisar : this.fleet.find(f => String(f.tle.norad) === ref.key);
      if (!o) return null; const p = subPoint(o, new Date()); if (!p) return null;
      return { kind: 'satellite', color: ACCENT, title: o.tle.name.trim(), subtitle: `NORAD ${o.tle.norad}`, lon: p.lon, lat: p.lat,
        fields: [['Altitude', `${p.altKm.toFixed(0)} km`], ['Speed', `${p.speedKms.toFixed(2)} km/s`], ['Inclination', `${(o.rec.inclo * 180 / Math.PI).toFixed(2)}°`], ['Period', `${periodMinutes(o).toFixed(1)} min`], ['Elements', 'CelesTrak GP · SGP4']] };
    }
    return null;
  }

  /** Counts of each feed inside a view rectangle (radians), for the HUD summary. */
  inView(rect: C.Rectangle | undefined) {
    if (!rect) return null;
    const inside = (lon: number, lat: number) => C.Rectangle.contains(rect, C.Cartographic.fromDegrees(lon, lat));
    return { quakes: this.quakes.filter(q => inside(q.lon, q.lat)).length, events: this.events.filter(e => inside(e.lon, e.lat)).length,
      frames: this.frames.filter(f => inside(f.ring[0][0], f.ring[0][1])).length };
  }

  destroy() {
    this.disposed = true; this.timers.forEach(clearInterval);
    if (this.viewer.isDestroyed()) return;
    const scene = this.viewer.scene;
    for (const p of [this.fireGlow, this.firePoints, this.quakePoints, this.quakeRings, this.quakeLabels, this.eventIcons, this.eventLabels, this.frameLabels, this.satLines, this.fleetPoints, this.satIcons, this.satLabels]) scene.primitives.remove(p);
    if (this.frameFill) scene.groundPrimitives.remove(this.frameFill); if (this.frameLine) scene.groundPrimitives.remove(this.frameLine);
  }
}
