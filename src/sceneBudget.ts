import * as C from 'cesium';

export type SceneMetrics = { fps: number; scale: number; readyMs: number | null; phase: 'loading' | 'refining' | 'ready'; pending: number; adaptive: boolean };
/** A bounded render budget. Coarse terrain arrives first; refinement and optional VFX follow it.
 * Frame samples are collected without React updates per frame. Background scenes stop drawing entirely. */
export class SceneBudget {
  private quality = 'balanced';
  private scale = 1;
  private first = false;
  private base = false;
  private baseAt = 0;
  private terrain = false;
  private readyMs: number | null = null;
  private readyAt = 0;
  private moving = false;
  private pending = 1;
  private samples: number[] = [];
  private lastFrame = 0;
  private lastReport = performance.now();
  private fps = 0;
  private adjustedAt = 0;
  private removers: (() => void)[] = [];
  private disposed = false;
  constructor(private v: C.Viewer, private report: (m: SceneMetrics) => void) {
    for (const name of ['first-frame','map-ready','terrain-ready','detail-ready']) performance.clearMarks(`terra:${name}`);
    this.removers.push(v.scene.postRender.addEventListener(() => this.frame()));
    this.removers.push(v.scene.globe.tileLoadProgressEvent.addEventListener((n: number) => { this.pending = n; }));
    this.removers.push(v.camera.moveStart.addEventListener(() => { this.moving = true; }));
    this.removers.push(v.camera.moveEnd.addEventListener(() => { this.moving = false; }));
    let operationsOpen = Boolean(document.querySelector('[data-terra-operations]'));
    const visibility = () => { v.useDefaultRenderLoop = !document.hidden && !operationsOpen; this.samples = []; this.lastFrame = 0; if (!document.hidden) v.scene.requestRender(); };
    const operations = (event: Event) => { operationsOpen = Boolean((event as CustomEvent).detail); visibility(); };
    document.addEventListener('terra:operations', operations);
    this.removers.push(() => document.removeEventListener('terra:operations', operations));
    document.addEventListener('visibilitychange', visibility);
    visibility();
    this.removers.push(() => document.removeEventListener('visibilitychange', visibility));
    this.configure('balanced');
  }
  get isReady() { return this.readyMs !== null; }
  error() { return (this.quality === 'high' ? 1.5 : this.quality === 'low' ? 5 : 2.5) * (this.readyMs === null ? 2 : 1); }
  configure(quality: string) {
    this.quality = quality;
    this.scale = quality === 'low' ? .75 : quality === 'high' ? Math.min(devicePixelRatio, 1.25) : 1;
    this.v.resolutionScale = this.scale;
    this.v.targetFrameRate = quality === 'low' ? 30 : 60;
    this.v.scene.msaaSamples = quality === 'high' ? 4 : 2;
    this.v.scene.postProcessStages.fxaa.enabled = true;
    this.v.scene.globe.maximumScreenSpaceError = this.error();
    // Ambient occlusion gives cinematic terrain depth, and is never paid for in the default profile.
    this.v.scene.postProcessStages.ambientOcclusion.enabled = quality === 'high' && C.PostProcessStageLibrary.isAmbientOcclusionSupported(this.v.scene);
    if (quality === 'high') {
      const ao = this.v.scene.postProcessStages.ambientOcclusion.uniforms;
      ao.intensity = 1.3; ao.lengthCap = .03; ao.stepSize = 1.5; ao.blurStepSize = 1;
    }
    this.v.scene.requestRender();
  }
  baseReady() { this.base = true; this.baseAt = performance.now(); }
  terrainReady(success = true) { this.terrain = true; if (success) performance.mark('terra:terrain-ready'); }
  private frame() {
    const now = performance.now();
    if (!this.first) { this.first = true; performance.mark('terra:first-frame'); }
    if (this.lastFrame && now - this.lastFrame < 250) { this.samples.push(now - this.lastFrame); if (this.samples.length > 90) this.samples.shift(); }
    this.lastFrame = now;
    if (this.readyMs === null && this.base && this.terrain && now - this.baseAt > 3000 && this.v.scene.globe.tilesLoaded) {
      performance.mark('terra:map-ready'); this.readyMs = now; this.readyAt = now;
      this.v.scene.globe.maximumScreenSpaceError = this.error(); this.v.scene.requestRender();
    }
    if (this.readyMs !== null && now - this.readyAt > 500 && !this.moving && this.v.scene.globe.tilesLoaded && !performance.getEntriesByName('terra:detail-ready').length) performance.mark('terra:detail-ready');
    if (now - this.lastReport < 2000) return;
    this.lastReport = now;
    if (this.samples.length >= 20) this.fps = Math.round(1000 * this.samples.length / this.samples.reduce((a,b) => a+b,0));
    // Only reduce pixels after sustained pressure; leave idle/on-demand scenes and quality=high alone.
    if (this.readyMs !== null && this.samples.length >= 40 && this.quality === 'balanced' && this.fps < 42 && now - this.adjustedAt > 6000 && this.scale > .75) {
      this.scale = Math.max(.75, this.scale - .1); this.v.resolutionScale = this.scale; this.adjustedAt = now; this.samples = []; this.v.scene.requestRender();
    }
    if (!this.disposed) this.report({fps:this.fps,scale:this.scale,readyMs:this.readyMs,phase:this.readyMs === null?'loading':this.pending?'refining':'ready',pending:this.pending,adaptive:this.scale < 1 && this.quality === 'balanced'});
  }
  destroy() { this.disposed = true; this.removers.forEach(f => f()); }
}
