import * as C from 'cesium';
import { aircraftGltf } from './droneModel';
import { ft, type DroneState, type DroneStore, type FieldNote, type Source } from './drones';

type Craft = { model: C.Model | null; loading: boolean; aircraft: string; pill: C.Billboard; tether: C.Polyline; sight: C.Polyline; link: C.Polyline; ground: number | null; pillKey: string; pos: C.Cartesian3 | null };
const DEG = Math.PI / 180;
const ACCENT = '#e5622d';
const SILHOUETTE = C.Color.fromCssColorString(ACCENT);
const RELAY_RANGE_M = 6000;  // as the coordinator plans: beyond this from launch, traffic goes through a relay
const LINK = { good: C.Color.fromCssColorString('#7be36b'), fair: C.Color.fromCssColorString('#e8b04a'), poor: C.Color.fromCssColorString('#ef5b4c') };
const NOTE_COLOR: Record<string, string> = { warm: '#ff6a3d', change: '#e8b04a', station: '#f4f1e8', observation: '#cfc9bb' };
const TAG: Record<Source, string> = { real: 'LIVE', sitl: 'SITL', virtual: 'SIM' };
const TAG_COLOR: Record<Source, string> = { real: '#9fe07a', sitl: '#8fc8ff', virtual: 'rgba(236, 233, 223, 0.45)' };

const metres = (a: [number, number], b: [number, number]) => Math.hypot((a[0] - b[0]) * 111_320 * Math.cos(a[1] * DEG), (a[1] - b[1]) * 110_574);

/** A small map pin: a diamond for a field note, a bracketed reading for warm ground. */
function markImage(kind: string, text?: string): HTMLCanvasElement {
  const k = 2; const c = document.createElement('canvas'); const x = c.getContext('2d')!;
  const colour = NOTE_COLOR[kind] ?? '#f4f1e8';
  if (!text) {
    c.width = c.height = 16 * k; x.translate(8 * k, 8 * k); x.rotate(Math.PI / 4);
    x.fillStyle = 'rgba(16, 16, 14, 0.85)'; x.fillRect(-5 * k, -5 * k, 10 * k, 10 * k);
    x.lineWidth = 1.5 * k; x.strokeStyle = colour; x.strokeRect(-4.2 * k, -4.2 * k, 8.4 * k, 8.4 * k);
    x.fillStyle = colour; x.fillRect(-1.6 * k, -1.6 * k, 3.2 * k, 3.2 * k);
    return c;
  }
  x.font = `600 ${10 * k}px "IBM Plex Mono", monospace`; const w = x.measureText(text).width;
  c.width = Math.ceil(w + 22 * k); c.height = 20 * k;
  x.fillStyle = 'rgba(16, 16, 14, 0.82)'; x.fillRect(0, 0, c.width, c.height);
  x.strokeStyle = colour; x.lineWidth = 1.5 * k; const b = 5 * k, W = c.width - k, H = c.height - k;
  x.beginPath(); x.moveTo(k, b); x.lineTo(k, k); x.lineTo(b, k); x.moveTo(W - b, k); x.lineTo(W, k); x.lineTo(W, b);
  x.moveTo(W, H - b); x.lineTo(W, H); x.lineTo(W - b, H); x.moveTo(b, H); x.lineTo(k, H); x.lineTo(k, H - b); x.stroke();
  x.fillStyle = colour; x.textBaseline = 'middle'; x.fillText(text, 11 * k, c.height / 2 + k);
  return c;
}

/** The launch site: a ground station pad. */
function padImage(): HTMLCanvasElement {
  const k = 2; const c = document.createElement('canvas'); c.width = c.height = 26 * k; const x = c.getContext('2d')!;
  x.beginPath(); x.arc(13 * k, 13 * k, 11 * k, 0, Math.PI * 2); x.fillStyle = 'rgba(16, 16, 14, 0.85)'; x.fill();
  x.lineWidth = 1.5 * k; x.strokeStyle = '#f4f1e8'; x.stroke();
  x.font = `700 ${12 * k}px "Archivo Variable", Arial, sans-serif`; x.fillStyle = '#f4f1e8'; x.textAlign = 'center'; x.textBaseline = 'middle'; x.fillText('H', 13 * k, 13.5 * k);
  return c;
}

/** The label tag above each aircraft: a status light, the call sign and the height above ground. */
function pillImage(callsign: string, height: string, selected: boolean, source: Source, stale: boolean): HTMLCanvasElement {
  const k = 2; const c = document.createElement('canvas'); const x = c.getContext('2d')!;
  const bold = `600 ${12 * k}px "Archivo Variable", Arial, sans-serif`, light = `500 ${12 * k}px "Archivo Variable", Arial, sans-serif`;
  x.font = bold; const w1 = x.measureText(callsign).width; x.font = light; const w2 = x.measureText(height).width;
  const tag = TAG[source]; x.font = `600 ${9 * k}px "IBM Plex Mono", monospace`; const w3 = x.measureText(tag).width;
  const pad = 8 * k, dot = 7 * k, gap = 6 * k, h = 23 * k;
  c.width = Math.ceil(pad + dot + gap + w1 + gap + w2 + gap + w3 + pad); c.height = h + 2 * k;
  x.beginPath(); x.roundRect(k, k, c.width - 2 * k, h, h / 2);
  x.fillStyle = selected ? ACCENT : 'rgba(21, 21, 18, 0.9)'; x.fill();
  x.lineWidth = k; x.strokeStyle = selected ? 'rgba(255, 220, 200, 0.55)' : 'rgba(255, 255, 255, 0.16)'; x.stroke();
  x.beginPath(); x.arc(pad + dot / 2, k + h / 2, dot / 2, 0, Math.PI * 2); x.fillStyle = stale ? '#e8b04a' : '#7be36b'; x.fill();
  x.textBaseline = 'middle'; let at = pad + dot + gap;
  x.font = bold; x.fillStyle = '#fbf8f1'; x.fillText(callsign, at, k + h / 2 + k); at += w1 + gap;
  x.font = light; x.fillStyle = selected ? 'rgba(255, 245, 238, 0.85)' : 'rgba(236, 233, 223, 0.7)'; x.fillText(height, at, k + h / 2 + k); at += w2 + gap;
  x.font = `600 ${9 * k}px "IBM Plex Mono", monospace`; x.fillStyle = selected ? 'rgba(255, 245, 238, 0.7)' : TAG_COLOR[source]; x.fillText(tag, at, k + h / 2 + k);
  return c;
}

/**
 * Draws the fleet on the main globe: a 3D model per aircraft (banking and pitching as it flies), a label tag,
 * a thin line to the ground, the camera's line of sight for the selected aircraft, each mission's route, each
 * aircraft's radio link (to the launch site or through a relay aircraft, coloured by signal), the launch pad,
 * the agents' field notes and the warm ground a thermal mission is watching.
 */
export class DroneLayer {
  private v: C.Viewer; private store: DroneStore;
  private crafts = new Map<string, Craft>();
  private pills: C.BillboardCollection; private lines: C.PolylineCollection; private routes: C.CustomDataSource;
  private marks: C.BillboardCollection; private marksKey = ''; private pad: C.Billboard | null = null;
  private visible = false; private selected: string | null = null; private routeKey = '';
  private raf = 0; private off: (() => void)[] = [];
  /** The ground point under the camera's crosshair in an open camera view, when it is known exactly. */
  sightOverride: { id: string; lon: number; lat: number } | null = null;

  constructor(v: C.Viewer, store: DroneStore) {
    this.v = v; this.store = store;
    this.pills = v.scene.primitives.add(new C.BillboardCollection({ scene: v.scene }));
    this.lines = v.scene.primitives.add(new C.PolylineCollection());
    this.marks = v.scene.primitives.add(new C.BillboardCollection({ scene: v.scene }));
    this.routes = new C.CustomDataSource('drone-routes'); void v.dataSources.add(this.routes);
    this.off.push(v.scene.preUpdate.addEventListener(() => this.update()));
    this.off.push(store.subscribe(() => { this.drawRoutes(); this.drawMarks(); }));
  }

  setVisible(on: boolean) {
    if (on === this.visible) return; this.visible = on;
    this.pills.show = on; this.lines.show = on; this.routes.show = on; this.marks.show = on;
    for (const c of this.crafts.values()) if (c.model) c.model.show = on;
    cancelAnimationFrame(this.raf);
    // The scene renders on demand; keep frames coming while aircraft are moving.
    if (on) { let odd = false; const loop = () => { odd = !odd; if (odd && !this.v.isDestroyed()) this.v.scene.requestRender(); this.raf = requestAnimationFrame(loop); }; loop(); }
    this.drawRoutes(); this.drawMarks(); this.v.scene.requestRender();
  }
  setSelected(id: string | null) { this.selected = id; this.routeKey = ''; this.drawRoutes(); }

  destroy() {
    cancelAnimationFrame(this.raf); this.off.forEach(f => f());
    if (this.v.isDestroyed()) return;
    for (const c of this.crafts.values()) if (c.model) this.v.scene.primitives.remove(c.model);
    this.v.scene.primitives.remove(this.pills); this.v.scene.primitives.remove(this.lines); this.v.scene.primitives.remove(this.marks); this.v.dataSources.remove(this.routes, true);
  }

  private drawRoutes() {
    if (this.v.isDestroyed()) return;
    const key = `${this.store.routes}:${this.selected}:${this.visible}:${this.store.ids().map(i => this.store.state(i)?.operation?.alt_lower_m).join(',')}`; if (key === this.routeKey) return; this.routeKey = key;
    this.routes.entities.removeAll(); if (!this.visible) return;
    for (const id of this.store.ids()) {
      const path = this.store.missionPath(id); const on = id === this.selected;
      if (path && path.length >= 2) {
        const line = this.store.closedPath(id) ? [...path, path[0]] : path;
        this.routes.entities.add({ id: `route-${id}`, polyline: { positions: C.Cartesian3.fromDegreesArray(line.flat()), clampToGround: true, width: on ? 2 : 1.3,
          material: on ? new C.PolylineDashMaterialProperty({ color: C.Color.WHITE.withAlpha(0.75), dashLength: 18, gapColor: C.Color.TRANSPARENT }) : C.Color.WHITE.withAlpha(0.2) } });
      }
      // The airspace the coordinator reserved for this aircraft's task: area and altitude band above the ground.
      const op = this.store.state(id)?.operation; if (!op) continue;
      const fill = (on ? C.Color.fromCssColorString(ACCENT) : C.Color.WHITE).withAlpha(on ? 0.1 : 0.035);
      const outline = (on ? C.Color.fromCssColorString(ACCENT) : C.Color.WHITE).withAlpha(on ? 0.75 : 0.22);
      const band = { height: op.alt_lower_m, extrudedHeight: op.alt_upper_m, heightReference: C.HeightReference.RELATIVE_TO_GROUND, extrudedHeightReference: C.HeightReference.RELATIVE_TO_GROUND,
        material: fill, outline: true, outlineColor: outline };
      const g = op.geometry;
      if (g.type === 'circle') this.routes.entities.add({ id: `volume-${id}`, position: C.Cartesian3.fromDegrees(g.lon, g.lat), ellipse: { semiMajorAxis: g.radius_m, semiMinorAxis: g.radius_m, ...band } });
      else this.routes.entities.add({ id: `volume-${id}`, rectangle: { coordinates: C.Rectangle.fromDegrees(...g.bbox), ...band } });
    }
    this.v.scene.requestRender();
  }

  /** Field notes (newest 40), warm-ground targets and the launch pad, clamped to the ground. */
  private drawMarks() {
    if (this.v.isDestroyed()) return;
    const info = this.store.info; const hot = info.targets.filter(t => t.kind === 'hotspot');
    const key = `${this.store.notesVersion}:${info.area?.launch?.join(',')}:${hot.map(t => t.id + t.label).join(',')}:${this.visible}`;
    if (key === this.marksKey) return; this.marksKey = key;
    this.marks.removeAll(); this.pad = null; if (!this.visible) return;
    const ground = { heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY, scale: 0.5 };
    const launch = info.area?.launch;
    if (launch) this.pad = this.marks.add({ ...ground, position: C.Cartesian3.fromDegrees(launch[0], launch[1]), image: padImage() as unknown as string, id: { kind: 'launch' } });
    for (const t of hot) {
      const reading = t.label.match(/(≥?-?\d+) °C/)?.[1];
      this.marks.add({ ...ground, position: C.Cartesian3.fromDegrees(t.lon, t.lat), image: markImage('warm', `HOT ${reading ?? ''}°C`) as unknown as string,
        verticalOrigin: C.VerticalOrigin.BOTTOM, pixelOffset: new C.Cartesian2(0, -4), id: { kind: 'target', targetId: t.id } });
    }
    const notes: FieldNote[] = info.notes.slice(0, 40);
    for (const n of notes) this.marks.add({ ...ground, position: C.Cartesian3.fromDegrees(n.lon, n.lat), image: markImage(n.kind) as unknown as string, id: { kind: 'note', noteId: n.id } });
    this.v.scene.requestRender();
  }

  private update() {
    if (!this.visible || this.v.isDestroyed()) return;
    const scene = this.v.scene; const g = scene.globe; const ex = scene.verticalExaggeration || 1;
    const states = this.store.states(); const seen = new Set<string>();
    for (const s of states) { seen.add(s.id); this.place(s, g, ex); }
    for (const [id, c] of this.crafts) if (!seen.has(id)) {
      if (c.model) scene.primitives.remove(c.model); this.pills.remove(c.pill); this.lines.remove(c.tether); this.lines.remove(c.sight); this.lines.remove(c.link); this.crafts.delete(id);
    }
    this.links(states, g, ex);
  }

  /** Radio links: each airborne aircraft to the launch site, or, beyond direct range, through the nearest relay aircraft. */
  private links(states: DroneState[], g: C.Globe, ex: number) {
    const launch = this.store.info.area?.launch;
    const relays = states.filter(s => s.status === 'airborne' && s.mission.kind === 'relay');
    const padHeight = launch ? (g.getHeight(C.Cartographic.fromDegrees(launch[0], launch[1])) ?? 0) + 3 * ex : 0;
    const padPos = launch ? C.Cartesian3.fromDegrees(launch[0], launch[1], padHeight) : null;
    for (const s of states) {
      const c = this.crafts.get(s.id); if (!c) continue;
      const up = s.status === 'airborne' || s.status === 'stale';
      if (!up || !c.pos || !padPos || !launch) { c.link.show = false; continue; }
      const far = metres([s.lon, s.lat], launch) > RELAY_RANGE_M;
      const via = far && s.mission.kind !== 'relay' ? relays.map(r => ({ r, d: metres([s.lon, s.lat], [r.lon, r.lat]) })).sort((a, b) => a.d - b.d)[0]?.r : undefined;
      const other = via ? this.crafts.get(via.id)?.pos ?? padPos : padPos;
      const dbm = s.linkDbm;
      const colour = s.status === 'stale' || (Number.isFinite(dbm) && dbm < -92) ? LINK.poor : Number.isFinite(dbm) && dbm < -78 ? LINK.fair : LINK.good;
      c.link.show = true; c.link.positions = [c.pos, other];
      const u = c.link.material.uniforms as { color: C.Color; dashLength?: number };
      u.color = colour.withAlpha(s.id === this.selected ? 0.85 : 0.42);
    }
    if (this.pad) this.pad.show = Boolean(launch);
  }

  private place(s: DroneState, g: C.Globe, ex: number) {
    let c = this.crafts.get(s.id);
    if (!c || c.aircraft !== s.aircraft) {
      if (c?.model) this.v.scene.primitives.remove(c.model);
      c = c ?? {
        model: null, loading: false, aircraft: s.aircraft, ground: null, pillKey: '',
        pill: this.pills.add({ position: C.Cartesian3.ZERO, show: true, scale: 0.5, verticalOrigin: C.VerticalOrigin.BOTTOM, pixelOffset: new C.Cartesian2(0, -24), disableDepthTestDistance: Number.POSITIVE_INFINITY, id: { kind: 'drone', droneId: s.id } }),
        tether: this.lines.add({ width: 1, material: C.Material.fromType('Color', { color: C.Color.WHITE.withAlpha(0.4) }) }),
        sight: this.lines.add({ width: 1.6, show: false, material: C.Material.fromType('Color', { color: C.Color.WHITE.withAlpha(0.85) }) }),
        link: this.lines.add({ width: 1.2, show: false, material: C.Material.fromType('PolylineDash', { color: LINK.good.withAlpha(0.4), gapColor: C.Color.TRANSPARENT, dashLength: 12 }) }),
        pos: null,
      };
      c.model = null; c.aircraft = s.aircraft; this.crafts.set(s.id, c); this.load(s, c);
    }
    // Height of the drawn ground under the aircraft (exaggerated like the terrain), eased so LOD changes never jolt it.
    const carto = C.Cartographic.fromDegrees(s.lon, s.lat);
    // Never inside the drawn terrain (coarse tiles can sit above the true ground), never below the true ground.
    const rendered = g.getHeight(carto); const known = s.groundM !== null ? s.groundM * ex : null;
    const drawn = rendered !== undefined && known !== null ? Math.max(rendered, known) : rendered ?? known;
    if (drawn !== null) c.ground = c.ground === null ? drawn : c.ground + (drawn - c.ground) * 0.12;
    const base = c.ground ?? (s.groundM ?? 0) * ex;
    const pos = C.Cartesian3.fromDegrees(s.lon, s.lat, base + s.aglM); c.pos = pos;
    if (c.model) {
      const hpr = new C.HeadingPitchRoll((s.headingDeg - 90) * DEG, s.pitchDeg * DEG, s.rollDeg * DEG);
      C.Transforms.headingPitchRollToFixedFrame(pos, hpr, C.Ellipsoid.WGS84, undefined, c.model.modelMatrix);
      const on = s.id === this.selected;
      if (c.model.silhouetteSize !== (on ? 2.2 : 0)) { c.model.silhouetteSize = on ? 2.2 : 0; c.model.silhouetteColor = SILHOUETTE; }
      const alpha = s.status === 'stale' ? 0.55 : 1; if (c.model.color?.alpha !== alpha) c.model.color = C.Color.WHITE.withAlpha(alpha);
    }
    const selected = s.id === this.selected;
    const label = `${Math.round(ft(s.aglM) / 10) * 10 >= 1000 ? (Math.round(ft(s.aglM) / 10) * 10).toLocaleString('en') : Math.round(ft(s.aglM) / 10) * 10} ft`;
    const key = `${s.callsign}|${label}|${selected}|${s.source}|${s.status}`;
    if (key !== c.pillKey) { c.pill.image = pillImage(s.callsign, label, selected, s.source, s.status === 'stale') as unknown as string; c.pillKey = key; }
    c.pill.position = pos;
    c.tether.positions = [pos, C.Cartesian3.fromDegrees(s.lon, s.lat, base)];
    (c.tether.material.uniforms as { color: C.Color }).color.alpha = selected ? 0.7 : 0.4;
    // Line of sight to the camera's aim point, for the selected aircraft.
    const o = this.sightOverride?.id === s.id ? [this.sightOverride.lon, this.sightOverride.lat] as [number, number] : this.store.sightPoint(s);
    c.sight.show = selected && Boolean(o);
    if (selected && o) {
      const h = g.getHeight(C.Cartographic.fromDegrees(o[0], o[1])) ?? base;
      c.sight.positions = [pos, C.Cartesian3.fromDegrees(o[0], o[1], h)];
    }
  }

  private load(s: DroneState, c: Craft) {
    if (c.loading) return; c.loading = true; const aircraft = s.aircraft;
    // Cesium accepts the glTF JSON itself (with an embedded buffer); its type definitions only list URLs.
    const options = { gltf: aircraftGltf(aircraft), minimumPixelSize: aircraft === 'quad' ? 46 : 58, maximumScale: 900,
      id: { kind: 'drone', droneId: s.id }, shadows: C.ShadowMode.DISABLED, show: this.visible } as unknown as Parameters<typeof C.Model.fromGltfAsync>[0];
    void C.Model.fromGltfAsync(options).then(m => {
      c.loading = false;
      if (this.v.isDestroyed() || c.aircraft !== aircraft || !this.crafts.has(s.id)) { m.destroy(); return; }
      this.v.scene.primitives.add(m); c.model = m; this.v.scene.requestRender();
    }).catch(() => { c.loading = false; });
  }
}
