import * as C from 'cesium';

/**
 * Ground heights under aircraft and other moving things, without ray-casting the drawn terrain every frame.
 *
 * `globe.getHeight` walks the terrain mesh's triangles for each call; called per aircraft per frame (and by every
 * consumer of the drone store) it cost ~200 ms of main-thread time per second, and it returns nothing at all while
 * Google Photorealistic 3D Tiles hide the globe, which left aircraft drawn at sea level inside the mountain.
 *
 * Here heights are sampled asynchronously on a ~100 m grid (0.001°) and cached; reads are synchronous and bilinear
 * between grid nodes, so a moving aircraft's ground changes smoothly. While the photoreal tiles are on screen the
 * samples come from those tiles (the ground actually drawn), otherwise from the terrain provider (true heights).
 * Heights are true metres above the ellipsoid (no vertical exaggeration).
 */
export class GroundHeights {
  private caches: Record<'terrain' | 'tiles', Map<string, number>> = { terrain: new Map(), tiles: new Map() };
  private queued = new Map<string, C.Cartographic>(); private tries = new Map<string, number>();
  private inFlight = false; private timer = 0; private disposed = false;
  /** Where samples come from: the photoreal tiles while they are drawn, otherwise the terrain provider. */
  source: 'terrain' | 'tiles' = 'terrain';

  constructor(private scene: C.Scene, private terrain: () => C.TerrainProvider | undefined,
              private exclude: () => object[] = () => [], private onUpdate: () => void = () => {}) {}

  private static STEP = 0.001;
  private static key(i: number, j: number) { return `${i}:${j}`; }

  /** True ground height at a point, or undefined until it has been sampled (a sample is then requested). */
  get(lon: number, lat: number): number | undefined {
    const s = GroundHeights.STEP; const x = lon / s, y = lat / s;
    const i = Math.floor(x), j = Math.floor(y); const fx = x - i, fy = y - j;
    const cache = this.caches[this.source];
    const h00 = this.node(cache, i, j), h10 = this.node(cache, i + 1, j), h01 = this.node(cache, i, j + 1), h11 = this.node(cache, i + 1, j + 1);
    const known = [h00, h10, h01, h11].filter((h): h is number => h !== undefined && Number.isFinite(h));
    if (!known.length) {
      // Fall back to the other source while this one fills in, so an aircraft never vanishes on a style change.
      const other = this.caches[this.source === 'tiles' ? 'terrain' : 'tiles'];
      const o = [this.peek(other, i, j), this.peek(other, i + 1, j), this.peek(other, i, j + 1), this.peek(other, i + 1, j + 1)].filter((h): h is number => h !== undefined && Number.isFinite(h));
      return o.length ? o.reduce((a, b) => a + b, 0) / o.length : undefined;
    }
    if (known.length < 4) return known.reduce((a, b) => a + b, 0) / known.length;
    return (h00! * (1 - fx) + h10! * fx) * (1 - fy) + (h01! * (1 - fx) + h11! * fx) * fy;  // all four finite here
  }

  private peek(cache: Map<string, number>, i: number, j: number) { return cache.get(GroundHeights.key(i, j)); }
  private node(cache: Map<string, number>, i: number, j: number): number | undefined {
    const k = GroundHeights.key(i, j); const h = cache.get(k);
    if (h === undefined && !this.queued.has(this.source + k)) {
      this.queued.set(this.source + k, C.Cartographic.fromDegrees(i * GroundHeights.STEP, j * GroundHeights.STEP));
      this.schedule();
    }
    return h;
  }

  private schedule() { if (!this.timer && !this.disposed) this.timer = window.setTimeout(() => { this.timer = 0; void this.flush(); }, 60); }

  private async flush() {
    if (this.inFlight || this.disposed || !this.queued.size) return;
    this.inFlight = true;
    const batch = [...this.queued.entries()].slice(0, 48); const source = this.source;
    const mine = batch.filter(([k]) => k.startsWith(source));
    for (const [k] of batch) if (!k.startsWith(source)) this.queued.delete(k);  // requests for a source no longer in use
    try {
      const points = mine.map(([, c]) => c.clone());
      let sampled: (C.Cartographic | undefined)[] = [];
      if (source === 'tiles' && this.scene.sampleHeightSupported) sampled = await this.scene.sampleHeightMostDetailed(points, this.exclude());
      else { const t = this.terrain(); if (t && !(t instanceof C.EllipsoidTerrainProvider)) sampled = await C.sampleTerrainMostDetailed(t, points); }
      const cache = this.caches[source];
      sampled.forEach((c, n) => { const k = mine[n][0].slice(source.length); if (c && Number.isFinite(c.height)) cache.set(k, c.height); });
      if (cache.size > 40_000) cache.clear();
      if (sampled.length) this.onUpdate();
    } catch { /* tiles not ready yet: the points stay queued and are retried */ }
    for (const [k] of mine) {
      if (this.caches[source].has(k.slice(source.length))) { this.queued.delete(k); this.tries.delete(k); continue; }
      // No height after three tries (open sea, or no data there): remember that, and stop asking.
      const n = (this.tries.get(k) ?? 0) + 1; this.tries.set(k, n);
      if (n >= 3) { this.caches[source].set(k.slice(source.length), NaN); this.queued.delete(k); this.tries.delete(k); }
    }
    this.inFlight = false;
    if (this.queued.size) this.timer = window.setTimeout(() => { this.timer = 0; void this.flush(); }, 250);
  }

  dispose() { this.disposed = true; clearTimeout(this.timer); }
}
