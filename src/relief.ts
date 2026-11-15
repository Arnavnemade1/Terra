import * as C from 'cesium';
import { imagery } from './tiles';
import type { Bounds } from './types';

export type LandCover = {
  bounds: Bounds; width: number; height: number; grid_png: string; resolution_m: number; source: string; source_url: string;
  classes: Record<string, { name: string; color: string }>; shares: { code: number; name: string; pct: number }[];
};
export type TerrainFeature = { id: string; name: string; kind: 'peak' | 'volcano' | 'lake' | 'glacier' | 'place'; lon: number; lat: number; elevation_m: number | null; population: number | null; source_url: string };
export type TerrainContext = { bbox: Bounds; landcover: LandCover | null; features: TerrainFeature[]; unavailable: string[] };
export type Grid = { codes: Uint8Array; width: number; height: number; bounds: Bounds; classes: LandCover['classes'] };
export type ReliefOptions = { contours: boolean; cover: boolean; trees: boolean; features: boolean; roads: boolean };
export type TerrainReading = { elevation: number | null; slope: number | null; facing: string | null; cover: string | null };

const MAPS = '/api/connector/maps';
const TREE = new Set([10, 95]);
const MAX_PLANTS = 3200;

/** Decode the land-cover grid (a greyscale PNG whose values are WorldCover class codes). */
export async function decodeGrid(lc: LandCover): Promise<Grid> {
  const img = new Image(); img.src = `data:image/png;base64,${lc.grid_png}`; await img.decode();
  const canvas = document.createElement('canvas'); canvas.width = lc.width; canvas.height = lc.height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!; ctx.drawImage(img, 0, 0);
  const rgba = ctx.getImageData(0, 0, lc.width, lc.height).data; const codes = new Uint8Array(lc.width * lc.height);
  for (let i = 0; i < codes.length; i++) codes[i] = rgba[i * 4];
  return { codes, width: lc.width, height: lc.height, bounds: lc.bounds, classes: lc.classes };
}

export function classAt(g: Grid | null, lon: number, lat: number): number {
  if (!g) return 0; const [w, s, e, n] = g.bounds;
  if (lon < w || lon > e || lat < s || lat > n) return 0;
  const x = Math.min(g.width - 1, Math.floor((lon - w) / (e - w) * g.width)), y = Math.min(g.height - 1, Math.floor((n - lat) / (n - s) * g.height));
  return g.codes[y * g.width + x];
}

/** The land-cover tint draped on the relief: muted colours, trees strongest, unknown cells transparent. */
function tintImage(g: Grid): string {
  const canvas = document.createElement('canvas'); canvas.width = g.width; canvas.height = g.height;
  const ctx = canvas.getContext('2d')!; const img = ctx.createImageData(g.width, g.height);
  const rgb: Record<number, [number, number, number, number]> = {};
  for (const [code, c] of Object.entries(g.classes)) {
    const v = c.color.match(/\w\w/g)!.map(h => parseInt(h, 16));
    const k = Number(code); rgb[k] = [v[0], v[1], v[2], k === 10 || k === 95 ? 200 : k === 80 ? 230 : k === 50 ? 170 : k === 70 ? 210 : 120];
  }
  for (let i = 0; i < g.codes.length; i++) { const c = rgb[g.codes[i]]; if (!c) continue; img.data.set(c, i * 4); }
  ctx.putImageData(img, 0, 0); return canvas.toDataURL('image/png');
}

/** Small map symbols drawn once on a canvas: summit and volcano triangles, a settlement dot. */
function symbol(kind: 'peak' | 'volcano' | 'place'): HTMLCanvasElement {
  const c = document.createElement('canvas'); c.width = 18; c.height = 16; const x = c.getContext('2d')!;
  x.lineJoin = 'round'; x.lineWidth = 2.5; x.strokeStyle = 'rgba(14,13,10,0.85)';
  if (kind === 'place') { x.beginPath(); x.arc(9, 8, 4, 0, Math.PI * 2); x.fillStyle = '#f4efe2'; x.stroke(); x.fill(); return c; }
  x.beginPath(); x.moveTo(9, 2.5); x.lineTo(16, 14); x.lineTo(2, 14); x.closePath(); x.stroke();
  x.fillStyle = kind === 'volcano' ? '#ff7a45' : '#f8f4ea'; x.fill(); return c;
}

const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const hash = (a: number, b: number, c = 0) => { let h = (a * 374761393 + b * 668265263 + c * 2147483647) | 0; h = (h ^ (h >>> 13)) * 1274126177; return ((h ^ (h >>> 16)) >>> 0) / 4294967296; };

/**
 * The stylised relief map: a muted terrain base, a land-cover tint, faceted shading and contours computed on the
 * GPU from the terrain itself, low-poly trees and shrubs where WorldCover says they grow, and named summits and towns.
 */
export class Relief {
  private v: C.Viewer;
  private base: C.ImageryLayer; private roads: C.ImageryLayer; private cover: C.ImageryLayer | null = null; private planet: C.ImageryLayer;
  private far = -1; private sunNow = false;
  private material: C.Material;
  private plants: C.Primitive | null = null; private pending: C.Primitive | null = null;
  private labels: C.EntityCollection;
  private grid: Grid | null = null; private features: TerrainFeature[] = [];
  private active = false; private opts: ReliefOptions = { contours: true, cover: true, trees: true, features: true, roads: true };
  private timer: ReturnType<typeof setTimeout> | undefined; private plantKey = ''; private plantHeights: number[] = []; plantings = 0; private settle: ReturnType<typeof setTimeout> | undefined;
  private sun: C.Light; private lamp: C.DirectionalLight | null = null; private lighting = false;
  private ground: C.Color; private hidden: C.ImageryLayer[] = [];
  private removers: (() => void)[] = [];

  /** `after` is the photographic base layer; `hide` lists layers the relief style replaces (photo base, fallback). */
  constructor(v: C.Viewer, after: C.ImageryLayer, hide: C.ImageryLayer[] = []) {
    this.v = v; this.sun = v.scene.light; this.ground = v.scene.globe.baseColor.clone(); this.hidden = hide;
    const at = v.imageryLayers.indexOf(after) + 1;
    // A sand-coloured globe shaded by Esri's global hillshade; land cover and GPU shading are layered on top.
    this.base = new C.ImageryLayer(new C.UrlTemplateImageryProvider({ ...imagery('maps/hillshade/{z}/{y}/{x}'), tilingScheme: new C.WebMercatorTilingScheme(), maximumLevel: 16, credit: new C.Credit('Esri World Hillshade') }));
    this.base.show = false; this.base.alpha = 0.5; this.base.brightness = 1.08; v.imageryLayers.add(this.base, at);
    this.roads = new C.ImageryLayer(new C.UrlTemplateImageryProvider({ ...imagery('maps/transport/{z}/{y}/{x}'), tilingScheme: new C.WebMercatorTilingScheme(), maximumLevel: 18, credit: new C.Credit('Esri World Transportation') }));
    // Roads as thin pale lines, as on a topographic sheet, rather than Esri's coloured road classes.
    this.roads.show = false; this.roads.alpha = 0.6; this.roads.saturation = 0; this.roads.brightness = 1.6; v.imageryLayers.add(this.roads, at + 1);
    // From far out Esri's hillshade is blank (its lowest levels are nearly white) and the faceted GPU shading turns into
    // blocks, so NASA's Blue Marble shaded relief and bathymetry takes over as the camera leaves the region (retune).
    this.planet = new C.ImageryLayer(new C.UrlTemplateImageryProvider({ ...imagery('gibs/BlueMarble_ShadedRelief_Bathymetry/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpg'),
      maximumLevel: 8, credit: new C.Credit('NASA GIBS · Blue Marble shaded relief') }));
    this.planet.show = false; this.planet.alpha = 0; this.planet.saturation = 0.82; this.planet.contrast = 1.06; v.imageryLayers.add(this.planet, at + 1);
    this.labels = new C.EntityCollection(); v.dataSources.add(new C.CustomDataSource('relief-labels')).then(ds => { if (this.v.isDestroyed()) return; this.labels = ds.entities; this.drawFeatures(); });
    this.material = new C.Material({
      fabric: {
        type: 'TerraRelief',
        uniforms: { interval: 50, contourAlpha: 0.32, detail: 1 },
        source: `
          czm_material czm_getMaterial(czm_materialInput materialInput) {
            czm_material m = czm_getDefaultMaterial(materialInput);
            vec3 p = -materialInput.positionToEyeEC;
            // Faceted normals from screen-space derivatives give the low-poly relief look on any terrain mesh.
            vec3 n = normalize(cross(dFdx(p), dFdy(p)));
            // The local frame comes from the ellipsoid at the fragment's world position. (Cesium's surface-normal
            // inputs are not usable without stored terrain normals, which ArcGIS heightmaps do not have.)
            vec3 world = (czm_inverseView * vec4(p, 1.0)).xyz;
            vec3 upW = normalize(world * czm_ellipsoidInverseRadii * czm_ellipsoidInverseRadii);
            vec3 eastW = normalize(cross(vec3(0.0, 0.0, 1.0), upW));
            vec3 northW = cross(upW, eastW);
            vec3 up = normalize(czm_viewRotation * upW);
            if (dot(n, up) < 0.0) n = -n;
            // Light from the north-west, 40 degrees up: the cartographic convention for shaded relief.
            vec3 sun = normalize(czm_viewRotation * (eastW * -0.54 + northW * 0.54 + upW * 0.64));
            const float shadeStrength = 1.7;
            float shade = dot(n, sun) - dot(up, sun);
            vec3 colour; float alpha;
            if (shade < 0.0) { colour = vec3(0.10, 0.075, 0.05); alpha = clamp(-shade * shadeStrength, 0.0, 0.72); }
            else { colour = vec3(1.0, 0.96, 0.86); alpha = clamp(shade * shadeStrength * 0.55, 0.0, 0.3); }
            // Contours every interval metres; every fifth line is an index contour. Faded when lines would merge.
            float h = materialInput.height;
            float w = max(fwidth(h), 1e-3);
            float d = abs(fract(h / interval + 0.5) - 0.5) * interval;
            float dMajor = abs(fract(h / (interval * 5.0) + 0.5) - 0.5) * interval * 5.0;
            float minor = 1.0 - smoothstep(0.0, w * 0.9, d);
            float major = 1.0 - smoothstep(0.0, w * 1.6, dMajor);
            float fade = 1.0 - smoothstep(interval * 0.18, interval * 0.45, w);
            float line = max(minor * 0.65, major) * contourAlpha * fade;
            colour = mix(colour, vec3(0.20, 0.14, 0.08), line / max(alpha + line, 1e-3));
            alpha = max(alpha, line);
            m.diffuse = colour; m.alpha = alpha * detail;  // detail fades the shading and contours out from orbit
            return m;
          }`,
      },
      translucent: true,
    });
    const schedule = () => this.schedule();
    this.removers.push(v.camera.moveEnd.addEventListener(schedule));
    // Re-plant when the terrain under the view has finished refining, so trees sit on the surface that is drawn.
    this.removers.push(v.scene.globe.tileLoadProgressEvent.addEventListener((queued: number) => { if (queued === 0) { schedule(); this.declutterSoon(); } }));
    this.removers.push(v.scene.preRender.addEventListener(() => this.retune()));
    this.removers.push(v.camera.changed.addEventListener(() => this.declutter()));
    this.removers.push(v.camera.moveEnd.addEventListener(() => { this.declutter(); this.declutterSoon(); }));
    v.camera.percentageChanged = 0.02;
  }

  /** Turn the style on or off and choose its parts. `lighting` is true when the live day/night sun should light the scene. */
  set(active: boolean, opts: ReliefOptions, lighting: boolean) {
    const changed = active !== this.active || JSON.stringify(opts) !== JSON.stringify(this.opts) || lighting !== this.lighting;
    this.active = active; this.opts = opts; this.lighting = lighting; if (!changed) return;
    const g = this.v.scene.globe;
    this.base.show = active; this.roads.show = active && opts.roads; this.planet.show = active && this.planet.alpha > 0; this.far = -1;
    g.baseColor = active ? C.Color.fromCssColorString('#c9b48b') : this.ground;
    for (const l of this.hidden) l.show = !active;
    if (this.cover) this.cover.show = active && opts.cover;
    g.material = active ? this.material : undefined;
    this.material.uniforms.contourAlpha = opts.contours ? 0.32 : 0;
    this.applyLight();
    this.drawFeatures(); this.plantKey = ''; this.schedule(0);
    this.v.scene.requestRender();
  }

  /** New land cover and named features for the area of interest. */
  setContext(grid: Grid | null, features: TerrainFeature[]) {
    this.grid = grid; this.features = features;
    if (this.cover) { this.v.imageryLayers.remove(this.cover, true); this.cover = null; }
    if (grid) {
      void C.SingleTileImageryProvider.fromUrl(tintImage(grid), { rectangle: C.Rectangle.fromDegrees(...grid.bounds), credit: new C.Credit('ESA WorldCover 2021') }).then(p => {
        if (this.v.isDestroyed() || this.grid !== grid) return;
        this.cover = new C.ImageryLayer(p); this.cover.show = this.active && this.opts.cover; this.cover.alpha = 0.85;
        this.v.imageryLayers.add(this.cover, this.v.imageryLayers.indexOf(this.base) + 1); this.v.scene.requestRender();
      });
    }
    this.applyLight(); this.drawFeatures(); this.plantKey = ''; this.schedule(0);
  }

  /** Height, slope, facing direction and land cover at a point, read from the terrain as rendered. */
  read(lon: number, lat: number, metresPerPixel: number): TerrainReading {
    const g = this.v.scene.globe; const ex = this.v.scene.verticalExaggeration || 1;
    const at = (x: number, y: number) => { const h = g.getHeight(C.Cartographic.fromDegrees(x, y)); return h === undefined ? undefined : h / ex; };
    const h0 = at(lon, lat); const d = Math.max(15, metresPerPixel * 4);
    const dLon = d / (111_320 * Math.cos(lat * Math.PI / 180)), dLat = d / 110_574;
    const e = at(lon + dLon, lat), w = at(lon - dLon, lat), n = at(lon, lat + dLat), s = at(lon, lat - dLat);
    let slope: number | null = null, facing: string | null = null;
    if (e !== undefined && w !== undefined && n !== undefined && s !== undefined) {
      const dx = (e - w) / (2 * d), dy = (n - s) / (2 * d); slope = Math.atan(Math.hypot(dx, dy)) * 180 / Math.PI;
      // Facing is the downhill direction, as on a slope map.
      facing = slope < 2 ? 'level' : COMPASS[Math.round(((Math.atan2(-dx, -dy) * 180 / Math.PI) + 360) % 360 / 45) % 8];
    }
    const code = classAt(this.grid, lon, lat);
    return { elevation: h0 ?? null, slope, facing, cover: code && this.grid ? this.grid.classes[String(code)]?.name ?? null : null };
  }

  destroy() {
    clearTimeout(this.timer); clearTimeout(this.settle); this.removers.forEach(r => r());
    if (this.v.isDestroyed()) return;
    for (const p of [this.plants, this.pending]) if (p && !p.isDestroyed()) this.v.scene.primitives.remove(p);
    this.v.scene.globe.material = undefined; this.v.scene.light = this.sun; this.v.scene.globe.baseColor = this.ground;
  }

  // ---------------------------------------------------------------- internals
  private applyLight() {
    // A fixed light from the north-west (the cartographic convention), unless the live sun was asked for.
    // The live sun lights the relief only from orbit, where the globe shows day and night; closer in the relief keeps its
    // north-west lamp (the globe is not sun-shaded there), so trees and aircraft never go dark with the local night.
    const s = this.v.scene; const b = this.grid?.bounds;
    if (!this.active || this.sunNow || !b) { s.light = this.sun; return; }
    const centre = C.Cartesian3.fromDegrees((b[0] + b[2]) / 2, (b[1] + b[3]) / 2);
    const enu = C.Transforms.eastNorthUpToFixedFrame(centre);
    const toward = C.Matrix4.multiplyByPointAsVector(enu, new C.Cartesian3(-0.54, 0.54, 0.64), new C.Cartesian3());
    this.lamp = new C.DirectionalLight({ direction: C.Cartesian3.negate(C.Cartesian3.normalize(toward, toward), new C.Cartesian3()), intensity: 2.4 });
    s.light = this.lamp;
  }

  /** Contour spacing follows the camera's height above the ground (not above sea level) so lines stay readable. */
  private retune() {
    if (!this.active) return;
    const cam = this.v.camera.positionCartographic; const ex = this.v.scene.verticalExaggeration || 1;
    const ground = this.v.scene.globe.getHeight(cam) ?? 0; const above = Math.max(1, cam.height - ground) / ex;
    const interval = above < 400 ? 10 : above < 1200 ? 20 : above < 3000 ? 50 : above < 8000 ? 100 : above < 25000 ? 200 : 500;
    if (this.material.uniforms.interval !== interval) this.material.uniforms.interval = interval;
    // 0 below 120 km (the relief is made for an area of interest), 1 from 450 km: the planet layer fades in as the relief
    // detail fades out.
    const far = Math.round(C.Math.clamp((cam.height - 120_000) / 330_000, 0, 1) * 40) / 40;
    if (far !== this.far) { this.far = far; this.planet.alpha = far; this.planet.show = far > 0; this.material.uniforms.detail = 1 - far; this.v.scene.requestRender(); }
    const sunNow = this.lighting && cam.height > 5_000_000;
    if (sunNow !== this.sunNow) { this.sunNow = sunNow; this.applyLight(); this.v.scene.requestRender(); }
  }

  private drawFeatures() {
    if (this.v.isDestroyed()) return;
    const list = this.labels; list.removeAll();
    if (!this.active || !this.opts.features) { this.v.scene.requestRender(); return; }
    const icons = { peak: symbol('peak'), volcano: symbol('volcano'), place: symbol('place') };
    for (const f of this.features) {
      const water = f.kind === 'lake' || f.kind === 'glacier';
      const text = f.kind === 'peak' || f.kind === 'volcano' ? `${f.name}${f.elevation_m !== null ? `\n${f.elevation_m.toLocaleString('en')} m` : ''}` : f.name;
      list.add({
        id: `relief-${f.id}`, position: C.Cartesian3.fromDegrees(f.lon, f.lat),
        billboard: water ? undefined : { image: icons[f.kind as 'peak' | 'volcano' | 'place'], heightReference: C.HeightReference.CLAMP_TO_GROUND, verticalOrigin: C.VerticalOrigin.BOTTOM,
          disableDepthTestDistance: Number.POSITIVE_INFINITY, scale: f.kind === 'place' ? 0.7 : 0.9, distanceDisplayCondition: new C.DistanceDisplayCondition(0, f.kind === 'place' ? 160_000 : 260_000) },
        label: { text, font: f.kind === 'place' ? '500 12px "Archivo Variable", Arial, sans-serif' : water ? 'italic 500 12px "Archivo Variable", Arial, sans-serif' : '500 13px "Archivo Variable", Arial, sans-serif',
          fillColor: water ? C.Color.fromCssColorString('#cfe3ef') : C.Color.fromCssColorString('#fbf8f0'), outlineColor: C.Color.fromCssColorString('#16130e').withAlpha(0.8), outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE, heightReference: C.HeightReference.CLAMP_TO_GROUND, horizontalOrigin: water ? C.HorizontalOrigin.CENTER : C.HorizontalOrigin.LEFT,
          verticalOrigin: water ? C.VerticalOrigin.CENTER : C.VerticalOrigin.BOTTOM, pixelOffset: water ? new C.Cartesian2(0, 0) : new C.Cartesian2(11, 2),
          disableDepthTestDistance: Number.POSITIVE_INFINITY, distanceDisplayCondition: new C.DistanceDisplayCondition(0, f.kind === 'place' ? 160_000 : 260_000) },
        properties: { kind: 'terrain-feature', url: f.source_url },
      });
    }
    this.declutter(); this.v.scene.requestRender();
  }

  /** Ground-clamped labels settle a moment after terrain tiles arrive; check overlaps once they have. */
  private declutterSoon() { clearTimeout(this.settle); this.settle = setTimeout(() => this.declutter(), 450); }

  /** Hide labels that would overlap a more important one (higher summits first, then larger towns). */
  private declutter() {
    if (this.v.isDestroyed() || !this.active || !this.opts.features) return;
    const scene = this.v.scene; const g = scene.globe; const placed: [number, number, number, number][] = []; let changed = false;
    const order = this.features.map((f, i) => ({ f, i, rank: f.kind === 'volcano' || f.kind === 'peak' ? 2e4 + (f.elevation_m ?? 0) : f.kind === 'place' ? 1e4 + Math.log10(f.population || 1) * 100 : 5e3 }))
      .sort((a, b) => b.rank - a.rank);
    for (const { f } of order) {
      const e = this.labels.getById(`relief-${f.id}`); if (!e) continue;
      const c = C.Cartographic.fromDegrees(f.lon, f.lat); const h = g.getHeight(c) ?? 0;
      const win = C.SceneTransforms.worldToWindowCoordinates(scene, C.Cartesian3.fromDegrees(f.lon, f.lat, h));
      let show = Boolean(win);
      if (win) {
        const wide = Math.min(220, f.name.length * 7 + 22), tall = f.elevation_m !== null && f.kind !== 'place' ? 34 : 18;
        const box: [number, number, number, number] = [win.x - 8, win.y - tall, win.x + wide, win.y + 4];
        show = !placed.some(b => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]);
        if (show) placed.push(box);
      }
      if (e.show !== show) { e.show = show; changed = true; }
    }
    if (changed) scene.requestRender();
  }

  /** For a camera that never stops moving (a drone's): re-check what to plant now and then. */
  replant() { this.schedule(0); }

  private schedule(delay = 350) { clearTimeout(this.timer); this.timer = setTimeout(() => this.plant(), delay); }

  /**
   * Trees and shrubs for the part of the area in view. Their size follows the zoom (about ten pixels tall), so the
   * forest reads as texture from afar and as individual trees close up; density follows WorldCover's classes.
   */
  private plant() {
    const v = this.v; if (v.isDestroyed()) return; const scene = v.scene; const g = this.grid;
    const remove = () => {
      for (const p of [this.plants, this.pending]) if (p && !p.isDestroyed()) scene.primitives.remove(p);
      this.plants = null; this.pending = null; scene.requestRender();
    };
    if (!this.active || !this.opts.trees || !g || scene.mode !== C.SceneMode.SCENE3D) { this.plantKey = ''; remove(); return; }
    const rect = v.camera.computeViewRectangle(); if (!rect) { remove(); return; }
    const [w, s, e, n] = g.bounds;
    // Metres per pixel at the centre of the view, measured to the terrain (the ellipsoid lies far below mountains).
    const mid = new C.Cartesian2(v.canvas.clientWidth / 2, v.canvas.clientHeight / 2); const ray = v.camera.getPickRay(mid);
    const centre = (ray && scene.globe.pick(ray, scene)) || v.camera.pickEllipsoid(mid);
    const range = centre ? C.Cartesian3.distance(v.camera.position, centre) : v.camera.positionCartographic.height;
    const fovy = (v.camera.frustum as C.PerspectiveFrustum).fovy ?? 1;
    const mpp = 2 * Math.tan(fovy / 2) * range / Math.max(1, v.canvas.clientHeight);
    if (mpp > 70) { this.plantKey = ''; remove(); return; }
    // Plant around the centre of attention, not out to a distant horizon.
    let vw = C.Math.toDegrees(rect.west), vs = C.Math.toDegrees(rect.south), ve = C.Math.toDegrees(rect.east), vn = C.Math.toDegrees(rect.north);
    if (centre) {
      const c = C.Cartographic.fromCartesian(centre); const reach = mpp * v.canvas.clientWidth * 1.4;
      const clat = C.Math.toDegrees(c.latitude), clon = C.Math.toDegrees(c.longitude);
      const dLat = reach / 110_574, dLon = reach / (111_320 * Math.cos(c.latitude));
      vw = Math.max(vw, clon - dLon); ve = Math.min(ve, clon + dLon); vs = Math.max(vs, clat - dLat); vn = Math.min(vn, clat + dLat);
    }
    vw = Math.max(w, vw); vs = Math.max(s, vs); ve = Math.min(e, ve); vn = Math.min(n, vn);
    if (ve <= vw || vn <= vs) { remove(); return; }
    const treeH = C.Math.clamp(mpp * 11, 9, 320), radius = treeH * 0.3;
    const lat0 = (s + n) / 2, cellW = (e - w) / g.width * 111_320 * Math.cos(lat0 * Math.PI / 180), cellH = (n - s) / g.height * 110_574;
    const perCell = Math.min(40, cellW * cellH / (radius * 2.3) ** 2);
    // Snap the planted block to 6-cell steps (with a margin) so small camera moves do not re-plant the forest.
    const snap = 6;
    const x0 = Math.max(0, Math.floor((vw - w) / (e - w) * g.width / snap - 0.5) * snap), x1 = Math.min(g.width - 1, Math.ceil((ve - w) / (e - w) * g.width / snap + 0.5) * snap);
    const y0 = Math.max(0, Math.floor((n - vn) / (n - s) * g.height / snap - 0.5) * snap), y1 = Math.min(g.height - 1, Math.ceil((n - vs) / (n - s) * g.height / snap + 0.5) * snap);
    const key = [x0, x1, y0, y1, Math.round(Math.log2(mpp) * 2), scene.verticalExaggeration].join(':');
    const weight = (c: number) => TREE.has(c) ? 1 : c === 20 ? 0.45 : c === 30 ? 0.05 : c === 90 ? 0.08 : 0;
    let expected = 0;
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) expected += weight(g.codes[y * g.width + x]) * perCell;
    const f = Math.min(1, MAX_PLANTS / Math.max(1, expected));
    const instances: C.GeometryInstance[] = []; const heights: number[] = []; let missing = 0;
    const cone = new C.CylinderGeometry({ length: treeH, topRadius: 0, bottomRadius: radius, slices: 7, vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT });
    const bush = new C.EllipsoidGeometry({ radii: new C.Cartesian3(radius * 0.75, radius * 0.75, radius * 0.5), stackPartitions: 4, slicePartitions: 6, vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT });
    const globe = scene.globe; const scratch = new C.Cartographic();
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const code = g.codes[y * g.width + x]; const k = weight(code) * perCell * f; if (!k) continue;
      const count = Math.floor(k) + (hash(x, y) < k - Math.floor(k) ? 1 : 0);
      for (let j = 0; j < count; j++) {
        const lon = w + (x + hash(x, y, j * 2 + 1)) / g.width * (e - w), lat = n - (y + hash(x, y, j * 2 + 2)) / g.height * (n - s);
        C.Cartographic.fromDegrees(lon, lat, 0, scratch); const h = globe.getHeight(scratch);
        if (h === undefined) { missing++; continue; }
        heights.push(h);
        const tree = TREE.has(code) || (code === 20 && hash(x, j, y) < 0.3);
        const size = 0.75 + hash(y, x, j) * 0.5;
        const height = tree ? treeH * size : radius * 0.5;
        const base = C.Cartesian3.fromDegrees(lon, lat, h - (tree ? treeH * 0.08 : 0) + height / 2);
        const m = C.Transforms.eastNorthUpToFixedFrame(base); C.Matrix4.multiplyByUniformScale(m, size, m);
        const tone = hash(j, y, x);
        const colour = tree ? C.Color.fromBytes(52 + tone * 22, 66 + tone * 20, 38 + tone * 10) : C.Color.fromBytes(96 + tone * 20, 92 + tone * 18, 56 + tone * 10);
        instances.push(new C.GeometryInstance({ geometry: tree ? cone : bush, modelMatrix: m, attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(colour) } }));
      }
    }
    // Wait for terrain under most of the view before planting, so trees sit on the surface that is drawn.
    if (missing > instances.length * 0.3 && instances.length + missing > 40) { this.schedule(600); return; }
    // The same view re-plants only if the drawn terrain under the trees has moved by more than a fraction of a tree.
    if (key === this.plantKey && (this.plants || this.pending) && heights.length === this.plantHeights.length
      && heights.every((h, i) => Math.abs(h - this.plantHeights[i]) < Math.max(3, treeH * 0.12))) return;
    this.plantKey = key; this.plantHeights = heights; this.plantings++;
    // Only one batch is built at a time; a newer view supersedes a batch still being built.
    if (this.pending) { if (!this.pending.isDestroyed()) scene.primitives.remove(this.pending); this.pending = null; }
    if (!instances.length) { remove(); return; }
    const next = new C.Primitive({ geometryInstances: instances, appearance: new C.PerInstanceColorAppearance({ flat: false, closed: true, translucent: false }), asynchronous: true, releaseGeometryInstances: true, shadows: C.ShadowMode.DISABLED });
    scene.primitives.add(next); this.pending = next;
    // Swap once the new batch is ready, so the forest never blinks out while moving. The scene renders on demand,
    // so keep asking for frames while the geometry is built in the workers.
    const started = performance.now();
    const pump = setInterval(() => { if (this.pending !== next || next.ready || performance.now() - started > 8000) clearInterval(pump); else scene.requestRender(); }, 120);
    const swap = scene.postRender.addEventListener(() => {
      if (this.pending !== next) { swap(); return; }
      if (!next.ready) return;
      swap(); clearInterval(pump); this.pending = null;
      if (this.plants && !this.plants.isDestroyed()) scene.primitives.remove(this.plants);
      this.plants = next; scene.requestRender();
    });
    scene.requestRender();
  }
}
