import { useEffect, useRef, useState } from 'react';
import * as C from 'cesium';
import { Crosshair, Eye, EyeOff, HelpCircle, Gamepad2, Maximize2, Minimize2, Minus, Plus, X } from 'lucide-react';
import { ft, kt, type DroneState, type DroneStore } from './drones';
import { Relief, type Grid, type ReliefOptions, type TerrainFeature } from './relief';
import { imagery, terrainUrl } from './tiles';
import { colorizeThermal, type ThermalPalette as Palette } from './thermalPalette';
import type { Bounds } from './types';

export type Thermal = { png: string; bounds: Bounds; minC: number; maxC: number; date: string; source: string };
type Mode = 'eo' | 'ir' | 'change';

const MAPS = '/api/connector/maps';
const DEG = Math.PI / 180;
const FOV = 72;
const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const PALETTES: Record<Palette, string> = { ironbow: 'Ironbow', white: 'White hot', black: 'Black hot' };
const SCALE: Record<Palette, string> = {
  ironbow: 'linear-gradient(to top, #0a0010, #210561, #780d99, #d13852, #f7850f, #ffdb5c, #fffff0)',
  white: 'linear-gradient(to top, #000, #fff)', black: 'linear-gradient(to top, #fff, #000)',
};
type Hot = { lon: number; lat: number; c: number };
const NOTE_LABEL: Record<string, string> = { warm: 'WARM GROUND', change: 'CHANGED GROUND', station: 'ON STATION', observation: 'OBSERVATION' };

/** A daylight sensor look for the visible-light camera: slightly desaturated, a touch more contrast, a soft vignette. */
const EO_SHADER = `
uniform sampler2D colorTexture;
in vec2 v_textureCoordinates;
void main() {
  vec3 c = texture(colorTexture, v_textureCoordinates).rgb;
  float l = dot(c, vec3(0.299, 0.587, 0.114));
  c = mix(vec3(l), c, 0.86);
  c = clamp((c - 0.5) * 1.07 + 0.5, 0.0, 1.0);
  vec2 d = v_textureCoordinates - 0.5;
  out_FragColor = vec4(c * (1.0 - dot(d, d) * 0.6), 1.0);
}`;

/** The warmest places in a thermal scene, at least `sepM` apart (read from the decoded temperature image). */
function hottest(px: { data: Uint8ClampedArray; w: number; h: number }, t: Thermal, count = 6, sepM = 400): Hot[] {
  const idx: number[] = []; for (let i = 0; i < px.w * px.h; i++) if (px.data[i * 4 + 3] > 0) idx.push(i);
  idx.sort((a, b) => px.data[b * 4] - px.data[a * 4]);
  const [w, s, e, n] = t.bounds; const out: Hot[] = [];
  for (const i of idx.slice(0, 20000)) {
    const x = i % px.w, y = Math.floor(i / px.w); const lon = w + (x + 0.5) / px.w * (e - w), lat = n - (y + 0.5) / px.h * (n - s);
    if (out.every(q => Math.hypot((lon - q.lon) * 111_320 * Math.cos(lat * DEG), (lat - q.lat) * 110_574) > sepM)) {
      out.push({ lon, lat, c: t.minC + px.data[i * 4] / 255 * (t.maxC - t.minC) }); if (out.length >= count) break;
    }
  }
  return out;
}

/** 34°17.36′N 117°55.90′W: degrees and decimal minutes, as aircraft displays show positions. */
export function dm(lat: number, lon: number) {
  const f = (v: number, p: string, n: string, w: number) => { const a = Math.abs(v); let d = Math.floor(a); let m = (a - d) * 60; if (m >= 59.995) { d += 1; m = 0; } return `${String(d).padStart(w, '0')}°${m.toFixed(2).padStart(5, '0')}′${v >= 0 ? p : n}`; };
  return `${f(lat, 'N', 'S', 2)} ${f(lon, 'E', 'W', 3)}`;
}

/**
 * The camera on one drone: Terra's 3D world rendered from the aircraft's position and gimbal, with a
 * sensor-style heads-up display. For simulated drones every frame is a rendering of real terrain, land cover
 * and imagery, not footage; IR shows measured land-surface temperature where a thermal scene exists.
 * A real drone's own video, when its ground station provides a stream, is shown instead of the rendering.
 */
export default function DroneFeed({ id, store, terrain, relief, reliefOptions, change, thermal, expanded, visible, onExpand, onClose, onSight, onSample }: {
  id: string; store: DroneStore; terrain: { grid: Grid | null; features: TerrainFeature[] } | null; relief: boolean; reliefOptions: ReliefOptions;
  change?: { url: string; bounds: Bounds }; thermal?: Thermal | null; expanded: boolean; visible: boolean; onExpand: () => void; onClose: () => void;
  onSight: (p: { id: string; lon: number; lat: number } | null) => void; onSample?: (lon: number, lat: number) => Promise<number | null>;
}) {
  const mount = useRef<HTMLDivElement>(null);
  const credits = useRef<HTMLDivElement>(null);
  const viewer = useRef<C.Viewer | null>(null);
  const world = useRef<Relief | null>(null);
  const layers = useRef<{ base?: C.ImageryLayer; change?: C.ImageryLayer; thermal?: C.ImageryLayer; thermalKey?: string; changeKey?: string }>({});
  const eoStage = useRef<C.PostProcessStage | null>(null);
  const hot = useRef<Hot[]>([]);
  const [marks, setMarks] = useState<{ x: number; y: number; c: number; key: string }[]>([]);
  const thermalPixels = useRef<{ data: Uint8ClampedArray; w: number; h: number } | null>(null);
  const [mode, setMode] = useState<Mode>('eo');
  const [palette, setPalette] = useState<Palette>('ironbow');
  const [zoom, setZoom] = useState(1);
  const [aim, setAim] = useState({ az: 0, el: 0 });
  const [flying, setFlying] = useState(false);
  const [s, setS] = useState<DroneState | null>(() => store.state(id));
  const [spi, setSpi] = useState<{ lon: number; lat: number; h: number | null; range: number; temp: number | null; delta?: number | null } | null>(null);
  const [rec, setRec] = useState(0);
  const [video, setVideo] = useState(false);
  const [videoState,setVideoState]=useState<'loading'|'ready'|'error'>('loading');
  const [hudDetail,setHudDetail]=useState(true);
  const [help,setHelp]=useState(false);
  const [nadir,setNadir]=useState(false);
  const [tileQueue,setTileQueue]=useState(0);
  const [cameraError,setCameraError]=useState('');
  const [thermalRevision,setThermalRevision]=useState(0);
  const modeRef=useRef(mode);modeRef.current=mode;
  const keys = useRef(new Set<string>());
  const live = useRef({ zoom, aim, flying, nadir }); live.current = { zoom, aim, flying, nadir };
  const idRef = useRef(id); idRef.current = id;

  // One globe for the camera, kept for the life of the workspace. A WebGL context compiles its shaders and caches
  // its tiles once, so the camera is created as soon as the Drones workspace opens, warms up out of sight (shaders,
  // terrain and imagery around the aircraft), and opening it or switching aircraft only re-points it.
  useEffect(() => {
    if (!mount.current) return; let disposed = false;
    const v = new C.Viewer(mount.current, {
      baseLayer: false, animation: false, timeline: false, baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
      navigationHelpButton: false, fullscreenButton: false, selectionIndicator: false, infoBox: false, creditContainer: credits.current ?? document.createElement('div'),
      requestRenderMode: false, skyBox: false, contextOptions: { webgl: { alpha: false, powerPreference: 'high-performance' } }, msaaSamples: 2,
    });
    viewer.current = v; const scene = v.scene;
    if (import.meta.env.DEV) Object.assign(window, { __terraFeed: v });
    scene.backgroundColor = C.Color.fromCssColorString('#8fa2aa'); scene.highDynamicRange = false; scene.globe.maximumScreenSpaceError = 1.4; scene.globe.baseColor = C.Color.fromCssColorString('#2a2620');
    scene.screenSpaceCameraController.enableInputs = false; scene.fog.enabled = true; scene.fog.density = 1.6e-4; scene.fog.minimumBrightness = 0.45;
    // The visible-light camera renders the world in daylight whatever the local time: atmosphere lighting follows the
    // scene's fixed light, not the real sun (which would turn a night-time view black), and the space-view ground haze is off.
    scene.globe.showGroundAtmosphere = false; scene.globe.dynamicAtmosphereLighting = false; scene.globe.enableLighting = false;
    scene.atmosphere.dynamicLighting = C.DynamicAtmosphereLightingType.NONE;
    if (scene.skyAtmosphere) { scene.skyAtmosphere.show = true; scene.skyAtmosphere.saturationShift = -0.25; scene.skyAtmosphere.brightnessShift = 0.05; }
    // Sharper rendering without overloading the GPU: up to 1.5× pixels, finer terrain detail, FXAA on top of MSAA.
    v.resolutionScale = Math.min(window.devicePixelRatio, 1.5); scene.globe.maximumScreenSpaceError = 1.25; scene.postProcessStages.fxaa.enabled = true;
    // Retain close-range detail across turns, instead of discarding it just outside the view.
    scene.globe.tileCacheSize = 400;
    scene.globe.loadingDescendantLimit = 64;
    scene.globe.preloadAncestors = true;
    scene.globe.preloadSiblings = false;
    const base = v.imageryLayers.addImageryProvider(new C.UrlTemplateImageryProvider({ ...imagery('maps/imagery/{z}/{y}/{x}'), tilingScheme: new C.WebMercatorTilingScheme(), maximumLevel: 19, credit: new C.Credit('Esri World Imagery') }));
    layers.current.base = base;
    world.current = new Relief(v, base, [base]);
    const stopQueue=scene.globe.tileLoadProgressEvent.addEventListener((n:number)=>setTileQueue(n));
    const stopError=scene.renderError.addEventListener(()=>setCameraError('The rendered camera could not continue. Close and reopen the drone workspace.'));
    const eo = new C.PostProcessStage({ fragmentShader: EO_SHADER }); scene.postProcessStages.add(eo); eoStage.current = eo;
    void C.ArcGISTiledElevationTerrainProvider.fromUrl(terrainUrl()).then(t => { if (!disposed && !v.isDestroyed()) v.terrainProvider = t; }).catch(() => {});
    // Manual flight reads the ground from this view, which has the finest terrain around the aircraft.
    store.feedGroundAt = (lon, lat) => { if (v.isDestroyed()) return undefined; return scene.globe.getHeight(C.Cartographic.fromDegrees(lon, lat)); };

    // Every frame: put the camera where the aircraft is, pointing where its gimbal points.
    let last = performance.now(); let replant = 0;
    const tick = scene.preUpdate.addEventListener(() => {
      const now = performance.now(); const dt = Math.min(0.1, (now - last) / 1000); last = now;
      if (live.current.flying) {
        const k = keys.current; const ax = (a: string, b: string) => (k.has(a) ? 1 : 0) - (k.has(b) ? 1 : 0);
        store.fly(idRef.current, { forward: ax('w', 's'), right: ax('d', 'a'), yaw: ax('e', 'q'), climb: ax('r', 'f'), gimbal: ax('arrowup', 'arrowdown'), boost: k.has('shift') }, dt);
      }
      const st = store.state(idRef.current); if (!st) return;
      const ground = scene.globe.getHeight(C.Cartographic.fromDegrees(st.lon, st.lat));
      const height = Math.max(st.amslM, (ground ?? -1e9) + 8);
      const { aim: a, zoom: z } = live.current;
      v.camera.setView({ destination: C.Cartesian3.fromDegrees(st.lon, st.lat, height),
        orientation: { heading: (st.gimbal.headingDeg + a.az) * DEG, pitch: (live.current.nadir ? -90 : C.Math.clamp(st.gimbal.pitchDeg + a.el, -90, 20)) * DEG, roll: 0 } });
      (v.camera.frustum as C.PerspectiveFrustum).fov = (FOV / z) * DEG;
      if (now - replant > 2500) { replant = now; world.current?.replant(); }
    });
    // Warm-ground markers: where the hottest measured places fall in the picture (in front of the camera only).
    const project = () => {
      if (modeRef.current!=='ir' || !hot.current.length) { setMarks(m => m.length ? [] : m); return; }
      const dir = v.camera.directionWC; const eye = v.camera.positionWC; const out: { x: number; y: number; c: number; key: string }[] = [];
      for (const h of hot.current) {
        const ground = scene.globe.getHeight(C.Cartographic.fromDegrees(h.lon, h.lat)); if (ground === undefined) continue;
        const p = C.Cartesian3.fromDegrees(h.lon, h.lat, ground);
        if (C.Cartesian3.dot(C.Cartesian3.subtract(p, eye, new C.Cartesian3()), dir) <= 0) continue;
        const w = C.SceneTransforms.worldToWindowCoordinates(scene, p);
        if (w && w.x > 0 && w.y > 0 && w.x < v.canvas.clientWidth && w.y < v.canvas.clientHeight) out.push({ x: w.x, y: w.y, c: h.c, key: `${h.lon},${h.lat}` });
      }
      setMarks(out);
    };
    const ui = setInterval(() => { setS(store.state(idRef.current)); project(); }, 120);
    return () => { disposed = true; tick(); stopQueue(); stopError(); clearInterval(ui); store.feedGroundAt = null; world.current?.destroy(); world.current = null; eoStage.current = null; layers.current = {}; if (!v.isDestroyed()) v.destroy(); viewer.current = null; };
  }, [store]);

  // Shown: full frame rate. Parked: two frames a second at the detail the small view will show, so the tiles it
  // needs are already loaded when it opens, for little cost while it waits.
  useEffect(() => {
    const v = viewer.current; if (!v) return;
    const dpr = window.devicePixelRatio || 1;
    v.targetFrameRate = 30;
    v.useDefaultRenderLoop = visible;
    v.resolutionScale = Math.min(dpr, expanded && visible ? 1.5 : 1.25);
    v.scene.globe.maximumScreenSpaceError = expanded && visible ? 1.25 : 1.6;
    if (visible) return;
    const slow = setInterval(() => { if (!v.isDestroyed()) v.render(); }, 500);
    return () => clearInterval(slow);
  }, [visible, expanded]);

  useEffect(()=>{if(!visible){setFlying(false);keys.current.clear();}},[visible]);

  // The parked canvas has a different size. Resize immediately when its container changes,
  // including when the browser throttles its animation loop in a background tab.
  useEffect(() => {
    const el=mount.current;if(!el)return;
    const resize=()=>{const v=viewer.current;if(v&&!v.isDestroyed()){v.forceResize();v.scene.requestRender();}};
    const observer=new ResizeObserver(resize);observer.observe(el);resize();
    return()=>observer.disconnect();
  }, []);

  // Another aircraft: re-point the same camera.
  useEffect(() => { setAim({ az: 0, el: 0 }); setZoom(1); setFlying(false); setMarks([]); setS(store.state(id));setNadir(false);setVideo(false);setVideoState('loading');setSpi(null); }, [id, store]);

  // The world as styled on the main map (relief or photographic), its land cover and named places.
  useEffect(() => { world.current?.setContext(terrain?.grid ?? null, terrain?.features ?? []); }, [terrain]);
  useEffect(() => {
    const v = viewer.current; const w = world.current; if (!v || !w) return;
    const eo = mode !== 'ir';
    // The camera shows the world as seen, without map furniture: no contours or name labels.
    w.set(eo && relief, { ...reliefOptions, features: false, contours: false }, false);
    // With globe lighting on, Cesium supplies the eye-space positions the relief shading needs (without it they can
    // arrive undefined in a second globe). The relief's fixed north-west light keeps this daylight at any hour.
    v.scene.globe.enableLighting = eo && relief;
    // Only valid Landsat pixels are coloured; cloud and scene edges retain the dim base imagery.
    const base = layers.current.base;
    if (base) {
      base.show = mode === 'ir' || (eo && !relief);
      base.saturation = mode === 'ir' ? 0 : 1; base.brightness = mode === 'ir' ? 0.55 : 1; base.contrast = mode === 'ir' ? 0.75 : 1;
    }
    if (layers.current.thermal) layers.current.thermal.alpha = 1;
    if (layers.current.change) layers.current.change.show = mode === 'change';
    if (layers.current.thermal) layers.current.thermal.show = mode === 'ir';
    v.scene.globe.baseColor = mode === 'ir' ? C.Color.fromCssColorString('#262626') : relief ? C.Color.fromCssColorString('#c9b48b') : C.Color.fromCssColorString('#2a2620');
    if (v.scene.skyAtmosphere) v.scene.skyAtmosphere.show = mode !== 'ir';
    v.scene.fog.enabled = mode !== 'ir';
    if (eoStage.current) eoStage.current.enabled = mode === 'eo';
  }, [mode, palette, relief, reliefOptions]);

  // Replace radar layers when investigations change; never retain a previous area's overlay.
  useEffect(()=>{
    const v=viewer.current;if(!v)return;let cancelled=false;
    if(layers.current.change){v.imageryLayers.remove(layers.current.change,true);layers.current.change=undefined;}
    if(change)void C.SingleTileImageryProvider.fromUrl(change.url,{rectangle:C.Rectangle.fromDegrees(...change.bounds),credit:new C.Credit('NISAR backscatter change (NASA-ISRO) · Terra')}).then(p=>{
      if(cancelled||v.isDestroyed())return;const l=v.imageryLayers.addImageryProvider(p);l.alpha=.9;l.show=modeRef.current==='change';layers.current.change=l;v.scene.requestRender();
    }).catch(()=>{});
    return()=>{cancelled=true;};
  },[change?.url]);

  useEffect(()=>{
    let cancelled=false;thermalPixels.current=null;hot.current=[];setMarks([]);
    const v=viewer.current;
    if(v&&layers.current.thermal){v.imageryLayers.remove(layers.current.thermal,true);layers.current.thermal=undefined;}
    if(!thermal){setThermalRevision(n=>n+1);return;}
    const img=new Image();img.src=`data:image/png;base64,${thermal.png}`;
    void img.decode().then(()=>{if(cancelled)return;const cv=document.createElement('canvas');cv.width=img.width;cv.height=img.height;
      const ctx=cv.getContext('2d',{willReadFrequently:true});if(!ctx)return;ctx.drawImage(img,0,0);
      thermalPixels.current={data:ctx.getImageData(0,0,img.width,img.height).data,w:img.width,h:img.height};
      hot.current=hottest(thermalPixels.current,thermal);setThermalRevision(n=>n+1);
    }).catch(()=>{});
    return()=>{cancelled=true;};
  },[thermal]);

  useEffect(()=>{
    const v=viewer.current,px=thermalPixels.current;if(!v||!px||!thermal)return;let cancelled=false;
    const cv=document.createElement('canvas');cv.width=px.w;cv.height=px.h;const ctx=cv.getContext('2d');if(!ctx)return;
    const coloured=ctx.createImageData(px.w,px.h);coloured.data.set(colorizeThermal(px.data,palette));ctx.putImageData(coloured,0,0);
    if(layers.current.thermal){v.imageryLayers.remove(layers.current.thermal,true);layers.current.thermal=undefined;}
    void C.SingleTileImageryProvider.fromUrl(cv.toDataURL(),{rectangle:C.Rectangle.fromDegrees(...thermal.bounds),credit:new C.Credit(`${thermal.source}, ${thermal.date} · USGS/NASA`)}).then(p=>{
      if(cancelled||v.isDestroyed())return;const l=v.imageryLayers.addImageryProvider(p);l.show=modeRef.current==='ir';l.alpha=1;layers.current.thermal=l;v.scene.requestRender();
    }).catch(()=>{});
    return()=>{cancelled=true;};
  },[thermalRevision,palette,thermal]);

  useEffect(()=>{if((mode==='ir'&&!thermal)||(mode==='change'&&!change))setMode('eo');},[thermal,change,mode]);

  // The ground under the crosshair, its temperature and measured change; and a recording clock.
  useEffect(() => {
    if (!visible) return;
    let alive = true; let lastKey = '';
    const read = async () => {
      const v = viewer.current; if (!v || v.isDestroyed()) return;
      const mid = new C.Cartesian2(v.canvas.clientWidth / 2, v.canvas.clientHeight / 2); const ray = v.camera.getPickRay(mid);
      const hit = ray && v.scene.globe.pick(ray, v.scene); if (!hit) { setSpi(null); onSight(null); return; }
      const c = C.Cartographic.fromCartesian(hit); const lon = c.longitude / DEG, lat = c.latitude / DEG;
      let temp: number | null = null; const t = thermal, px = thermalPixels.current;
      if (t && px && lon >= t.bounds[0] && lon <= t.bounds[2] && lat >= t.bounds[1] && lat <= t.bounds[3]) {
        const x = Math.min(px.w - 1, Math.floor((lon - t.bounds[0]) / (t.bounds[2] - t.bounds[0]) * px.w)), y = Math.min(px.h - 1, Math.floor((t.bounds[3] - lat) / (t.bounds[3] - t.bounds[1]) * px.h));
        const i = (y * px.w + x) * 4; if (px.data[i + 3] > 0) temp = t.minC + px.data[i] / 255 * (t.maxC - t.minC);
      }
      setSpi(prev => ({ lon, lat, h: v.scene.globe.getHeight(c) ?? null, range: C.Cartesian3.distance(v.camera.position, hit), temp, delta: prev?.delta }));
      onSight({ id, lon, lat });
      const key = `${lon.toFixed(4)},${lat.toFixed(4)}`;
      if (mode === 'change' && onSample && key !== lastKey) { lastKey = key; const d = await onSample(lon, lat).catch(() => null); if (alive) setSpi(prev => prev&&Math.abs(prev.lon-lon)<0.00001&&Math.abs(prev.lat-lat)<0.00001 ? { ...prev, delta: d } : prev); }
    };
    const a = setInterval(() => { void read(); }, 400); const b = setInterval(() => setRec(r => r + 1), 1000);
    return () => { alive = false; clearInterval(a); clearInterval(b); onSight(null); };
  }, [id, mode, thermal, onSight, onSample, visible]);

  // Keyboard flight. Keys are captured only while flying, so the rest of the workspace keeps its shortcuts.
  useEffect(() => {
    if (!flying) return;
    const down = (e: KeyboardEvent) => {
      if(e.target instanceof HTMLInputElement||e.target instanceof HTMLSelectElement||e.target instanceof HTMLTextAreaElement)return;
      const k = e.key.toLowerCase(); if (k === 'escape') { setFlying(false); return; }
      if (['w', 'a', 's', 'd', 'q', 'e', 'r', 'f', 'arrowup', 'arrowdown', 'shift', ' '].includes(k)) { e.preventDefault(); e.stopPropagation(); keys.current.add(k === ' ' ? 'r' : k); }
    };
    const up = (e: KeyboardEvent) => { const k = e.key.toLowerCase(); keys.current.delete(k === ' ' ? 'r' : k); };
    const blur = () => keys.current.clear();
    window.addEventListener('keydown', down, true); window.addEventListener('keyup', up, true); window.addEventListener('blur', blur);
    store.takeControl(id); setAim({ az: 0, el: 0 });
    return () => { window.removeEventListener('keydown', down, true); window.removeEventListener('keyup', up, true); window.removeEventListener('blur', blur); keys.current.clear(); store.releaseControl(id); };
  }, [flying, id, store]);

  // Drag slews the gimbal; the wheel zooms.
  const drag = useRef<{ x: number; y: number; az: number; el: number } | null>(null);
  const onDown = (e: React.PointerEvent) => { if(video)return;setNadir(false); (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); drag.current = { x: e.clientX, y: e.clientY, az: aim.az, el: aim.el }; };
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current; const el = mount.current; if (!d || !el) return; const k = FOV / zoom / el.clientWidth;
    setAim({ az: C.Math.clamp(d.az - (e.clientX - d.x) * k, -170, 170), el: C.Math.clamp(d.el + (e.clientY - d.y) * k, -60, 60) });
  };
  const onWheel = (e: React.WheelEvent) => !video && setZoom(z => C.Math.clamp(z * (e.deltaY < 0 ? 1.12 : 1 / 1.12), 1, 16));

  if (!s) return <div className="sensor drone-feed parked" aria-hidden="true"><div className="sensor-screen"><div ref={mount} className="sensor-canvas" /></div><div ref={credits} className="sensor-credits" /></div>;
  const gimbalHeading = ((s.gimbal.headingDeg + aim.az) % 360 + 360) % 360;
  const gimbalPitch = nadir ? -90 : C.Math.clamp(s.gimbal.pitchDeg + aim.el, -90, 20);
  const ticks: number[] = []; for (let a = Math.floor((gimbalHeading - 40) / 5) * 5; a <= gimbalHeading + 40; a += 5) ticks.push(a);
  const now = new Date();
  const stale=s.status==='stale'||s.status==='lost';
  const criticalBattery=Number.isFinite(s.batteryPct)&&s.batteryPct<=15&&s.status==='airborne';
  const isLiveVideo=Boolean(s.videoUrl)&&s.source==='real'&&video;
  const hasVideo = Boolean(s.videoUrl) && s.source === 'real';
  const fmt = (n: number, d = 0) => Number.isFinite(n) ? n.toLocaleString('en', { maximumFractionDigits: d, minimumFractionDigits: d }) : '—';
  const sensor = mode === 'eo' ? (relief ? 'EO · DAY · RELIEF' : 'EO · DAY') : mode === 'ir' ? `IR · ${PALETTES[palette].toUpperCase()}` : 'Δ · CHANGE';
  const note = store.info.notes.find(n => n.drone_id === id);
  const recText = `${Math.floor(rec / 3600)}:${String(Math.floor(rec / 60) % 60).padStart(2, '0')}:${String(rec % 60).padStart(2, '0')}`;

  return <div className={`sensor drone-feed ${expanded ? 'expanded' : ''} ${visible ? '' : 'parked'} ${hudDetail?'':'minimal-hud'} ${isLiveVideo?'video-mode':''}`} role="region" aria-label={`Camera on ${s.callsign} ${s.name}`} aria-hidden={!visible} inert={!visible}>
    <div className="sensor-screen" onPointerDown={onDown} onPointerMove={onMove} onPointerUp={() => { drag.current = null; }} onPointerCancel={()=>{drag.current=null;}} onWheel={onWheel}>
      <div ref={mount} className="sensor-canvas" />
      <div className="camera-head"><div><strong>{s.callsign}</strong><span>{s.name||'Observation camera'}{store.info.area?.name?` · ${store.info.area.name}`:''}</span><em className={s.source}>{s.source==='real'?'Connected aircraft':s.source==='sitl'?'SITL simulation':'Virtual simulation'}</em></div><div><button onClick={()=>setHudDetail(v=>!v)} aria-label={hudDetail?'Simplify camera HUD':'Show full camera HUD'} aria-pressed={!hudDetail}>{hudDetail?<Eye size={14}/>:<EyeOff size={14}/>}</button><button onClick={onExpand} aria-label={expanded?'Return to map':'Expand camera'}>{expanded?<Minimize2 size={14}/>:<Maximize2 size={14}/>}</button><button onClick={onClose} aria-label="Close camera"><X size={15}/></button></div></div>
      {hasVideo && video && <img className="drone-video" src={s.videoUrl} alt={`Live video from ${s.name}`} referrerPolicy="no-referrer" onLoad={()=>setVideoState('ready')} onError={()=>setVideoState('error')} />}
      {cameraError&&<div className="camera-state error" role="alert">{cameraError}</div>}
      {isLiveVideo&&videoState!=='ready'&&<div className="camera-state" role="status">{videoState==='error'?'The aircraft video stream is unavailable.':'Connecting to aircraft video…'}{videoState==='error'&&<button onClick={()=>setVideo(false)}>Use rendered view</button>}</div>}
      {stale&&<div className="camera-telemetry-warning" role="status">{s.status==='lost'?'Telemetry lost':'Telemetry delayed'} · last report {s.ageS!==undefined?`${Math.round(s.ageS)} s ago`:'unavailable'}</div>}
      {!stale&&criticalBattery&&<div className="camera-telemetry-warning" role="status">Battery critical · {Math.round(s.batteryPct)}% · {s.mission.status}</div>}
      <div className="hud" aria-hidden="true">
        <i className="br tl" /><i className="br tr" /><i className="br bl" /><i className="br bottom-r" />
        <div className="hud-tl"><span><b className="rec" />VIEW {recText}<em className={`src ${s.source}`}>{s.source === 'real' ? (hasVideo && video ? 'LIVE VIDEO' : 'LIVE · RENDERED') : s.source === 'sitl' ? 'SITL · ARDUPILOT' : 'SIM'}</em></span>
          <span>{s.callsign}{s.name ? ` ${s.name.toUpperCase()}` : ''}</span><span className="dim">{hasVideo && video ? 'EO · LIVE STREAM' : sensor}</span></div>
        <div className="hud-tr"><span>{now.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' }).toUpperCase()}, {now.toISOString().slice(11, 16)} UTC</span><span className="dim">{s.mission.status.toUpperCase()}</span></div>
        <div className="hud-tape">
          <div className="tape-ticks">{ticks.map(a => { const x = 50 + (a - gimbalHeading) * 1.2; const n = ((a % 360) + 360) % 360;
            return <span key={a} style={{ left: `${x}%` }} className={n % 30 === 0 ? 'major' : n % 10 === 0 ? 'mid' : ''}>{n % 30 === 0 && <em>{n % 90 === 0 ? COMPASS[n / 45] : n}</em>}</span>; })}</div>
          <b className="tape-mark">▲</b><b className="tape-value">{Math.round(gimbalHeading)}°</b>
        </div>
        <div className="hud-pitch"><span className="pitch-label">GMB</span><div className="pitch-scale"><i style={{ top: `${C.Math.clamp(-gimbalPitch / 90, 0, 1) * 100}%` }}><em>{Math.round(gimbalPitch)}°</em></i></div></div>
        <div className="hud-cross"><i /><i /></div>
        {mode === 'ir' && marks.map(m => <div key={m.key} className="hot-mark" style={{ left: m.x, top: m.y }}><i /><span>HOT {thermal && m.c >= thermal.maxC - 0.2 ? '≥' : ''}{m.c.toFixed(0)}°C</span></div>)}
        {mode === 'ir' && thermal && <div className="ir-scale"><b>{thermal.maxC.toFixed(0)}°C</b><i style={{ background: SCALE[palette] }} /><b>{thermal.minC.toFixed(0)}°C</b>
          <small>LANDSAT LST {thermal.date}</small></div>}
        {note && <div className={`hud-note ${note.kind}`}><b>NOTE · {NOTE_LABEL[note.kind] ?? note.kind.toUpperCase()}</b><span>{note.text}</span></div>}
        <div className="hud-bl"><span>ALT {fmt(Math.round(ft(s.aglM) / 10) * 10)} FT AGL</span><span>GS {String(Math.round(kt(s.speedMps))).padStart(3, ' ')} KT</span><span>HDG {String(Math.round(s.headingDeg) % 360).padStart(3, '0')}°</span></div>
        <div className="hud-bc">
          {flying && <span className="fly-hint">W/S FWD · A/D SIDE · Q/E YAW · R/F CLIMB · ↑/↓ GIMBAL · SHIFT FAST · ESC</span>}
          <span>SPI {spi ? dm(spi.lat, spi.lon) : '—'}</span>
          <span className="dim">RNG {spi ? `${fmt(spi.range)} M` : '—'} · ELEV {spi?.h != null ? `${fmt(spi.h)} M` : '—'}{mode === 'ir' ? (spi?.temp != null ? ` · ${spi.temp.toFixed(0)} °C` : ' · NO LST HERE') : ''}{mode === 'change' && spi?.delta != null ? ` · Δ ${spi.delta >= 0 ? '+' : '−'}${Math.abs(spi.delta).toFixed(1)} DB` : ''}</span></div>
        <div className="hud-br"><span>BAT {Number.isFinite(s.batteryPct) ? `${Math.round(s.batteryPct)}%` : '—'}</span><span>LNK {Number.isFinite(s.linkDbm) ? `${Math.round(s.linkDbm)}`.replace('-', '−') : '—'} DBM</span>
          <span>ZOOM {zoom.toFixed(1)}× · {Math.round(FOV / zoom)}° FOV</span></div>
      </div>
    </div>
    <div ref={credits} className="sensor-credits" aria-label="Imagery and data credits" />
    <div className="sensor-bar">

      <div className="sensor-modes" role="group" aria-label="Camera mode">
        <button className={mode === 'eo' ? 'on' : ''} onClick={() => {setMode('eo');setVideo(false);}} title="Visible light: the 3D world as styled on the map">EO</button>
        <button className={mode === 'ir' ? 'on sar' : ''} disabled={!thermal} onClick={() => {setMode('ir');setVideo(false);}} title={thermal ? `Land-surface temperature, ${thermal.source}, ${thermal.date}` : 'No cloud-free thermal scene for this area yet'}>IR</button>
        <button className={mode === 'change' ? 'on' : ''} disabled={!change} onClick={() => {setMode('change');setVideo(false);}} title={change ? 'Radar backscatter change from this investigation' : 'Appears after a two-date investigation'}>Δ</button>
      </div>
      {mode === 'ir' && <button className="sensor-palette" onClick={() => setPalette(p => p === 'ironbow' ? 'white' : p === 'white' ? 'black' : 'ironbow')}>{PALETTES[palette]}</button>}
      {hasVideo && <button className={`sensor-palette ${video ? 'on' : ''}`} onClick={() => {setVideoState('loading');setVideo(v => !v);setMode('eo');setFlying(false);}}>{video ? 'Rendered view' : 'Live video'}</button>}
      {s.source !== 'real' && <button className={`sensor-palette fly ${flying ? 'on' : ''}`} onClick={() => setFlying(f => !f)}
        title={s.source === 'sitl' ? 'Fly this ArduPilot SITL aircraft with the keyboard (GUIDED mode setpoints; real aircraft are never flown from the browser)' : 'Fly this virtual aircraft with the keyboard (real aircraft are never flown from the browser)'}><Gamepad2 size={14} />{flying ? 'Flying' : 'Fly'}</button>}
      {!isLiveVideo&&<div className="gimbal-presets" role="group" aria-label="Camera direction"><button className={!nadir?'on':''} onClick={()=>{setNadir(false);setAim({az:0,el:0});}}>Mission target</button><button className={nadir?'on':''} onClick={()=>setNadir(true)}>Look down</button></div>}
      <span className="grow" />
      <button className="icon-btn" disabled={isLiveVideo} onClick={() => setZoom(z => Math.max(1, z / 1.5))} aria-label="Zoom out"><Minus size={14} /></button>
      <span className="num zoom-read">{zoom.toFixed(1)}×</span>
      <button className="icon-btn" disabled={isLiveVideo} onClick={() => setZoom(z => Math.min(16, z * 1.5))} aria-label="Zoom in"><Plus size={14} /></button>
      <button className="icon-btn" onClick={() => { setNadir(false);setAim({ az: 0, el: 0 }); setZoom(1); }} aria-label="Point the camera back at the mission target" title="Re-centre"><Crosshair size={14} /></button>
<button className="icon-btn" onClick={()=>setHelp(h=>!h)} aria-label="Camera controls help" aria-expanded={help}><HelpCircle size={14}/></button>
    </div>
    <div className="camera-context"><span><i className={stale?'warn':'ok'}/>{isLiveVideo?'Aircraft video':mode==='ir'?`Landsat surface temperature · ${thermal?.date} · ~100 m source`:mode==='change'?'NISAR change overlay · measured acquisition pair':`${relief?'Relief':'Satellite imagery'} · rendered from aircraft telemetry`}</span><span>{isLiveVideo?'':tileQueue>0?'Loading terrain and imagery…':'Drag to aim · scroll to zoom'}</span></div>
    {help&&<div className="camera-help"><strong>Camera controls</strong><p>Drag the image to slew the gimbal. Scroll or use +/− to zoom. Mission target restores tracking; Look down gives a vertical view. The eye button simplifies telemetry.</p><p>IR shows a dated Landsat temperature layer; Δ shows the investigation’s radar change. Areas without a measurement show the base map.</p>{s.source!=='real'&&<p>Fly controls simulation: W/S forward, A/D sideways, Q/E yaw, R/F climb, arrows for gimbal, Shift for speed, Escape to hand back.</p>}</div>}
  </div>;
}
