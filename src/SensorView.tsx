import { useEffect, useMemo, useRef, useState } from 'react';
import * as C from 'cesium';
import { Crosshair, Maximize2, Minimize2, Minus, Plus, X } from 'lucide-react';
import { SWATH, type Sat } from './fleet';
import { dmsPretty, subPoint } from './geo';
import type { Bounds } from './types';

export type SensorLayer = { url: string; bounds: Bounds; date?: string };
type Mode = 'eo' | 'sar' | 'change';
type Palette = 'gray' | 'ironbow';

const MAPS = '/api/connector/maps';
const R_EARTH = 6371;
const DEG = 180 / Math.PI;
const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

/** Iron-bow false colour (black, violet, magenta, orange, yellow, white) on luminance, as thermal cameras use. */
const PALETTE_SHADER = `
uniform sampler2D colorTexture;
uniform float ironbow;
in vec2 v_textureCoordinates;
vec3 ramp(float t) {
  vec3 c0 = vec3(0.02, 0.0, 0.05), c1 = vec3(0.17, 0.02, 0.42), c2 = vec3(0.55, 0.05, 0.56), c3 = vec3(0.86, 0.26, 0.24), c4 = vec3(0.98, 0.58, 0.05), c5 = vec3(1.0, 0.9, 0.42), c6 = vec3(1.0, 1.0, 0.95);
  t = clamp(t, 0.0, 1.0) * 6.0;
  if (t < 1.0) return mix(c0, c1, t); if (t < 2.0) return mix(c1, c2, t - 1.0); if (t < 3.0) return mix(c2, c3, t - 2.0);
  if (t < 4.0) return mix(c3, c4, t - 3.0); if (t < 5.0) return mix(c4, c5, t - 4.0); return mix(c5, c6, t - 5.0);
}
void main() {
  vec4 c = texture(colorTexture, v_textureCoordinates);
  float l = dot(c.rgb, vec3(0.299, 0.587, 0.114));
  out_FragColor = vec4(ironbow > 0.5 ? ramp(l) : vec3(l), 1.0);
}`;

/** Bearing (degrees from north) from point a to point b. */
function bearing(lat1: number, lon1: number, lat2: number, lon2: number) {
  const p1 = lat1 / DEG, p2 = lat2 / DEG, dl = (lon2 - lon1) / DEG;
  return (Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)) * DEG + 360) % 360;
}
const centralAngle = (lat1: number, lon1: number, lat2: number, lon2: number) => {
  const p1 = lat1 / DEG, p2 = lat2 / DEG, dl = (lon2 - lon1) / DEG;
  return Math.acos(Math.min(1, Math.sin(p1) * Math.sin(p2) + Math.cos(p1) * Math.cos(p2) * Math.cos(dl)));
};

/**
 * A simulated view of the area from a satellite at its predicted position during a pass, with a sensor-style
 * heads-up display. It shows look geometry (range, off-nadir and incidence angles), not imagery the satellite took:
 * the EO view drapes basemap or Sentinel-2 imagery, the SAR and change views drape this investigation's NISAR layers.
 */
export default function SensorView({ sat, at, target, bbox, eo, sar, change, expanded, onExpand, onClose }: {
  sat: Sat; at: number; target: { lon: number; lat: number }; bbox: Bounds;
  eo?: SensorLayer; sar?: SensorLayer; change?: SensorLayer; expanded: boolean; onExpand: () => void; onClose: () => void;
}) {
  const mount = useRef<HTMLDivElement>(null);
  const credits = useRef<HTMLDivElement>(null);
  const viewer = useRef<C.Viewer | null>(null);
  const layers = useRef<Partial<Record<Mode, C.ImageryLayer>>>({});
  const stage = useRef<C.PostProcessStage | null>(null);
  const [mode, setMode] = useState<Mode>(sar && sat.role === 'Radar imaging' ? 'sar' : 'eo');
  const [palette, setPalette] = useState<Palette>('ironbow');
  const [zoom, setZoom] = useState(1);
  const [aim, setAim] = useState<{ az: number; el: number }>({ az: 0, el: 0 });
  const [spi, setSpi] = useState<{ lon: number; lat: number; h: number | null } | null>(null);
  const [clock, setClock] = useState(0);
  const [ready, setReady] = useState(false);

  // Geometry of the pass: where the satellite is, how it moves, and how it sees the target.
  const geo = useMemo(() => {
    const when = new Date(at); const p = subPoint(sat.orbit, when); const q = subPoint(sat.orbit, new Date(at + 10_000));
    if (!p) return null;
    const h = p.altKm; const gamma = centralAngle(p.lat, p.lon, target.lat, target.lon);
    const range = Math.sqrt(R_EARTH ** 2 + (R_EARTH + h) ** 2 - 2 * R_EARTH * (R_EARTH + h) * Math.cos(gamma));
    const offNadir = Math.asin(Math.min(1, R_EARTH * Math.sin(gamma) / range)) * DEG;
    const incidence = offNadir + gamma * DEG;
    const track = q ? bearing(p.lat, p.lon, q.lat, q.lon) : 0;
    const look = bearing(p.lat, p.lon, target.lat, target.lon);
    const side = ((look - track + 540) % 360) - 180; // negative: target left of track
    const ground = p.speedKms * R_EARTH / (R_EARTH + h);
    const halfKm = Math.hypot((bbox[2] - bbox[0]) * 111.32 * Math.cos(target.lat / DEG), (bbox[3] - bbox[1]) * 110.57) / 2;
    const fov = Math.max(0.05, 2 * Math.atan(halfKm * 1.35 / range) * DEG);
    return { p, h, range, offNadir, incidence, track, look, side, ground, fov, nadirKm: gamma * R_EARTH };
  }, [sat, at, target.lat, target.lon, bbox]);

  // A second, small globe dedicated to this view.
  useEffect(() => {
    if (!mount.current) return; let disposed = false;
    const v = new C.Viewer(mount.current, {
      baseLayer: false, animation: false, timeline: false, baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
      navigationHelpButton: false, fullscreenButton: false, selectionIndicator: false, infoBox: false, creditContainer: credits.current!,
      requestRenderMode: true, maximumRenderTimeChange: Infinity, skyBox: false,
      contextOptions: { webgl: { alpha: false, powerPreference: 'high-performance' } },
    });
    viewer.current = v;
    v.scene.backgroundColor = C.Color.BLACK; v.scene.globe.baseColor = C.Color.fromCssColorString('#0c0d0b');
    v.scene.globe.maximumScreenSpaceError = 1.5; v.scene.highDynamicRange = false;
    v.scene.screenSpaceCameraController.enableInputs = false;
    v.imageryLayers.addImageryProvider(new C.UrlTemplateImageryProvider({ url: `${MAPS}/imagery/{z}/{y}/{x}`, tilingScheme: new C.WebMercatorTilingScheme(), maximumLevel: 19, credit: new C.Credit('Esri World Imagery') }));
    const palettes = new C.PostProcessStage({ fragmentShader: PALETTE_SHADER, uniforms: { ironbow: 1 } });
    palettes.enabled = false; v.scene.postProcessStages.add(palettes); stage.current = palettes;
    void C.ArcGISTiledElevationTerrainProvider.fromUrl('/api/connector/terrain').then(t => { if (!disposed && !v.isDestroyed()) { v.terrainProvider = t; v.scene.requestRender(); } }).catch(() => {});
    setReady(true);
    return () => { disposed = true; layers.current = {}; stage.current = null; if (!v.isDestroyed()) v.destroy(); viewer.current = null; };
  }, []);

  // Draped layers for each mode.
  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return; let cancelled = false;
    for (const [k, src] of [['eo', eo], ['sar', sar], ['change', change]] as const) {
      if (!src || layers.current[k]) continue;
      void C.SingleTileImageryProvider.fromUrl(src.url, { rectangle: C.Rectangle.fromDegrees(...src.bounds) }).then(p => {
        if (cancelled || v.isDestroyed()) return; const l = v.imageryLayers.addImageryProvider(p);
        if (k !== 'eo') l.magnificationFilter = C.TextureMagnificationFilter.NEAREST;
        l.show = false; layers.current[k] = l; v.scene.requestRender(); apply();
      }).catch(() => {});
    }
    const apply = () => {
      const ls = layers.current;
      if (ls.eo) ls.eo.show = mode === 'eo';
      if (ls.sar) ls.sar.show = mode === 'sar';
      if (ls.change) { ls.change.show = mode === 'change'; ls.change.alpha = 0.92; }
      if (stage.current) { stage.current.enabled = mode === 'sar'; stage.current.uniforms.ironbow = palette === 'ironbow' ? 1 : 0; }
      v.scene.requestRender();
    };
    apply();
    return () => { cancelled = true; };
  }, [ready, eo?.url, sar?.url, change?.url, mode, palette]);

  // Point the camera: from the satellite toward the area, plus any gimbal offset, through a narrow field of view.
  useEffect(() => {
    const v = viewer.current; if (!v || !geo) return;
    const pos = C.Cartesian3.fromDegrees(geo.p.lon, geo.p.lat, geo.h * 1000);
    const h = v.scene.globe.getHeight(C.Cartographic.fromDegrees(target.lon, target.lat)) ?? 0;
    const tgt = C.Cartesian3.fromDegrees(target.lon, target.lat, h);
    const dir = C.Cartesian3.normalize(C.Cartesian3.subtract(tgt, pos, new C.Cartesian3()), new C.Cartesian3());
    const surfaceUp = C.Ellipsoid.WGS84.geodeticSurfaceNormal(pos, new C.Cartesian3());
    const right = C.Cartesian3.normalize(C.Cartesian3.cross(dir, surfaceUp, new C.Cartesian3()), new C.Cartesian3());
    const up = C.Cartesian3.cross(right, dir, new C.Cartesian3());
    // Gimbal offsets rotate the line of sight about the camera's up and right axes.
    const yaw = C.Quaternion.fromAxisAngle(up, -aim.az / DEG), pitch = C.Quaternion.fromAxisAngle(right, aim.el / DEG);
    const rot = C.Matrix3.fromQuaternion(C.Quaternion.multiply(yaw, pitch, new C.Quaternion()));
    const d2 = C.Matrix3.multiplyByVector(rot, dir, new C.Cartesian3()), u2 = C.Matrix3.multiplyByVector(rot, up, new C.Cartesian3());
    v.camera.setView({ destination: pos, orientation: { direction: d2, up: u2 } });
    const f = v.camera.frustum as C.PerspectiveFrustum; f.fov = Math.min(60, geo.fov / zoom) / DEG; f.near = Math.max(1, geo.range * 1000 * 0.5); f.far = geo.range * 1000 * 2;
    v.scene.requestRender();
  }, [geo, zoom, aim, target.lat, target.lon, ready]);

  // The sensor point of interest: the ground under the crosshair, re-read each second as terrain loads.
  useEffect(() => {
    const read = () => {
      const v = viewer.current; if (!v || v.isDestroyed()) return;
      const ray = v.camera.getPickRay(new C.Cartesian2(v.canvas.clientWidth / 2, v.canvas.clientHeight / 2));
      const hit = (ray && v.scene.globe.pick(ray, v.scene)) || v.camera.pickEllipsoid(new C.Cartesian2(v.canvas.clientWidth / 2, v.canvas.clientHeight / 2));
      if (!hit) return; const c = C.Cartographic.fromCartesian(hit);
      // Elevation is shown only once the terrain under the view has fully loaded, so a coarse tile never reads as fact.
      setSpi({ lon: c.longitude * DEG, lat: c.latitude * DEG, h: v.scene.globe.tilesLoaded ? v.scene.globe.getHeight(c) ?? null : null });
    };
    const id = setInterval(() => { setClock(c => c + 1); read(); }, 1000); read();
    return () => clearInterval(id);
  }, [aim, zoom]);

  // Drag to slew the gimbal, wheel to zoom.
  const drag = useRef<{ x: number; y: number; az: number; el: number } | null>(null);
  const fovNow = geo ? Math.min(60, geo.fov / zoom) : 1;
  const onDown = (e: React.PointerEvent) => { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); drag.current = { x: e.clientX, y: e.clientY, az: aim.az, el: aim.el }; };
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current; const el = mount.current; if (!d || !el) return;
    const k = fovNow / el.clientWidth;
    setAim({ az: C.Math.clamp(d.az - (e.clientX - d.x) * k, -fovNow * 3, fovNow * 3), el: C.Math.clamp(d.el + (e.clientY - d.y) * k, -fovNow * 3, fovNow * 3) });
  };
  const onWheel = (e: React.WheelEvent) => setZoom(z => C.Math.clamp(z * (e.deltaY < 0 ? 1.15 : 1 / 1.15), 0.5, 12));

  if (!geo) return null;
  const heading = (geo.look + aim.az + 360) % 360;
  const swath = SWATH[sat.norad];
  const inSwath = swath ? geo.nadirKm >= swath.nearKm && geo.nadirKm <= swath.farKm && (geo.side < 0) === (swath.side === 'left') : null;
  const gimbal = -(90 - geo.offNadir) + aim.el;
  const when = new Date(at);
  const rel = at - Date.now();
  const span = Math.abs(rel), days = Math.floor(span / 86_400_000), hours = Math.floor(span % 86_400_000 / 3_600_000), mins = Math.floor(span % 3_600_000 / 60_000);
  const relText = span < 60_000 ? 'NOW' : `${rel > 0 ? 'IN ' : ''}${days ? `${days} D ${hours} H` : `${hours} H ${mins} MIN`}${rel < 0 ? ' AGO' : ''}`;
  const sensor = mode === 'eo' ? `EO · ${eo ? 'SENTINEL-2' : 'BASEMAP'}` : mode === 'sar' ? `SAR · ${palette === 'ironbow' ? 'IRONBOW' : 'GREY'}` : 'Δ · CHANGE';
  const fmtClock = `${Math.floor(clock / 60)}:${String(clock % 60).padStart(2, '0')}`;
  const ticks: number[] = []; for (let a = Math.floor((heading - 40) / 5) * 5; a <= heading + 40; a += 5) ticks.push(a);

  return <div className={`sensor ${expanded ? 'expanded' : ''} ${mode}`} role="region" aria-label={`Simulated view from ${sat.name}`}>
    <div className="sensor-screen" onPointerDown={onDown} onPointerMove={onMove} onPointerUp={() => { drag.current = null; }} onWheel={onWheel}>
      <div ref={mount} className="sensor-canvas" />
      <div className="hud" aria-hidden="true">
        <i className="br tl" /><i className="br tr" /><i className="br bl" /><i className="br bottom-r" />
        <div className="hud-tl"><span><b className="rec" />SIM {fmtClock}</span><span>{sat.name.toUpperCase()}</span><span className="dim">{sensor}</span></div>
        <div className="hud-tr"><span>{when.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' }).toUpperCase()}, {when.toISOString().slice(11, 16)} UTC</span><span className="dim">PASS {relText}</span></div>
        <div className="hud-tape">
          <div className="tape-ticks">{ticks.map(a => { const x = 50 + (a - heading) * 1.2; const n = ((a % 360) + 360) % 360;
            return <span key={a} style={{ left: `${x}%` }} className={n % 30 === 0 ? 'major' : n % 10 === 0 ? 'mid' : ''}>{n % 30 === 0 && <em>{n % 90 === 0 ? COMPASS[n / 45] : n}</em>}</span>; })}</div>
          <b className="tape-mark">▲</b><b className="tape-value">{Math.round(heading)}°</b>
        </div>
        <div className="hud-pitch"><span className="pitch-label">LOOK</span><div className="pitch-scale"><i style={{ top: `${C.Math.clamp(-gimbal / 90, 0, 1) * 100}%` }}><em>{Math.round(gimbal)}°</em></i></div></div>
        <div className="hud-cross"><i /><i /></div>
        <div className="hud-bl"><span>ALT {Math.round(geo.h)} KM</span><span>GS {geo.ground.toFixed(1)} KM/S</span><span>HDG {Math.round(geo.track)}°</span></div>
        <div className="hud-bc"><span>SPI {dmsPretty(spi?.lat ?? target.lat, spi?.lon ?? target.lon).toUpperCase()}</span>
          <span className="dim">RNG {Math.round(geo.range)} KM · ELEV {spi?.h == null ? '—' : `${Math.round(spi.h).toLocaleString('en')} M`} · INC {geo.incidence.toFixed(0)}°</span></div>
        <div className="hud-br"><span>OFF-NADIR {geo.offNadir.toFixed(1)}°</span><span>LOOK {geo.side < 0 ? 'L' : 'R'} · {Math.round(geo.nadirKm)} KM</span><span>ZOOM {zoom.toFixed(1)}× · {fovNow < 1 ? fovNow.toFixed(2) : fovNow.toFixed(1)}° FOV</span>
          {inSwath !== null && <span className={inSwath ? 'ok' : 'warn'} title={swath?.label}>{inSwath ? 'IN SWATH' : 'OUTSIDE SWATH'}</span>}</div>
      </div>
    </div>
    <div ref={credits} className="sensor-credits" aria-label="Imagery and data credits" />
    <div className="sensor-bar">
      <b>{sat.name}</b><span className="dim">{sat.group}</span>
      <div className="sensor-modes" role="group" aria-label="Sensor view">
        <button className={mode === 'eo' ? 'on' : ''} onClick={() => setMode('eo')} title="True colour: Sentinel-2 from this investigation, else the basemap">EO</button>
        <button className={mode === 'sar' ? 'on sar' : ''} disabled={!sar} onClick={() => setMode('sar')} title={sar ? 'NISAR radar backscatter from this investigation' : 'Run an investigation to drape radar here'}>SAR</button>
        <button className={mode === 'change' ? 'on' : ''} disabled={!change} onClick={() => setMode('change')} title={change ? 'Backscatter change from this investigation' : 'Appears for two-date comparisons'}>Δ</button>
      </div>
      <button className="sensor-palette" disabled={mode !== 'sar'} onClick={() => setPalette(p => p === 'ironbow' ? 'gray' : 'ironbow')}>{palette === 'ironbow' ? 'Ironbow' : 'Grey'}</button>
      <span className="grow" />
      <button className="icon-btn" onClick={() => setZoom(z => Math.max(0.5, z / 1.5))} aria-label="Zoom out"><Minus size={14} /></button>
      <span className="num zoom-read">{zoom.toFixed(1)}×</span>
      <button className="icon-btn" onClick={() => setZoom(z => Math.min(12, z * 1.5))} aria-label="Zoom in"><Plus size={14} /></button>
      <button className="icon-btn" onClick={() => { setAim({ az: 0, el: 0 }); setZoom(1); }} aria-label="Re-centre on the area" title="Re-centre"><Crosshair size={14} /></button>
      <button className="icon-btn" onClick={onExpand} aria-label={expanded ? 'Shrink' : 'Expand'}>{expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}</button>
      <button className="icon-btn" onClick={onClose} aria-label="Close sensor view"><X size={14} /></button>
    </div>
  </div>;
}
