import * as C from 'cesium';
import { aircraftGltf } from './droneModel';
import { ft, type DroneState, type DroneStore } from './drones';

type Craft = { model: C.Model | null; loading: boolean; aircraft: string; pill: C.Billboard; tether: C.Polyline; sight: C.Polyline; ground: number | null; pillKey: string };
const DEG = Math.PI / 180;
const ACCENT = '#e5622d';
const SILHOUETTE = C.Color.fromCssColorString(ACCENT);

/** The label tag above each aircraft: a status light, the call sign and the height above ground. */
function pillImage(callsign: string, height: string, selected: boolean, source: 'virtual' | 'real', stale: boolean): HTMLCanvasElement {
  const k = 2; const c = document.createElement('canvas'); const x = c.getContext('2d')!;
  const bold = `600 ${12 * k}px "Archivo Variable", Arial, sans-serif`, light = `500 ${12 * k}px "Archivo Variable", Arial, sans-serif`;
  x.font = bold; const w1 = x.measureText(callsign).width; x.font = light; const w2 = x.measureText(height).width;
  const tag = source === 'real' ? 'LIVE' : 'SIM'; x.font = `600 ${9 * k}px "IBM Plex Mono", monospace`; const w3 = x.measureText(tag).width;
  const pad = 8 * k, dot = 7 * k, gap = 6 * k, h = 23 * k;
  c.width = Math.ceil(pad + dot + gap + w1 + gap + w2 + gap + w3 + pad); c.height = h + 2 * k;
  x.beginPath(); x.roundRect(k, k, c.width - 2 * k, h, h / 2);
  x.fillStyle = selected ? ACCENT : 'rgba(21, 21, 18, 0.9)'; x.fill();
  x.lineWidth = k; x.strokeStyle = selected ? 'rgba(255, 220, 200, 0.55)' : 'rgba(255, 255, 255, 0.16)'; x.stroke();
  x.beginPath(); x.arc(pad + dot / 2, k + h / 2, dot / 2, 0, Math.PI * 2); x.fillStyle = stale ? '#e8b04a' : '#7be36b'; x.fill();
  x.textBaseline = 'middle'; let at = pad + dot + gap;
  x.font = bold; x.fillStyle = '#fbf8f1'; x.fillText(callsign, at, k + h / 2 + k); at += w1 + gap;
  x.font = light; x.fillStyle = selected ? 'rgba(255, 245, 238, 0.85)' : 'rgba(236, 233, 223, 0.7)'; x.fillText(height, at, k + h / 2 + k); at += w2 + gap;
  x.font = `600 ${9 * k}px "IBM Plex Mono", monospace`; x.fillStyle = selected ? 'rgba(255, 245, 238, 0.7)' : source === 'real' ? '#9fe07a' : 'rgba(236, 233, 223, 0.45)'; x.fillText(tag, at, k + h / 2 + k);
  return c;
}

/**
 * Draws the fleet on the main globe: a 3D model per aircraft (banking and pitching as it flies), a label tag,
 * a thin line to the ground, the camera's line of sight for the selected aircraft and each mission's route.
 */
export class DroneLayer {
  private v: C.Viewer; private store: DroneStore;
  private crafts = new Map<string, Craft>();
  private pills: C.BillboardCollection; private lines: C.PolylineCollection; private routes: C.CustomDataSource;
  private visible = false; private selected: string | null = null; private routeKey = '';
  private raf = 0; private off: (() => void)[] = [];
  /** The ground point under the camera's crosshair in an open camera view, when it is known exactly. */
  sightOverride: { id: string; lon: number; lat: number } | null = null;

  constructor(v: C.Viewer, store: DroneStore) {
    this.v = v; this.store = store;
    this.pills = v.scene.primitives.add(new C.BillboardCollection({ scene: v.scene }));
    this.lines = v.scene.primitives.add(new C.PolylineCollection());
    this.routes = new C.CustomDataSource('drone-routes'); void v.dataSources.add(this.routes);
    this.off.push(v.scene.preUpdate.addEventListener(() => this.update()));
    this.off.push(store.subscribe(() => this.drawRoutes()));
  }

  setVisible(on: boolean) {
    if (on === this.visible) return; this.visible = on;
    this.pills.show = on; this.lines.show = on; this.routes.show = on;
    for (const c of this.crafts.values()) if (c.model) c.model.show = on;
    cancelAnimationFrame(this.raf);
    // The scene renders on demand; keep frames coming while aircraft are moving.
    if (on) { let odd = false; const loop = () => { odd = !odd; if (odd && !this.v.isDestroyed()) this.v.scene.requestRender(); this.raf = requestAnimationFrame(loop); }; loop(); }
    this.drawRoutes(); this.v.scene.requestRender();
  }
  setSelected(id: string | null) { this.selected = id; this.routeKey = ''; this.drawRoutes(); }

  destroy() {
    cancelAnimationFrame(this.raf); this.off.forEach(f => f());
    if (this.v.isDestroyed()) return;
    for (const c of this.crafts.values()) if (c.model) this.v.scene.primitives.remove(c.model);
    this.v.scene.primitives.remove(this.pills); this.v.scene.primitives.remove(this.lines); this.v.dataSources.remove(this.routes, true);
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

  private update() {
    if (!this.visible || this.v.isDestroyed()) return;
    const scene = this.v.scene; const g = scene.globe; const ex = scene.verticalExaggeration || 1;
    const states = this.store.states(); const seen = new Set<string>();
    for (const s of states) { seen.add(s.id); this.place(s, g, ex); }
    for (const [id, c] of this.crafts) if (!seen.has(id)) {
      if (c.model) scene.primitives.remove(c.model); this.pills.remove(c.pill); this.lines.remove(c.tether); this.lines.remove(c.sight); this.crafts.delete(id);
    }
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
    const pos = C.Cartesian3.fromDegrees(s.lon, s.lat, base + s.aglM);
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
