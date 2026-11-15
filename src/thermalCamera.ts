import * as C from 'cesium';
import { imagery } from './tiles';
import type { Bounds } from './types';

/**
 * The drone's thermal camera.
 *
 * Measured base: Landsat 8/9 land-surface temperature (Collection 2, band 10, ~100 m), the large-scale truth.
 * Modelled detail: a drone flies tens to hundreds of metres up, where one Landsat pixel spans much of the frame, so
 * each map tile adds fine-scale variation from the imagery under it, using simple daytime surface physics: dark,
 * dry surfaces run warmer, vegetation (transpiration) and water run cooler. The detail is zero-mean over each tile,
 * so every area still averages to the measured temperature. Readouts and warm-ground markers use Landsat only.
 *
 * Tiles carry temperature as grey (an extended range around the scene's own); a post-process applies automatic
 * gain over what is in view (as real thermal cameras do), the palette, soft optics, a static fixed-pattern grain
 * and a vignette.
 */
export type ThermalScene = { bounds: Bounds; minC: number; maxC: number; date: string };
export type ThermalPixels = { data: Uint8ClampedArray; w: number; h: number };

const MARGIN_C = 12;  // room above and below the scene's range for the modelled detail
const DETAIL_MAX_C = 9;

/** The extended temperature range the grey tiles encode. */
export function greyRange(t: ThermalScene): [number, number] { return [t.minC - MARGIN_C, t.maxC + MARGIN_C]; }

/** Measured surface temperature at a point (bilinear over valid Landsat pixels), or null where there is none. */
export function celsiusAt(px: ThermalPixels, t: ThermalScene, lon: number, lat: number): number | null {
  const [w, s, e, n] = t.bounds;
  if (lon < w || lon > e || lat < s || lat > n) return null;
  const fx = (lon - w) / (e - w) * px.w - 0.5, fy = (n - lat) / (n - s) * px.h - 0.5;
  const x0 = Math.max(0, Math.floor(fx)), y0 = Math.max(0, Math.floor(fy)), x1 = Math.min(px.w - 1, x0 + 1), y1 = Math.min(px.h - 1, y0 + 1);
  const ax = Math.min(1, Math.max(0, fx - x0)), ay = Math.min(1, Math.max(0, fy - y0));
  let sum = 0, weight = 0;
  for (const [x, y, k] of [[x0, y0, (1 - ax) * (1 - ay)], [x1, y0, ax * (1 - ay)], [x0, y1, (1 - ax) * ay], [x1, y1, ax * ay]] as const) {
    const i = (y * px.w + x) * 4;
    if (px.data[i + 3] > 0 && k > 0) { sum += px.data[i] * k; weight += k; }
  }
  return weight > 0.25 ? t.minC + sum / weight / 255 * (t.maxC - t.minC) : null;
}

function median(px: ThermalPixels, t: ThermalScene): number {
  const values: number[] = [];
  for (let i = 0; i < px.w * px.h; i += 7) if (px.data[i * 4 + 3] > 0) values.push(px.data[i * 4]);
  values.sort((a, b) => a - b);
  return values.length ? t.minC + values[values.length >> 1] / 255 * (t.maxC - t.minC) : (t.minC + t.maxC) / 2;
}

const mercatorLat = (y: number, n: number) => Math.atan(Math.sinh(Math.PI * (1 - 2 * y / n))) * 180 / Math.PI;

/** Imagery provider for the thermal camera: one grey temperature tile per map tile (see above). */
export class ModelledThermalProvider {
  readonly tilingScheme = new C.WebMercatorTilingScheme();
  readonly rectangle = this.tilingScheme.rectangle;
  readonly tileWidth = 256; readonly tileHeight = 256;
  readonly minimumLevel = 0; readonly maximumLevel = 18;
  readonly tileDiscardPolicy = undefined; readonly proxy = undefined;
  readonly errorEvent = new C.Event();
  readonly hasAlphaChannel = false;
  readonly credit: C.Credit;
  private readonly fill: number;
  private readonly range: [number, number];
  private readonly source = imagery('maps/imagery/{z}/{y}/{x}');

  constructor(private px: ThermalPixels, private scene: ThermalScene) {
    this.fill = median(px, scene); this.range = greyRange(scene);
    this.credit = new C.Credit(`Landsat surface temperature ${scene.date} (USGS/NASA) · fine detail modelled from Esri World Imagery`);
  }

  getTileCredits() { return []; }
  pickFeatures() { return undefined; }

  private url(x: number, y: number, z: number) {
    const subs = this.source.subdomains;
    return this.source.url.replace('{s}', subs ? subs[(x + y + z) % subs.length] : '').replace('{z}', String(z)).replace('{y}', String(y)).replace('{x}', String(x));
  }

  async requestImage(x: number, y: number, level: number): Promise<HTMLCanvasElement> {
    const size = 256, n = 2 ** level;
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = size;
    const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
    let rgb: Uint8ClampedArray | null = null;
    try {
      const blob = await (await fetch(this.url(x, y, level))).blob();
      const bitmap = await createImageBitmap(blob); ctx.drawImage(bitmap, 0, 0, size, size); bitmap.close();
      rgb = ctx.getImageData(0, 0, size, size).data;
    } catch { rgb = null; }  // no imagery here (ocean, beyond coverage): the measured field alone
    const out = ctx.createImageData(size, size); const o = out.data;
    const [lo, hi] = this.range; const span = hi - lo;
    // Measured temperature on a coarse 33 x 33 lattice (Landsat is ~100 m; bilinear in between).
    const G = 32, lattice = new Float32Array((G + 1) * (G + 1));
    for (let j = 0; j <= G; j++) {
      const lat = mercatorLat(y + j / G, n);
      for (let i = 0; i <= G; i++) {
        const lon = (x + i / G) / n * 360 - 180;
        lattice[j * (G + 1) + i] = celsiusAt(this.px, this.scene, lon, lat) ?? this.fill;
      }
    }
    // Daytime surface physics from the imagery: darker and drier is warmer; vegetation and water are cooler.
    const detail = new Float32Array(size * size); let mean = 0;
    if (rgb) {
      for (let p = 0; p < size * size; p++) {
        const r = rgb[p * 4] / 255, g = rgb[p * 4 + 1] / 255, b = rgb[p * 4 + 2] / 255;
        const lum = 0.299 * r + 0.587 * g + 0.114 * b;
        const veg = Math.min(0.5, Math.max(0, (g - r) / (g + r + 0.04)));
        const water = b > g && b > r * 1.1 && lum < 0.28 ? 1 : 0;
        const d = Math.max(-DETAIL_MAX_C, Math.min(DETAIL_MAX_C, 10 * (0.3 - lum) - 16 * veg - 5 * water));
        detail[p] = d; mean += d;
      }
      mean /= size * size;
    }
    for (let py = 0; py < size; py++) {
      const gy = py / (size - 1) * G, j0 = Math.min(G - 1, Math.floor(gy)), fy = gy - j0;
      for (let pxl = 0; pxl < size; pxl++) {
        const gx = pxl / (size - 1) * G, i0 = Math.min(G - 1, Math.floor(gx)), fx = gx - i0;
        const a = lattice[j0 * (G + 1) + i0], b = lattice[j0 * (G + 1) + i0 + 1], c = lattice[(j0 + 1) * (G + 1) + i0], d = lattice[(j0 + 1) * (G + 1) + i0 + 1];
        const measured = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy;
        const p = py * size + pxl;
        const v = Math.round(Math.max(0, Math.min(1, (measured + (rgb ? detail[p] - mean : 0) - lo) / span)) * 255);
        o[p * 4] = o[p * 4 + 1] = o[p * 4 + 2] = v; o[p * 4 + 3] = 255;
      }
    }
    ctx.putImageData(out, 0, 0);
    return canvas;
  }
}

/** Thermal sensor post-process: automatic gain window (lo..hi of the grey scale), palette, optics, grain, vignette. */
export const THERMAL_SHADER = `
uniform sampler2D colorTexture;
uniform float palette;
uniform float lo;
uniform float hi;
in vec2 v_textureCoordinates;
vec3 ironbow(float t) {
  vec3 c0 = vec3(0.01, 0.0, 0.04), c1 = vec3(0.13, 0.02, 0.38), c2 = vec3(0.47, 0.05, 0.6), c3 = vec3(0.82, 0.22, 0.32), c4 = vec3(0.97, 0.52, 0.06), c5 = vec3(1.0, 0.86, 0.36), c6 = vec3(1.0, 1.0, 0.92);
  t = clamp(t, 0.0, 1.0) * 6.0;
  if (t < 1.0) return mix(c0, c1, t); if (t < 2.0) return mix(c1, c2, t - 1.0); if (t < 3.0) return mix(c2, c3, t - 2.0);
  if (t < 4.0) return mix(c3, c4, t - 3.0); if (t < 5.0) return mix(c4, c5, t - 4.0); return mix(c5, c6, t - 5.0);
}
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
void main() {
  vec2 px = 1.0 / czm_viewport.zw;
  // Thermal optics are softer than visible ones.
  float g = texture(colorTexture, v_textureCoordinates).r * 0.4;
  g += texture(colorTexture, v_textureCoordinates + vec2(px.x, 0.0)).r * 0.15;
  g += texture(colorTexture, v_textureCoordinates - vec2(px.x, 0.0)).r * 0.15;
  g += texture(colorTexture, v_textureCoordinates + vec2(0.0, px.y)).r * 0.15;
  g += texture(colorTexture, v_textureCoordinates - vec2(0.0, px.y)).r * 0.15;
  float t = clamp((g - lo) / max(0.002, hi - lo), 0.0, 1.0);
  // A microbolometer's fixed-pattern grain and faint column banding: static, never flickering.
  t = clamp(t + (hash(floor(gl_FragCoord.xy)) - 0.5) * 0.03 + (hash(vec2(floor(gl_FragCoord.x), 3.0)) - 0.5) * 0.018, 0.0, 1.0);
  vec3 c = palette < 0.5 ? ironbow(t) : palette < 1.5 ? vec3(t) : vec3(1.0 - t);
  vec2 d = v_textureCoordinates - 0.5;
  out_FragColor = vec4(c * (1.0 - dot(d, d) * 0.7), 1.0);
}`;

/** Automatic gain: the measured temperatures across the view (a grid of ground points), widened for the detail. */
export function viewRange(v: C.Viewer, px: ThermalPixels, t: ThermalScene): [number, number] | null {
  const W = v.canvas.clientWidth, H = v.canvas.clientHeight; const temps: number[] = [];
  for (let j = 1; j <= 4; j++) for (let i = 1; i <= 6; i++) {
    const ray = v.camera.getPickRay(new C.Cartesian2(W * i / 7, H * j / 5)); const hit = ray && v.scene.globe.pick(ray, v.scene);
    if (!hit) continue;
    const c = C.Cartographic.fromCartesian(hit); const temp = celsiusAt(px, t, C.Math.toDegrees(c.longitude), C.Math.toDegrees(c.latitude));
    if (temp !== null) temps.push(temp);
  }
  if (temps.length < 3) return null;
  temps.sort((a, b) => a - b);
  let lo = temps[0] - 5, hi = temps[temps.length - 1] + 5;
  if (hi - lo < 12) { const mid = (hi + lo) / 2; lo = mid - 6; hi = mid + 6; }
  return [lo, hi];
}
