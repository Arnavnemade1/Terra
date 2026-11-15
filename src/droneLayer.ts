import * as C from 'cesium';
import { aircraftGltf, propellers } from './droneModel';
import type { GroundHeights } from './groundHeights';
import { ft, type DroneState, type DroneStore, type FieldNote, type Source } from './drones';

type Craft = { props: { node: C.ModelNode; sign: number }[]; spinAt: number; model: C.Model | null; loading: boolean; aircraft: string; pill: C.Billboard; tether: C.Polyline; sight: C.Polyline; link: C.Polyline; ground: number | null; pillKey: string; pos: C.Cartesian3 | null;
  strobe: C.Billboard; beacon: C.Billboard; ring: C.Billboard; trail: C.Polyline[]; trailPts: C.Cartesian3[]; trailAt: number; rendered: number | undefined; renderedAt: number; phase: number; spin: number };
const DEG = Math.PI / 180;
const ACCENT = '#e5622d';
const SILHOUETTE = C.Color.fromCssColorString(ACCENT);
const RELAY_RANGE_M = 6000;  // as the coordinator plans: beyond this from launch, traffic goes through a relay
const LINK = { good: C.Color.fromCssColorString('#7be36b'), fair: C.Color.fromCssColorString('#e8b04a'), poor: C.Color.fromCssColorString('#ef5b4c') };
const NOTE_COLOR: Record<string, string> = { warm: '#ff6a3d', change: '#e8b04a', station: '#f4f1e8', observation: '#cfc9bb', verify: '#7be36b' };
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

/** A soft round glow (strobe, beacon). */
function glowImage(rgb: string): HTMLCanvasElement {
  const c = document.createElement('canvas'); c.width = c.height = 64; const x = c.getContext('2d')!;
  const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, `rgba(${rgb}, 1)`); g.addColorStop(0.18, `rgba(${rgb}, 0.95)`); g.addColorStop(0.45, `rgba(${rgb}, 0.28)`); g.addColorStop(1, `rgba(${rgb}, 0)`);
  x.fillStyle = g; x.fillRect(0, 0, 64, 64); return c;
}
/** The mark on the ground under an aircraft: a thin ring around a dot. */
function ringImage(): HTMLCanvasElement {
  const k = 2; const c = document.createElement('canvas'); c.width = c.height = 28 * k; const x = c.getContext('2d')!;
  x.beginPath(); x.arc(14 * k, 14 * k, 10 * k, 0, Math.PI * 2); x.lineWidth = 1.6 * k; x.strokeStyle = 'rgba(255, 255, 255, 0.85)'; x.stroke();
  x.beginPath(); x.arc(14 * k, 14 * k, 2.2 * k, 0, Math.PI * 2); x.fillStyle = 'rgba(255, 255, 255, 0.9)'; x.fill();
  return c;
}
let sprites: { strobe: HTMLCanvasElement; beacon: HTMLCanvasElement; ring: HTMLCanvasElement } | null = null;
const sprite = () => (sprites ??= { strobe: glowImage('255, 255, 255'), beacon: glowImage('255, 64, 48'), ring: ringImage() });
const TRAIL_S = 20, TRAIL_EVERY_MS = 400, TRAIL_ALPHA = [0.42, 0.26, 0.15, 0.07];

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
  /** Strobes, beacons and ground rings (depth-tested, so terrain hides them like it hides the aircraft). */
  private glows: C.BillboardCollection;
  /** Cached ground heights (set by the globe); never ray-cast the terrain per aircraft per frame. */
  ground: GroundHeights | null = null;
  /** Whether an airborne aircraft is on screen: the scene then renders continuously, otherwise twice a second. */
  private onScreen = false;
  private visible = false; private selected: string | null = null; private routeKey = '';
  private raf = 0; private off: (() => void)[] = [];
  /** Flying with an aircraft: the main camera chases it every frame (see follow()). */
  private chase: { id: string; h: number; p: number; r: number; rel: number; set: [number, number, number] | null; last: number } | null = null;
  onFollowEnd: (() => void) | null = null;
  /** The ground point under the camera's crosshair in an open camera view, when it is known exactly. */
  sightOverride: { id: string; lon: number; lat: number } | null = null;

  constructor(v: C.Viewer, store: DroneStore) {
    this.v = v; this.store = store;
    this.pills = v.scene.primitives.add(new C.BillboardCollection({ scene: v.scene }));
    this.lines = v.scene.primitives.add(new C.PolylineCollection());
    this.marks = v.scene.primitives.add(new C.BillboardCollection({ scene: v.scene }));
    this.glows = v.scene.primitives.add(new C.BillboardCollection({ scene: v.scene }));
    this.routes = new C.CustomDataSource('drone-routes'); void v.dataSources.add(this.routes);
    this.off.push(v.scene.preUpdate.addEventListener(() => this.update()));
    this.off.push(store.subscribe(() => { this.drawRoutes(); this.drawMarks(); }));
  }

  setVisible(on: boolean) {
    if (on === this.visible) return; this.visible = on;
    this.pills.show = on; this.lines.show = on; this.routes.show = on; this.marks.show = on; this.glows.show = on;
    for (const c of this.crafts.values()) if (c.model) c.model.show = on;
    cancelAnimationFrame(this.raf);
    // The scene renders on demand. While an airborne aircraft is on screen (or the camera flies with one) keep frames
    // coming for smooth motion; otherwise twice a second is enough to notice one flying into view.
    if (on) { let odd = false, last = 0; const loop = () => {
      odd = !odd; const now = performance.now();
      if (!this.v.isDestroyed() && ((this.chase || (this.onScreen && odd)) || now - last > 500)) { last = now; this.v.scene.requestRender(); }
      this.raf = requestAnimationFrame(loop); }; loop(); }
    this.drawRoutes(); this.drawMarks(); this.v.scene.requestRender();
  }
  setSelected(id: string | null) { this.selected = id; this.routeKey = ''; this.drawRoutes(); }

  destroy() {
    cancelAnimationFrame(this.raf); this.off.forEach(f => f());
    if (this.v.isDestroyed()) return;
    for (const c of this.crafts.values()) if (c.model) this.v.scene.primitives.remove(c.model);
    this.v.scene.primitives.remove(this.pills); this.v.scene.primitives.remove(this.lines); this.v.scene.primitives.remove(this.marks); this.v.scene.primitives.remove(this.glows); this.v.dataSources.remove(this.routes, true);
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
      if (c.model) scene.primitives.remove(c.model); this.pills.remove(c.pill); this.lines.remove(c.tether); this.lines.remove(c.sight); this.lines.remove(c.link);
      this.glows.remove(c.strobe); this.glows.remove(c.beacon); this.glows.remove(c.ring); c.trail.forEach(t => this.lines.remove(t)); this.crafts.delete(id);
    }
    // Is any airborne aircraft on screen? (Cheap: project each drawn position to the window.)
    const w = this.v.canvas.clientWidth, hgt = this.v.canvas.clientHeight; let any = false;
    for (const s of states) {
      const c = this.crafts.get(s.id); if (!c?.pos || s.status === 'landed') continue;
      const xy = C.SceneTransforms.worldToWindowCoordinates(scene, c.pos);
      if (xy && xy.x > -50 && xy.y > -50 && xy.x < w + 50 && xy.y < hgt + 50) { any = true; break; }
    }
    this.onScreen = any;
    this.links(states, g, ex);
    this.chaseCamera(scene);
    this.declutterLabels(states);
  }

  /** Keep nearby callsigns readable while retaining every aircraft's map marker. */
  private declutterLabels(states: DroneState[]) {
    const occupied: { x: number; y: number; w: number; h: number }[] = [];
    const priority = (s: DroneState) => s.id === this.selected ? 0 : s.status === 'airborne' ? 1 : 2;
    for (const s of [...states].sort((a, b) => priority(a) - priority(b) || a.id.localeCompare(b.id))) {
      const craft = this.crafts.get(s.id); if (!craft?.pos) continue;
      const p = C.SceneTransforms.worldToWindowCoordinates(this.v.scene, craft.pos);
      if (!p || p.x < 0 || p.y < 0 || p.x > this.v.canvas.clientWidth || p.y > this.v.canvas.clientHeight) { craft.pill.show = false; continue; }
      const w = (craft.pill.width ?? 320) * craft.pill.scale, h = (craft.pill.height ?? 56) * craft.pill.scale;
      const r = { x: p.x - w / 2 - 4, y: p.y - 24 - h - 4, w: w + 8, h: h + 8 };
      const overlaps = occupied.some(b => r.x < b.x + b.w && r.x + r.w > b.x && r.y < b.y + b.h && r.y + r.h > b.y);
      craft.pill.show = !overlaps;
      if (!overlaps) occupied.push(r);
    }
  }

  /**
   * Fly with an aircraft: the camera sits behind it and follows its turns smoothly, from the aircraft's drawn
   * position each frame. Dragging orbits around it (the chosen angle is kept relative to its heading), the wheel
   * zooms and tilting is kept; it never goes below the terrain. null stops following.
   */
  follow(id: string | null) {
    const cam = this.v.camera;
    if (!id) { if (this.chase) { this.chase = null; cam.lookAtTransform(C.Matrix4.IDENTITY); } return; }
    const s = this.store.state(id); if (!s) return;
    const c = this.crafts.get(id);
    // Start from wherever the camera is now, measured from the aircraft, and ease into the chase position.
    let h = s.headingDeg * DEG, p = -0.35, r = 2500;
    if (c?.pos) {
      const enu = C.Transforms.eastNorthUpToFixedFrame(c.pos); const inv = C.Matrix4.inverseTransformation(enu, new C.Matrix4());
      const v = C.Matrix4.multiplyByPoint(inv, cam.positionWC, new C.Cartesian3()); r = Math.max(60, C.Cartesian3.magnitude(v));
      h = Math.atan2(-v.x, -v.y); p = -Math.asin(Math.min(1, Math.max(-1, v.z / r)));
    }
    this.chase = { id, h, p, r, rel: 0, set: null, last: performance.now() };
    this.v.scene.requestRender();
  }
  following() { return this.chase?.id ?? null; }

  private chaseCamera(scene: C.Scene) {
    const f = this.chase; if (!f) return;
    const s = this.store.state(f.id); const c = this.crafts.get(f.id);
    if (!s || !c?.pos) return;
    const cam = this.v.camera; const now = performance.now(); const dt = Math.min(0.1, (now - f.last) / 1000); f.last = now;
    // What the person did since the last frame (orbit, tilt, zoom), measured in the aircraft's local frame.
    if (f.set) {
      const v = cam.position; const r = C.Cartesian3.magnitude(v);
      if (r > 1) {
        const h = Math.atan2(-v.x, -v.y), p = -Math.asin(Math.min(1, Math.max(-1, v.z / r)));
        const dh = Math.atan2(Math.sin(h - f.set[0]), Math.cos(h - f.set[0]));
        if (Math.abs(dh) > 0.002) f.rel += dh;
        if (Math.abs(p - f.set[1]) > 0.002) f.p = p;
        if (Math.abs(r - f.set[2]) > 0.5) f.r = r;
      }
    }
    // Behind the aircraft (plus any angle chosen by dragging), a little above, at a range that suits its height.
    const target = { h: s.headingDeg * DEG + f.rel, p: f.set ? f.p : -0.3, r: f.set ? f.r : Math.min(1200, Math.max(220, s.aglM * 1.6 + 160)) };
    const k = 1 - Math.exp(-dt / 0.6);
    f.h += Math.atan2(Math.sin(target.h - f.h), Math.cos(target.h - f.h)) * k;
    if (!f.set) { f.p += (target.p - f.p) * k; f.r = Math.exp(Math.log(f.r) + (Math.log(target.r) - Math.log(f.r)) * k); if (Math.abs(Math.log(f.r / target.r)) < 0.02) f.set = [f.h, f.p, f.r]; }
    else { f.p = target.p; f.r = target.r; }
    // Keep the camera above the drawn ground.
    let pitch = f.p;
    for (let i = 0; i < 12; i++) {
      const offset = new C.HeadingPitchRange(f.h, pitch, f.r);
      cam.lookAt(c.pos, offset);
      const at = cam.positionCartographic; const ground = this.groundAt(C.Math.toDegrees(at.longitude), C.Math.toDegrees(at.latitude), scene.globe, scene.verticalExaggeration || 1);
      if (ground === undefined || at.height > ground + 25) break;
      pitch = Math.max(-1.45, pitch - 0.08);
    }
    if (f.set) f.set = [f.h, pitch, f.r];
    scene.requestRender();
  }

  /** Radio links: each airborne aircraft to the launch site, or, beyond direct range, through the nearest relay aircraft. */
  private links(states: DroneState[], g: C.Globe, ex: number) {
    const launch = this.store.info.area?.launch;
    const relays = states.filter(s => s.status === 'airborne' && s.mission.kind === 'relay');
    const padGround = launch ? this.groundAt(launch[0], launch[1], g, ex) : undefined;
    const padHeight = launch ? (padGround ?? 0) + 3 * ex : 0;
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

  /** Drawn ground height (exaggerated like the terrain) from the cache; ray-casts the globe only before the first samples arrive. */
  private groundAt(lon: number, lat: number, g: C.Globe, ex: number): number | undefined {
    const h = this.ground?.get(lon, lat); if (h !== undefined) return h * ex;
    return g.show ? g.getHeight(C.Cartographic.fromDegrees(lon, lat)) : undefined;
  }

  private place(s: DroneState, g: C.Globe, ex: number) {
    let c = this.crafts.get(s.id);
    if (!c || c.aircraft !== s.aircraft) {
      if (c?.model) this.v.scene.primitives.remove(c.model);
      const glow = { position: C.Cartesian3.ZERO, show: false, eyeOffset: new C.Cartesian3(0, 0, -6) };
      c = c ?? {
        props: [], spinAt: performance.now(),
        strobe: this.glows.add({ ...glow, image: sprite().strobe as unknown as string, scale: 0.42 }),
        beacon: this.glows.add({ ...glow, image: sprite().beacon as unknown as string, scale: 0.3, pixelOffset: new C.Cartesian2(0, -7) }),
        ring: this.glows.add({ ...glow, image: sprite().ring as unknown as string, scale: 0.5, eyeOffset: new C.Cartesian3(0, 0, -25) }),
        trail: TRAIL_ALPHA.map(a => this.lines.add({ width: 2, show: false, material: C.Material.fromType('Color', { color: C.Color.WHITE.withAlpha(a) }) })),
        trailPts: [], trailAt: 0, rendered: undefined, renderedAt: 0, phase: Math.random() * 1.4, spin: 0,
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
    // From the height cache (true ground, or the photoreal tiles while they are drawn); the drawn terrain is checked at
    // most every 300 ms so an aircraft never sits inside coarse tiles, and never ray-cast per frame.
    const now = performance.now();
    const cached = this.ground?.get(s.lon, s.lat);
    const known = cached !== undefined ? cached * ex : s.groundM !== null ? s.groundM * ex : undefined;
    if (g.show && now - c.renderedAt > 300) { c.renderedAt = now; c.rendered = g.getHeight(C.Cartographic.fromDegrees(s.lon, s.lat)); }
    const rendered = g.show ? c.rendered : undefined;
    const drawn = rendered !== undefined && known !== undefined ? Math.max(rendered, known) : rendered ?? known;
    if (drawn !== undefined) c.ground = c.ground === null ? drawn : c.ground + (drawn - c.ground) * 0.12;
    // Until the ground is known, draw nothing (rather than an aircraft at sea level inside a mountain).
    const grounded = c.ground !== null; const show = this.visible && grounded;
    if (c.model && c.model.show !== show) c.model.show = show;
    c.pill.show = show; c.tether.show = show;
    if (!grounded) { c.pos = null; c.strobe.show = c.beacon.show = c.ring.show = false; c.trail.forEach(t => t.show = false); c.sight.show = false; return; }
    const base = c.ground!;
    const pos = C.Cartesian3.fromDegrees(s.lon, s.lat, base + s.aglM); c.pos = pos;
    const airborne = s.status !== 'landed';
    // Anti-collision strobe (a white double flash) and a red beacon, as on real aircraft; a ring on the ground beneath.
    const ts = (now / 1000 + c.phase) % 1.4;
    c.strobe.show = show && airborne && (ts < 0.05 || (ts > 0.16 && ts < 0.21)); c.strobe.position = pos;
    c.beacon.show = show && airborne && ((now / 1000 + c.phase * 0.7) % 1.0) < 0.14; c.beacon.position = pos;
    c.ring.show = show && airborne; c.ring.position = C.Cartesian3.fromDegrees(s.lon, s.lat, base + 1.5);
    c.ring.color = s.id === this.selected ? C.Color.fromCssColorString(ACCENT) : C.Color.WHITE.withAlpha(0.8);
    // A fading trail of the last 20 s, newest segment brightest.
    if (airborne && now - c.trailAt > TRAIL_EVERY_MS) { c.trailAt = now; c.trailPts.push(pos); if (c.trailPts.length > TRAIL_S * 1000 / TRAIL_EVERY_MS) c.trailPts.shift(); }
    if (!airborne) c.trailPts = [];
    const pts = c.trailPts; const per = Math.ceil(pts.length / c.trail.length); const tint = s.id === this.selected ? C.Color.fromCssColorString(ACCENT) : C.Color.WHITE;
    c.trail.forEach((line, k) => {
      const end = pts.length - k * per, start = Math.max(0, end - per - 1);
      const seg = k === 0 ? [...pts.slice(start, end), pos] : pts.slice(start, end);
      line.show = show && seg.length >= 2; if (line.show) line.positions = seg;
      (line.material.uniforms as { color: C.Color }).color = tint.withAlpha(TRAIL_ALPHA[k]);
    });
    // Spinning propellers (alternate directions on a quad); a readable speed rather than a strobing one.
    if (c.props.length) {
      const dt = Math.min(0.1, (now - c.spinAt) / 1000); c.spinAt = now; c.spin += dt * (airborne ? 11 : 0);
      const axis = propellers(s.aircraft).axis;
      for (const p of c.props) {
        const r = axis === 'y' ? C.Matrix3.fromRotationY(c.spin * p.sign) : C.Matrix3.fromRotationZ(c.spin * p.sign);
        p.node.matrix = C.Matrix4.multiply(p.node.originalMatrix, C.Matrix4.fromRotationTranslation(r, C.Cartesian3.ZERO), new C.Matrix4());
      }
    }
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
    if (key !== c.pillKey) {
      const image = pillImage(s.callsign, label, selected, s.source, s.status === 'stale');
      c.pill.image = image as unknown as string; c.pill.width = image.width; c.pill.height = image.height; c.pillKey = key;
    }
    c.pill.position = pos;
    c.tether.positions = [pos, C.Cartesian3.fromDegrees(s.lon, s.lat, base)];
    (c.tether.material.uniforms as { color: C.Color }).color.alpha = selected ? 0.7 : 0.4;
    // Line of sight to the camera's aim point, for the selected aircraft.
    const o = this.sightOverride?.id === s.id ? [this.sightOverride.lon, this.sightOverride.lat] as [number, number] : this.store.sightPoint(s);
    c.sight.show = selected && Boolean(o);
    if (selected && o) {
      const h = this.groundAt(o[0], o[1], g, ex) ?? base;
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
      const spin = () => { const { names } = propellers(aircraft); c.props = names.map((n, i) => ({ node: m.getNode(n), sign: i === 1 || i === 2 ? -1 : 1 })).filter(p => p.node); };
      if (m.ready) spin(); else m.readyEvent.addEventListener(spin);
    }).catch(() => { c.loading = false; });
  }
}
