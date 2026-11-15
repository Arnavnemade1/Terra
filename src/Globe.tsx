import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import * as C from 'cesium';
import type { Bounds, Observation, Region } from './types';
import { DisplayModes, type DisplayMode } from './displayModes';
import { ACCENT, LiveLayers, type FeedState, type LayerKey, type Selection } from './liveLayers';
import { createStarSky } from './sky';
import { Relief, type Grid, type ReliefOptions, type TerrainFeature, type TerrainReading } from './relief';
import { DroneLayer } from './droneLayer';
import { droneStore } from './drones';
import { imagery as tileSource, terrainUrl, googleTilesUrl } from './tiles';
import { installSmoothCamera } from './smoothCamera';

export type GlobeHandle = {
  home: () => void; flyTo: (b: Bounds) => void; flyToPoint: (lon: number, lat: number, heightM?: number) => void;
  zoom: (direction: number) => void; north: () => void; tilt: () => void; screenshot: () => void;
  /** Frame the area of interest, magnified `factor` times relative to a full view of it. */
  frame: (factor: number) => void;
  /** Bring a drone into view from behind and above. */
  followDrone: (id: string) => void; stopFollowing: () => void; following: () => string | null;
  /** The exact ground point under an open drone camera's crosshair (for its line of sight on the map). */
  droneSight: (p: { id: string; lon: number; lat: number } | null) => void;
};
export type Telemetry = {
  lat: number; lon: number; alt: number; heading: number; pitch: number; centerLat: number | null; centerLon: number | null;
  metersPerPixel: number | null; fps: number; terrain: boolean; imagery: string; photoreal: 'unavailable' | 'standby' | 'active';
  nisar: { lat: number; lon: number; altKm: number; speedKms: number } | null; approach: { at: Date; km: number } | null;
  inView: { quakes: number; events: number; frames: number } | null; counts: LiveLayers['counts'];
  /** The visible extent in degrees (west, south, east, north), when the view is not the whole globe. */
  view: Bounds | null;
};
type Props = {
  region: Region; observations: Observation[]; selected?: Observation; footprints: boolean; terrain: boolean; lighting: boolean;
  labels: boolean; photoreal: boolean; quality: string; picking: boolean; overlays: Overlay[]; imageryAlpha: number; labelAlpha: number; exaggeration: number;
  mode: DisplayMode; layers: Record<LayerKey, boolean>; satTime?: number | null; satFocus?: number | null;
  marks?: Mark[]; flat:boolean; profiling:boolean; profileVertices:[number,number][]; onPathPoint:(lon:number,lat:number)=>void;
  deploy?: { stage: 'draw' | 'confirm'; area: Bounds | null; pad: [number, number] | null } | null;  // drawing a deployment
  onDeployArea?: (b: Bounds) => void; onDeployPad?: (lon: number, lat: number) => void;
  /** The stylised relief map (3D only) and the land cover and named features that dress it. */
  relief: boolean; reliefOptions: ReliefOptions; terrainContext: { grid: Grid | null; features: TerrainFeature[] } | null;
  onHover?: (h: Hover | null) => void;
  drones: boolean; selectedDrone: string | null; onSelectDrone: (id: string) => void;
  onPick: (bbox: Bounds) => void; onQuickPick: (lon: number, lat: number) => void; onSelect: (s: Selection | null) => void; onProbe: (lon: number, lat: number) => void;
  onTelemetry: (t: Telemetry) => void; onFeeds: (s: Record<string, FeedState>) => void; onReady: () => void;
  /** Google Photorealistic 3D Tiles could not be loaded (key missing, API disabled, quota). */
  onPhotorealError?: (message: string) => void;
};

/** What lies under the pointer: the terrain reading plus the marked area (if any) it falls in. */
export type Hover = TerrainReading & { lon: number; lat: number; mark: Mark | null; metresPerPixel: number };
/** Outlined areas drawn on the ground with a callout, such as change hotspots. */
export type Mark = { id: string; bounds: Bounds; label: string; color: string; active?: boolean; centre?: [number, number] };
/** A georeferenced image drawn on the ground (radar, change, true colour), with its own opacity. */
export type Overlay = { id: string; url: string; bounds: Bounds; alpha: number; show: boolean; smooth?: boolean };

const HOME_HEIGHT = 17_500_000;
const MAPS = '/api/connector/maps';
const GIBS = '/api/connector/gibs';
const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
const css = (value: string, alpha = 1) => C.Color.fromCssColorString(value).withAlpha(alpha);
const PHOTOREAL_BELOW = 300_000; // metres: Google 3D tiles take over from the imagery globe below this altitude
/** Served by the server connector (backend/terra/google_tiles.py), which attaches the key; none enters this bundle. */
// Google Maps attribution: the logo on a busy background, 16–19 px tall, never shrunk or faded (Map Tiles API policies).
const googleCredit = () => new C.Credit('<img class="google-logo" alt="Google" src="/attribution/google-on-dark.png" width="98" height="18">', true);
const tileError = (q: string) => q === 'high' ? 8 : q === 'low' ? 24 : 16;

function obliqueView(b: Bounds) {
  const rect = C.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]);
  const sphere = C.BoundingSphere.fromRectangle3D(rect, C.Ellipsoid.WGS84, 0);
  return { sphere, offset: new C.HeadingPitchRange(0, C.Math.toRadians(-62), Math.max(sphere.radius * 3.1, 6000)) };
}

const Globe = forwardRef<GlobeHandle, Props>(function Globe(props, ref) {
  const mount = useRef<HTMLDivElement>(null);
  const credits = useRef<HTMLDivElement>(null);
  const viewer = useRef<C.Viewer | null>(null);
  const latest = useRef(props); latest.current = props;
  const baseError = useRef(2);
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState('');
  const terrainProvider = useRef<C.TerrainProvider | undefined>(undefined);
  const flatLayers = useRef<C.ImageryLayer[]>([]);
  const baseLayer = useRef<C.ImageryLayer | null>(null);
  const overlayLayers = useRef(new Map<string, { url: string; layer: C.ImageryLayer }>());
  const labelLayer = useRef<C.ImageryLayer | null>(null);
  const nightLayer = useRef<C.ImageryLayer | null>(null);
  const live = useRef<LiveLayers | null>(null);
  const modes = useRef<DisplayModes | null>(null);
  const retune = useRef<() => void>(() => {});
  const tiles = useRef<C.Cesium3DTileset | null>(null);
  const tilesLoading = useRef(false);
  const tilesHaveView = useRef(false);
  const [googleLoading,setGoogleLoading]=useState(false);
  const relief = useRef<Relief | null>(null);
  const drones = useRef<DroneLayer | null>(null);
  const metresPerPixel = useRef(30);

  const homeDestination = () => { const b = latest.current.region.bbox; return C.Cartesian3.fromDegrees((b[0] + b[2]) / 2, (b[1] + b[3]) / 2 * 0.5, HOME_HEIGHT); };
  const home = () => { drones.current?.follow(null); const v = viewer.current; if (v) v.camera.flyTo({ destination: homeDestination(), orientation: { heading: 0, pitch: -C.Math.PI_OVER_TWO, roll: 0 }, duration: reducedMotion() ? 0 : 2.2, easingFunction: C.EasingFunction.QUADRATIC_IN_OUT }); };
  const flyTo = (b: Bounds) => { drones.current?.follow(null);
    const v = viewer.current; if (!v) return; const { sphere, offset } = obliqueView(b);
    v.camera.flyToBoundingSphere(sphere, { offset, duration: reducedMotion() ? 0 : 2.8, easingFunction: C.EasingFunction.CUBIC_IN_OUT, maximumHeight: Math.min(Math.max(v.camera.positionCartographic.height, sphere.radius * 6), 9_000_000) });
  };

  useImperativeHandle(ref, () => ({
    home, flyTo,
    frame: (factor) => {
      drones.current?.follow(null); const v = viewer.current; if (!v) return; const { sphere, offset } = obliqueView(latest.current.region.bbox);
      const flat = v.scene.mode === C.SceneMode.SCENE2D;
      v.camera.flyToBoundingSphere(sphere, { offset: new C.HeadingPitchRange(flat || !Number.isFinite(v.camera.heading) ? 0 : v.camera.heading, flat ? -C.Math.PI_OVER_TWO : offset.pitch, offset.range / Math.max(0.25, factor)), duration: reducedMotion() ? 0 : 1.2 });
    },
    flyToPoint: (lon, lat, height = 900_000) => { drones.current?.follow(null); const v = viewer.current; if (v) v.camera.flyTo({ destination: C.Cartesian3.fromDegrees(lon, lat, height), duration: reducedMotion() ? 0 : 2.4, easingFunction: C.EasingFunction.CUBIC_IN_OUT }); },
    zoom: (d) => { const v = viewer.current; if (!v) return; v.camera.zoomIn(d * v.camera.positionCartographic.height * 0.4); v.scene.requestRender(); },
    north: () => { drones.current?.follow(null); const v = viewer.current; if (v) v.camera.flyTo({ destination: v.camera.position, orientation: { heading: 0, pitch: v.camera.pitch, roll: 0 }, duration: 0.7 }); },
    tilt: () => {
      drones.current?.follow(null); const v = viewer.current; if (!v) return;
      const ray = v.camera.getPickRay(new C.Cartesian2(v.canvas.clientWidth / 2, v.canvas.clientHeight / 2)); const target = ray && v.scene.globe.pick(ray, v.scene);
      if (!target) return; const range = C.Cartesian3.distance(v.camera.position, target);
      const straight = v.camera.pitch < -1.35;
      v.camera.flyToBoundingSphere(new C.BoundingSphere(target, 1), { offset: new C.HeadingPitchRange(v.camera.heading, straight ? C.Math.toRadians(-38) : -C.Math.PI_OVER_TWO, range), duration: 1.1 });
    },
    // Fly with the aircraft: a chase camera that follows its drawn position every frame (DroneLayer.follow).
    followDrone: (id) => { drones.current?.follow(id); },
    stopFollowing: () => { drones.current?.follow(null); },
    following: () => drones.current?.following() ?? null,
    droneSight: (p) => { if (drones.current) drones.current.sightOverride = p; },
    screenshot: () => { const v = viewer.current; if (!v) return; v.render(); const a = document.createElement('a'); a.download = `terra-${Date.now()}.png`; a.href = v.canvas.toDataURL('image/png'); a.click(); },
  }));

  // Deploying the fleet: drag (or click two corners) to draw the perimeter, then click to place the launch pad.
  const deployStage = props.deploy?.stage ?? null;
  useEffect(() => {
    const v = viewer.current; if (!v || !ready || !deployStage) return;
    const scene = v.scene; const ctrl = scene.screenSpaceCameraController;
    const ground = (p: C.Cartesian2) => { const ray = v.camera.getPickRay(p); const hit = ray && (scene.globe.pick(ray, scene) ?? v.camera.pickEllipsoid(p)); return hit ? C.Cartographic.fromCartesian(hit) : undefined; };
    const deg = (c: C.Cartographic): [number, number] => [C.Math.toDegrees(c.longitude), C.Math.toDegrees(c.latitude)];
    const handler = new C.ScreenSpaceEventHandler(v.canvas);
    let start: { at: [number, number]; screen: C.Cartesian2 } | null = null; let now: [number, number] | null = null;
    const box = (a: [number, number], b: [number, number]): Bounds => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
    const preview = v.entities.add({ id: 'deploy-draft', rectangle: { coordinates: new C.CallbackProperty(() => start && now ? C.Rectangle.fromDegrees(...box(start.at, now)) : C.Rectangle.fromDegrees(0, 0, 0, 0), false),
      material: C.Color.fromCssColorString(ACCENT).withAlpha(0.14), outline: false, show: new C.CallbackProperty(() => Boolean(start && now), false) } as unknown as C.RectangleGraphics.ConstructorOptions });
    const edge = v.entities.add({ id: 'deploy-draft-edge', polyline: { positions: new C.CallbackProperty(() => { if (!start || !now) return []; const [w, s2, e, n] = box(start.at, now); return C.Cartesian3.fromDegreesArray([w, s2, e, s2, e, n, w, n, w, s2]); }, false),
      width: 2, clampToGround: true, material: C.Color.fromCssColorString(ACCENT) } });
    if (deployStage === 'draw') {
      ctrl.enableInputs = false; v.canvas.style.cursor = 'crosshair';
      handler.setInputAction((e: { position: C.Cartesian2 }) => { const c = ground(e.position); if (!c) return; if (!start) start = { at: deg(c), screen: C.Cartesian2.clone(e.position) }; now = deg(c); scene.requestRender(); }, C.ScreenSpaceEventType.LEFT_DOWN);
      handler.setInputAction((e: { endPosition: C.Cartesian2 }) => { if (!start) return; const c = ground(e.endPosition); if (c) { now = deg(c); scene.requestRender(); } }, C.ScreenSpaceEventType.MOUSE_MOVE);
      handler.setInputAction((e: { position: C.Cartesian2 }) => {
        if (!start) return; const c = ground(e.position); if (c) now = deg(c);
        const dragged = C.Cartesian2.distance(start.screen, e.position) > 8;
        if (!dragged && (!now || (now[0] === start.at[0] && now[1] === start.at[1]))) return;  // first corner of a two-click box: keep drawing
        const b = box(start.at, now!); start = null; now = null;
        const wM = (b[2] - b[0]) * 111_320 * Math.cos((b[1] + b[3]) / 2 * Math.PI / 180), hM = (b[3] - b[1]) * 110_574;
        if (wM > 150 && hM > 150) latest.current.onDeployArea?.(b);
        scene.requestRender();
      }, C.ScreenSpaceEventType.LEFT_UP);
    } else {
      handler.setInputAction((e: { position: C.Cartesian2 }) => { const c = ground(e.position); if (c) latest.current.onDeployPad?.(...deg(c)); }, C.ScreenSpaceEventType.LEFT_CLICK);
    }
    return () => { handler.destroy(); v.entities.remove(preview); v.entities.remove(edge); if (!v.isDestroyed()) { ctrl.enableInputs = true; v.canvas.style.cursor = ''; scene.requestRender(); } };
  }, [ready, deployStage]);

  // The drawn perimeter and launch pad while a deployment is being confirmed.
  const deployArea = props.deploy?.area?.join(','); const deployPad = props.deploy?.pad?.join(',');
  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return;
    const area = props.deploy?.area, pad = props.deploy?.pad; const made: C.Entity[] = [];
    if (area) {
      const [w, s2, e, n] = area;
      made.push(v.entities.add({ rectangle: { coordinates: C.Rectangle.fromDegrees(w, s2, e, n), material: C.Color.fromCssColorString(ACCENT).withAlpha(0.1) } }));
      made.push(v.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray([w, s2, e, s2, e, n, w, n, w, s2]), width: 2.5, clampToGround: true,
        material: new C.PolylineDashMaterialProperty({ color: C.Color.fromCssColorString(ACCENT), dashLength: 16 }) } }));
    }
    if (pad) made.push(v.entities.add({ position: C.Cartesian3.fromDegrees(pad[0], pad[1]), point: { pixelSize: 14, color: C.Color.fromCssColorString('#f4f1e8'), outlineColor: C.Color.fromCssColorString(ACCENT), outlineWidth: 3,
      heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY }, label: { text: 'LAUNCH', font: '600 11px "IBM Plex Mono", monospace', pixelOffset: new C.Cartesian2(0, -20),
      fillColor: C.Color.fromCssColorString('#f4f1e8'), showBackground: true, backgroundColor: C.Color.fromCssColorString('#15161399'), heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY } }));
    v.scene.requestRender();
    return () => { if (!v.isDestroyed()) { made.forEach(e => v.entities.remove(e)); v.scene.requestRender(); } };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, deployArea, deployPad]);

  // Keep the map controls above the attribution strip, however many lines the credits wrap to: attribution
  // (Google's logo in particular) must never be covered.
  useEffect(() => {
    const el = credits.current; const area = mount.current?.parentElement; if (!el || !area) return;
    const fit = () => area.style.setProperty('--chrome-bottom', `${Math.max(30, Math.ceil(el.getBoundingClientRect().height) + 10)}px`);
    const observer = new ResizeObserver(fit); observer.observe(el); fit();
    return () => { observer.disconnect(); area.style.removeProperty('--chrome-bottom'); };
  }, []);

  useEffect(() => {
    let disposed = false; let v: C.Viewer | undefined; let handler: C.ScreenSpaceEventHandler | undefined;
    let telemetryTimer: ReturnType<typeof setInterval> | undefined; let clockTimer: ReturnType<typeof setInterval> | undefined;
    let terrainReady = false; let imagery = 'CONNECTING'; let frames = 0; let last = performance.now(); let smoothOff: (() => void) | null = null;
    try {
      C.Ion.defaultAccessToken = '';
      v = new C.Viewer(mount.current!, {
        baseLayer: false, animation: false, timeline: false, baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
        navigationHelpButton: false, fullscreenButton: false, selectionIndicator: false, infoBox: false, creditContainer: credits.current!,
        requestRenderMode: true, maximumRenderTimeChange: Infinity, skyBox: createStarSky(),
        contextOptions: { webgl: { preserveDrawingBuffer: true, alpha: false, powerPreference: 'high-performance' } }, msaaSamples: 4,
      });
      const viewerRef = v; viewer.current = v;
      if (import.meta.env.DEV) Object.assign(window, { __terraViewer: v, __terraCesium: C });
      const { scene, camera } = v; const globe = scene.globe;
      v.clock.currentTime = C.JulianDate.now(); v.clock.shouldAnimate = false;
      clockTimer = setInterval(() => { viewerRef.clock.currentTime = C.JulianDate.now(); }, 60_000);

      scene.backgroundColor = C.Color.BLACK;
      scene.highDynamicRange = false; // HDR tone mapping washed out the imagery; LDR keeps the Esri colour true
      scene.postProcessStages.fxaa.enabled = true;
      globe.baseColor = css('#06121c');
      globe.showGroundAtmosphere = true;
      // Cesium's own default error budget, and no sibling preloading: about half the tiles for the same view.
      globe.maximumScreenSpaceError = 2;
      globe.tileCacheSize = 200;
      globe.loadingDescendantLimit = 8;
      globe.preloadAncestors = true; globe.preloadSiblings = false;
      globe.lightingFadeOutDistance = 6_500_000; globe.lightingFadeInDistance = 14_000_000;
      globe.nightFadeOutDistance = 6_500_000; globe.nightFadeInDistance = 14_000_000;
      if (scene.skyAtmosphere) { scene.skyAtmosphere.show = true; scene.skyAtmosphere.atmosphereLightIntensity = 18; scene.skyAtmosphere.saturationShift = -0.12; scene.skyAtmosphere.brightnessShift = -0.06; }
      scene.fog.enabled = true; scene.fog.density = 1.4e-4; scene.fog.minimumBrightness = 0.06;
      v.resolutionScale = Math.min(window.devicePixelRatio, 1.5);
      const controller = scene.screenSpaceCameraController;
      controller.minimumZoomDistance = 40; controller.maximumZoomDistance = 45_000_000; controller.enableCollisionDetection = true;
      // Eased wheel zoom toward the cursor, softer inertia, coarser tiles while moving (smoothCamera.ts).
      smoothOff = installSmoothCamera(v, { baseError: () => baseError.current });
      // Trackpad pinch arrives as ctrl+wheel; let it zoom like the wheel.
      controller.zoomEventTypes = [C.CameraEventType.WHEEL, C.CameraEventType.PINCH, { eventType: C.CameraEventType.WHEEL, modifier: C.KeyboardEventModifier.CTRL }];

      const initial=latest.current.region.bbox;
      camera.setView({destination:C.Rectangle.fromDegrees(...initial),orientation:{heading:0,pitch:-C.Math.PI_OVER_TWO,roll:0}});
      scene.renderError.addEventListener((_s, err) => { if (!disposed) setError(`The renderer stopped: ${String((err as Error)?.message || err)}`); });
      scene.postRender.addEventListener(() => { frames++; });

      const tune = () => {
        const layer = labelLayer.current; const h = camera.positionCartographic.height;
        const t = tiles.current;
        // Science layers stay on the imagery globe so radar pixels sit exactly on the ground.
        const overlaid = latest.current.overlays.some(o => o.show && o.alpha > 0);
        if (t) {
          // Google tiles are drawn on their own: while they show, the globe (and with it every non-Google basemap) is hidden.
          const p = latest.current; const on = p.photoreal && !p.flat && !p.relief && !overlaid && scene.mode === C.SceneMode.SCENE3D && h < PHOTOREAL_BELOW;
          // Prepare only the view the user selected. The two basemaps never draw together.
          t.preloadWhenHidden=on&&!tilesHaveView.current;
          const showGoogle=on&&tilesHaveView.current;
          if(t.show!==showGoogle){t.show=showGoogle;scene.requestRender();}
          const showTerrain=!showGoogle;
          if(globe.show!==showTerrain){globe.show=showTerrain;scene.requestRender();}
        }
        // Labels step back while a science layer is shown so the measurements stay legible.
        if (layer) layer.alpha = C.Math.clamp((9_000_000 - h) / 5_000_000, 0, 0.9) * (overlaid ? 0.5 : 1) * latest.current.labelAlpha;
        const sunlit = latest.current.lighting && h > 5_000_000;
        if (nightLayer.current) nightLayer.current.show = sunlit;
        if (globe.enableLighting === sunlit) return;
        globe.enableLighting = sunlit; globe.dynamicAtmosphereLighting = sunlit;
        scene.atmosphere.dynamicLighting = sunlit ? C.DynamicAtmosphereLightingType.SUNLIGHT : C.DynamicAtmosphereLightingType.NONE;
        scene.requestRender();
      };
      retune.current = tune; globe.enableLighting = !latest.current.lighting; tune();
      scene.preRender.addEventListener(tune);

      modes.current = new DisplayModes(v); modes.current.set(latest.current.mode);
      live.current = new LiveLayers(v, s => { if (!disposed) latest.current.onFeeds(s); });
      for (const [k, on] of Object.entries(latest.current.layers)) live.current.setVisible(k as LayerKey, on);

      handler = new C.ScreenSpaceEventHandler(v.canvas);
      let first: C.Cartographic | undefined;
      const pickGround = (pos: C.Cartesian2) => {
        let p: C.Cartesian3 | undefined;
        if (globe.show) { const ray = camera.getPickRay(pos); p = ray ? globe.pick(ray, scene) : undefined; }
        else if (scene.pickPositionSupported) p = scene.pickPosition(pos);
        // In the 2D map (and before terrain tiles load) the ellipsoid is the reliable surface.
        if (!p) p = camera.pickEllipsoid(pos, scene.globe.ellipsoid);
        return p ? C.Cartographic.fromCartesian(p) : undefined;
      };
      handler.setInputAction((ev: { position: C.Cartesian2 }) => {
        const vv = viewer.current; if (!vv) return;
        if (latest.current.deploy) return;  // drawing a deployment: its own handler owns the clicks
        if(latest.current.profiling){const p=pickGround(ev.position);if(p)latest.current.onPathPoint(C.Math.toDegrees(p.longitude),C.Math.toDegrees(p.latitude));return;}
        if (latest.current.picking) {
          const cart = pickGround(ev.position); if (!cart) return;
          if (!first) {
            first = cart; vv.entities.removeById('selection-start');
            vv.entities.add({ id: 'selection-start', position: C.Cartographic.toCartesian(cart), point: { pixelSize: 8, color: css(ACCENT), outlineColor: C.Color.BLACK, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY } });
          } else {
            const lon1 = C.Math.toDegrees(first.longitude), lat1 = C.Math.toDegrees(first.latitude), lon2 = C.Math.toDegrees(cart.longitude), lat2 = C.Math.toDegrees(cart.latitude);
            first = undefined; vv.entities.removeById('selection-start');
            const bounds: Bounds = [Math.min(lon1, lon2), Math.min(lat1, lat2), Math.max(lon1, lon2), Math.max(lat1, lat2)];
            if (bounds[2] - bounds[0] >= 0.01 && bounds[3] - bounds[1] >= 0.01) latest.current.onPick(bounds);
          }
          vv.scene.requestRender(); return;
        }
        first = undefined;
        const picked = vv.scene.pick(ev.position);
        const pickedId = (picked as { id?: { kind?: string; droneId?: string } } | undefined)?.id;
        if (pickedId?.kind === 'drone' && pickedId.droneId) { latest.current.onSelectDrone(pickedId.droneId); return; }
        const selection = picked ? live.current?.describe((picked as { id?: unknown }).id) ?? null : null;
        if (selection) { latest.current.onSelect(selection); return; }
        // With a science layer shown, a plain click reads the computed value under the cursor.
        const shown = latest.current.overlays.filter(o => o.show && o.alpha > 0);
        const cart = shown.length ? pickGround(ev.position) : undefined;
        if (cart) {
          const lon = C.Math.toDegrees(cart.longitude), lat = C.Math.toDegrees(cart.latitude);
          if (shown.some(l => lon >= l.bounds[0] && lon <= l.bounds[2] && lat >= l.bounds[1] && lat <= l.bounds[3])) {
            vv.entities.removeById('probe');
            vv.entities.add({ id: 'probe', position: C.Cartesian3.fromDegrees(lon, lat), point: { pixelSize: 7, color: C.Color.WHITE, outlineColor: css('#080907'), outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY } });
            vv.scene.requestRender(); latest.current.onProbe(lon, lat); return;
          }
        }
        latest.current.onSelect(null);
      }, C.ScreenSpaceEventType.LEFT_CLICK);
      // Right-click anywhere: make that point the area of interest.
      handler.setInputAction((ev: { position: C.Cartesian2 }) => {
        const cart = pickGround(ev.position); if (cart) latest.current.onQuickPick(C.Math.toDegrees(cart.longitude), C.Math.toDegrees(cart.latitude));
      }, C.ScreenSpaceEventType.RIGHT_CLICK);
      let hoverAt = 0;
      handler.setInputAction((ev: { endPosition: C.Cartesian2 }) => {
        const vv = viewer.current; const now = performance.now(); if (!vv || now - hoverAt < 80) return; hoverAt = now;
        const picked = vv.scene.pick(ev.endPosition);
        if (latest.current.deploy) return;
        vv.canvas.style.cursor = latest.current.profiling || latest.current.picking || latest.current.overlays.some(o => o.show) ? 'crosshair' : picked && (picked as { id?: { kind?: string } }).id?.kind ? 'pointer' : '';
        const onHover = latest.current.onHover; if (!onHover) return;
        // Height, slope and land cover come from Terra's own terrain; nothing is measured from Google's 3D tiles.
        const cart = scene.mode === C.SceneMode.SCENE3D && globe.show && camera.positionCartographic.height < 400_000 ? pickGround(ev.endPosition) : undefined;
        if (!cart || !relief.current) { onHover(null); return; }
        const lon = C.Math.toDegrees(cart.longitude), lat = C.Math.toDegrees(cart.latitude);
        const mark = (latest.current.marks || []).find(m => lon >= m.bounds[0] && lon <= m.bounds[2] && lat >= m.bounds[1] && lat <= m.bounds[3]) || null;
        onHover({ lon, lat, mark, metresPerPixel: metresPerPixel.current, ...relief.current.read(lon, lat, metresPerPixel.current) });
      }, C.ScreenSpaceEventType.MOUSE_MOVE);
      v.canvas.addEventListener('mouseleave', () => latest.current.onHover?.(null));

      telemetryTimer = setInterval(() => {
        // Heading and pitch are undefined while the scene morphs between 2D and 3D.
        if (disposed || !v || scene.mode === C.SceneMode.MORPHING) return;
        const now = performance.now(); const pos = camera.positionCartographic;
        const w = v.canvas.clientWidth, h = v.canvas.clientHeight;
        const center = pickGround(new C.Cartesian2(w / 2, h / 2));
        let mpp: number | null = null;
        if (center) {
          const a = pickGround(new C.Cartesian2(w / 2 - 50, h / 2)), b = pickGround(new C.Cartesian2(w / 2 + 50, h / 2));
          if (a && b) { mpp = new C.EllipsoidGeodesic(a, b).surfaceDistance / 100; metresPerPixel.current = mpp; }
        }
        const l = live.current;
        latest.current.onTelemetry({
          lat: C.Math.toDegrees(pos.latitude), lon: C.Math.toDegrees(pos.longitude), alt: pos.height, heading: Number.isFinite(camera.heading) ? C.Math.toDegrees(camera.heading) : 0, pitch: Number.isFinite(camera.pitch) ? C.Math.toDegrees(camera.pitch) : -90,
          centerLat: center ? C.Math.toDegrees(center.latitude) : null, centerLon: center ? C.Math.toDegrees(center.longitude) : null, metersPerPixel: mpp,
          fps: Math.round(frames * 1000 / (now - last)), terrain: terrainReady, imagery,
          photoreal: !tiles.current ? 'unavailable' : tiles.current.show ? 'active' : 'standby',
          nisar: l?.nisarNow ? { lat: l.nisarNow.lat, lon: l.nisarNow.lon, altKm: l.nisarNow.altKm, speedKms: l.nisarNow.speedKms } : null,
          approach: l?.approach ?? null, inView: l?.inView(camera.computeViewRectangle()) ?? null, counts: l ? { ...l.counts } : { quakes: 0, fires: 0, events: 0, frames: 0, fleet: 0 },
          view: (() => { const r = camera.computeViewRectangle(); return r ? [C.Math.toDegrees(r.west), C.Math.toDegrees(r.south), C.Math.toDegrees(r.east), C.Math.toDegrees(r.north)] as Bounds : null; })(),
        });
        frames = 0; last = now;
      }, 250);

      // Terrain first and in parallel: no place on the globe can refine until its terrain tile has arrived.
      const terrainLoad = C.ArcGISTiledElevationTerrainProvider.fromUrl(terrainUrl());
      const providers = async () => {
        // Every provider request uses the same-origin server connector (spread over loopback names; see tiles.ts).
        // No credentials enter this bundle.
        const fallback = viewerRef.imageryLayers.addImageryProvider(new C.UrlTemplateImageryProvider({
          ...tileSource('gibs/BlueMarble_ShadedRelief_Bathymetry/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpg'),
          maximumLevel: 8, credit: new C.Credit('NASA GIBS · Blue Marble'),
        }));
        fallback.show = true;
        const provider = new C.UrlTemplateImageryProvider({...tileSource('maps/imagery/{z}/{y}/{x}'),
          tilingScheme: new C.WebMercatorTilingScheme(), maximumLevel: 19, credit: new C.Credit('Esri World Imagery · Esri and partners')});
        const base = viewerRef.imageryLayers.addImageryProvider(provider);
        base.nightAlpha = .18; imagery = 'Esri World Imagery'; baseLayer.current = base; base.alpha = latest.current.imageryAlpha;
        relief.current = new Relief(viewerRef, base, [fallback]);
        drones.current = new DroneLayer(viewerRef, droneStore);
        // True terrain height (the drawn terrain is exaggerated) for aircraft flying at a height above ground.
        droneStore.groundAt = (lon, lat) => { const h = globe.getHeight(C.Cartographic.fromDegrees(lon, lat)); return h === undefined ? undefined : h / (scene.verticalExaggeration || 1); };
        if (import.meta.env.DEV) Object.assign(window, { __terraRelief: relief.current });
        // The 2D map is cartographic rather than photographic: dark shaded relief, a dark land/water base and dark labels.
        const flatSource = (name: string, credit: string, level = 16) => viewerRef.imageryLayers.addImageryProvider(new C.UrlTemplateImageryProvider({
          ...tileSource(`maps/${name}/{z}/{y}/{x}`), tilingScheme: new C.WebMercatorTilingScheme(), maximumLevel: level, credit: new C.Credit(credit) }));
        const shaded = flatSource('relief-dark', 'Esri World Hillshade (dark)'), tint = flatSource('dark', 'Esri World Dark Gray Base');
        tint.alpha = 0.55; flatLayers.current = [shaded, tint];
        for (const l of flatLayers.current) l.show = latest.current.flat;
        let errors = 0;
        provider.errorEvent.addEventListener(() => { if (++errors > 5 && !disposed) {base.show=false;imagery='NASA Blue Marble';setNotice('High-resolution map imagery is unavailable. Showing NASA Blue Marble.');scene.requestRender();} });
        const night = viewerRef.imageryLayers.addImageryProvider(new C.UrlTemplateImageryProvider({ ...tileSource('gibs/VIIRS_Black_Marble/default/2016-01-01/GoogleMapsCompatible_Level8/{z}/{y}/{x}.png'), maximumLevel: 8, credit: new C.Credit('NASA GIBS · VIIRS Black Marble') }));
        night.dayAlpha = 0; night.nightAlpha = 1; night.brightness = 2.2; night.show = false; nightLayer.current = night;
        labelLayer.current = viewerRef.imageryLayers.addImageryProvider(new C.UrlTemplateImageryProvider({...tileSource('maps/labels/{z}/{y}/{x}'), tilingScheme:new C.WebMercatorTilingScheme(),maximumLevel:19,credit:new C.Credit('Esri places and borders')}));
        labelLayer.current.show = latest.current.labels;
        if (disposed) return;
        setReady(true); latest.current.onReady(); scene.requestRender();
        // Open on the selected landscape, keeping map and investigation in the same place.
        if(!new URLSearchParams(location.search).has('investigation'))flyTo(latest.current.region.bbox);
        try {
          const t = await terrainLoad;
          if (disposed) return; terrainProvider.current=t;terrainReady=true;if(latest.current.terrain)viewerRef.terrainProvider=t;scene.requestRender();
        } catch { if(!disposed)setNotice('Terrain is unavailable; imagery is shown on the WGS84 ellipsoid.'); }
      };
      void providers();
    } catch (err) { setError(err instanceof Error ? err.message : 'WebGL could not initialize.'); }
    return () => {
      disposed = true; clearInterval(telemetryTimer); clearInterval(clockTimer); handler?.destroy(); smoothOff?.();
      live.current?.destroy(); modes.current?.destroy(); relief.current?.destroy(); drones.current?.destroy(); live.current = null; modes.current = null; relief.current = null; drones.current = null;
      droneStore.groundAt = null;
      if (v && !v.isDestroyed()) v.destroy(); viewer.current = null; labelLayer.current = null; nightLayer.current = null; tiles.current = null; tilesLoading.current = false; tilesHaveView.current=false;
    };
  }, []);

  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return;
    for (const l of flatLayers.current) l.show = props.flat;
    if (baseLayer.current) baseLayer.current.show = !props.flat && !props.relief;
    if (labelLayer.current) labelLayer.current.show = props.labels;
    if (props.flat && v.scene.mode !== C.SceneMode.SCENE2D) {
      // Frame the area once the morph has finished: a top-down map of the area of interest.
      const done = v.scene.morphComplete.addEventListener(() => {
        done(); const vv = viewer.current; if (!vv || vv.isDestroyed()) return; const [w, s, e, n] = latest.current.region.bbox;
        const pw = (e - w) * 0.6, ph = (n - s) * 0.6; vv.camera.setView({ destination: C.Rectangle.fromDegrees(w - pw, s - ph, e + pw, n + ph) }); vv.scene.requestRender();
      });
      v.scene.morphTo2D(0);
    }
    else if (!props.flat && v.scene.mode !== C.SceneMode.SCENE3D) {
      const done = v.scene.morphComplete.addEventListener(() => { done(); flyTo(latest.current.region.bbox); });
      v.scene.morphTo3D(0);
    }
    v.scene.requestRender();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, props.flat]);
  useEffect(() => { const v = viewer.current; if (!v || !ready) return; if (baseLayer.current) baseLayer.current.alpha = props.imageryAlpha; retune.current(); v.scene.requestRender(); }, [ready, props.imageryAlpha, props.labelAlpha]);
  // The relief style replaces the photographic base in 3D; the 2D map keeps its own cartographic style.
  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return;
    if (baseLayer.current) baseLayer.current.show = !props.flat && !props.relief;
    relief.current?.set(props.relief && !props.flat, props.reliefOptions, props.lighting); v.scene.requestRender();
  }, [ready, props.relief, props.flat, props.reliefOptions, props.lighting]);
  useEffect(() => { if (ready) relief.current?.setContext(props.terrainContext?.grid ?? null, props.terrainContext?.features ?? []); }, [ready, props.terrainContext]);
  useEffect(() => { if (ready) drones.current?.setVisible(props.drones && !props.flat); }, [ready, props.drones, props.flat]);
  useEffect(() => { if (ready) drones.current?.setSelected(props.selectedDrone); }, [ready, props.selectedDrone]);
  useEffect(() => { const v = viewer.current; if (!v || !ready) return; v.scene.verticalExaggeration = props.exaggeration; v.scene.requestRender(); }, [ready, props.exaggeration]);
  useEffect(()=>{const v=viewer.current;if(!v||!ready)return;v.entities.values.filter(e=>e.id.startsWith('profile-')).forEach(e=>v.entities.remove(e));
    props.profileVertices.forEach((p,i)=>v.entities.add({id:`profile-point-${i}`,position:C.Cartesian3.fromDegrees(...p),point:{pixelSize:9,color:css('#b8603e'),outlineColor:C.Color.WHITE,outlineWidth:2,disableDepthTestDistance:Number.POSITIVE_INFINITY}}));
    if(props.profileVertices.length>1)v.entities.add({id:'profile-path',polyline:{positions:C.Cartesian3.fromDegreesArray(props.profileVertices.flat()),width:3,material:css('#b8603e'),clampToGround:true}});v.scene.requestRender();
  },[ready,props.profileVertices]);

  // Area of interest: corner brackets on the ground, a faint fill and a callout.
  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return;
    v.entities.values.filter(e => e.id.startsWith('geo-')).forEach(e => v.entities.remove(e));
    const [w, s, e, n] = props.region.bbox; const kx = (e - w) * 0.22, ky = (n - s) * 0.22;
    const corners: [number, number][][] = [[[w, s + ky], [w, s], [w + kx, s]], [[e - kx, s], [e, s], [e, s + ky]], [[e, n - ky], [e, n], [e - kx, n]], [[w + kx, n], [w, n], [w, n - ky]]];
    corners.forEach((c, i) => v.entities.add({ id: `geo-corner-${i}`, polyline: { positions: C.Cartesian3.fromDegreesArray(c.flat()), width: 3, clampToGround: true, material: css(ACCENT) } }));
    v.entities.add({ id: 'geo-aoi', polyline: { positions: C.Cartesian3.fromDegreesArray([w, s, e, s, e, n, w, n, w, s]), width: 1, clampToGround: true, material: css(ACCENT, 0.45) } });
    v.entities.add({ id: 'geo-label', position: C.Cartesian3.fromDegrees(w, n, 2500), label: {
      text: props.region.name, font: '650 13px "Archivo Variable", Arial, sans-serif', fillColor: css(ACCENT), outlineColor: css('#080907', 0.9), outlineWidth: 3,
      style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.BOTTOM,
      pixelOffset: new C.Cartesian2(2, -8), disableDepthTestDistance: 2_000_000 } });
    // Acquisition extents as outlines: stacked translucent fills would tint the imagery.
    if (props.footprints) props.observations.slice(0, 16).forEach((o, i) => {
      if (!o.bbox) return; const on = o.id === props.selected?.id; const [fw, fs, fe, fn] = o.bbox;
      v.entities.add({ id: `geo-footprint-${i}`, polyline: { positions: C.Cartesian3.fromDegreesArray([fw, fs, fe, fs, fe, fn, fw, fn, fw, fs]), width: on ? 2 : 1, clampToGround: true,
        material: on ? new C.PolylineDashMaterialProperty({ color: css('#f3efe4', 0.9), dashLength: 12 }) : new C.PolylineDashMaterialProperty({ color: css('#d8d4c8', 0.28), dashLength: 8 }) } });
    });
    live.current?.setAoi((w + e) / 2, (s + n) / 2);
    v.scene.requestRender();
  }, [ready, props.region, props.observations, props.footprints, props.selected]);

  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return;
    retune.current();
    v.terrainProvider = props.terrain && terrainProvider.current ? terrainProvider.current : new C.EllipsoidTerrainProvider();
    v.resolutionScale = props.quality === 'high' ? Math.min(window.devicePixelRatio, 2) : props.quality === 'low' ? 0.8 : Math.min(window.devicePixelRatio, 1.5);
    baseError.current = props.quality === 'high' ? 1.3 : props.quality === 'low' ? 4 : 2;
    v.scene.globe.maximumScreenSpaceError = baseError.current;
    v.scene.requestRender();
  }, [ready, props.terrain, props.lighting, props.quality, props.photoreal]);
  useEffect(() => { const v = viewer.current; if (v && labelLayer.current) { labelLayer.current.show = props.labels; v.scene.requestRender(); } }, [ready, props.labels]);

  // Google Photorealistic 3D Tiles, loaded the first time the style is chosen (each load starts a billed tileset session).
  useEffect(() => {
    const v = viewer.current; if (!v || !ready || !props.photoreal || tiles.current || tilesLoading.current) return;
    tilesLoading.current = true;setGoogleLoading(true);tilesHaveView.current=false;let failures=0;
    // Resource accepts `credits` at runtime (Cesium's own Google loader uses it); the published typings omit it.
    const resource = new C.Resource({ url: googleTilesUrl(), credits: [googleCredit()] } as unknown as C.Resource.ConstructorOptions);
    C.Cesium3DTileset.fromUrl(resource, { showCreditsOnScreen: true, enableCollision: true, maximumScreenSpaceError: tileError(latest.current.quality),
      cacheBytes: 128 * 1024 * 1024, maximumCacheOverflowBytes: 32 * 1024 * 1024, preloadWhenHidden: false, foveatedScreenSpaceError: true })
      .then(t => {
        tilesLoading.current = false;
        if (v.isDestroyed() || viewer.current !== v) { t.destroy(); return; }
        t.show = false; v.scene.primitives.add(t); tiles.current = t;
        t.initialTilesLoaded.addEventListener(()=>{if(v.isDestroyed()||viewer.current!==v)return;tilesHaveView.current=true;setGoogleLoading(false);retune.current();v.scene.requestRender();});
        t.tileLoad.addEventListener(()=>{failures=0;v.scene.requestRender();});
        t.tileFailed.addEventListener(() => {
          if (++failures === 12) { setGoogleLoading(false); const m = 'Google 3D tiles stopped loading. Showing terrain and imagery instead.'; setNotice(m); latest.current.onPhotorealError?.(m); }
        });
        retune.current(); v.scene.requestRender();
      })
      .catch(() => { tilesLoading.current = false;if(v.isDestroyed()||viewer.current!==v)return;setGoogleLoading(false);const m = 'Photorealistic 3D is unavailable right now. Showing terrain and imagery instead.'; setNotice(m); latest.current.onPhotorealError?.(m); });
  }, [ready, props.photoreal]);
  useEffect(() => { const t = tiles.current; if (t) { t.maximumScreenSpaceError = tileError(props.quality); viewer.current?.scene.requestRender(); } }, [props.quality]);
  useEffect(() => { retune.current(); viewer.current?.scene.requestRender(); }, [ready, props.photoreal, props.relief, props.flat]);
  useEffect(() => { modes.current?.set(props.mode); }, [props.mode]);
  useEffect(() => { const l = live.current; if (!l) return; for (const [k, on] of Object.entries(props.layers)) l.setVisible(k as LayerKey, on); }, [props.layers]);
  useEffect(() => { live.current?.setTime(props.satTime ?? null); }, [ready, props.satTime]);
  useEffect(() => { live.current?.setFocus(props.satFocus ?? null); }, [ready, props.satFocus]);

  // Georeferenced overlays (true colour below radar below change), each with its own opacity, beneath the labels.
  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return; let canceled = false;
    const current = overlayLayers.current; const wanted = new Map(props.overlays.map(o => [o.id, o]));
    for (const [id, item] of current) {
      const o = wanted.get(id);
      if (!o || o.url !== item.url) { v.imageryLayers.remove(item.layer, true); current.delete(id); }
    }
    const order = () => {
      for (const o of props.overlays) { const item = current.get(o.id); if (item) v.imageryLayers.raiseToTop(item.layer); }
      if (labelLayer.current) v.imageryLayers.raiseToTop(labelLayer.current);
    };
    for (const o of props.overlays) {
      const item = current.get(o.id);
      if (item) { item.layer.alpha = o.alpha; item.layer.show = o.show; continue; }
      void C.SingleTileImageryProvider.fromUrl(o.url, { rectangle: C.Rectangle.fromDegrees(...o.bounds), credit: new C.Credit('Terra investigation output') }).then(p => {
        if (canceled || v.isDestroyed() || current.has(o.id)) return;
        const layer = v.imageryLayers.addImageryProvider(p); layer.alpha = o.alpha; layer.show = o.show;
        // Radar pixels stay crisp (nearest); true-colour photographs are smoothed.
        if (!o.smooth) layer.magnificationFilter = C.TextureMagnificationFilter.NEAREST;
        current.set(o.id, { url: o.url, layer }); order(); retune.current(); v.scene.requestRender();
      }).catch(() => { if (!canceled) setNotice('A result layer did not load; its data is still in the investigation.'); });
    }
    if (!props.overlays.some(o => o.show)) v.entities.removeById('probe');
    order(); retune.current(); v.scene.requestRender();
    return () => { canceled = true; };
  }, [ready, props.overlays]);

  // Marked areas (change hotspots): a ground outline, a ringed marker at the centre and a dark callout.
  useEffect(() => {
    const v = viewer.current; if (!v || !ready) return;
    v.entities.values.filter(e => e.id.startsWith('mark-')).forEach(e => v.entities.remove(e));
    for (const [index, m] of (props.marks || []).entries()) {
      const [w, s, e, n] = m.bounds; const [lat, lon] = m.centre || [(s + n) / 2, (w + e) / 2];
      v.entities.add({ id: `mark-line-${m.id}`, polyline: { positions: C.Cartesian3.fromDegreesArray([w, s, e, s, e, n, w, n, w, s]), width: m.active ? 2.6 : 1.4,
        clampToGround: true, material: css(m.color, m.active ? 1 : 0.8) } });
      v.entities.add({ id: `mark-point-${m.id}`, position: C.Cartesian3.fromDegrees(lon, lat, 400), point: { pixelSize: m.active ? 10 : 8, color: css('#fff5e6'),
        outlineColor: css(m.color), outlineWidth: 3, disableDepthTestDistance: 2_000_000, distanceDisplayCondition: new C.DistanceDisplayCondition(0, 600_000) } });
      v.entities.add({ id: `mark-label-${m.id}`, position: C.Cartesian3.fromDegrees(lon, lat, 400), label: {
        text: m.label, font: '500 12px "Archivo Variable", Arial, sans-serif', fillColor: css('#f3efe4'), showBackground: true,
        backgroundColor: css('#141511', m.active ? 0.96 : 0.86), backgroundPadding: new C.Cartesian2(7, 5),
        // Alternate callouts left and right (and nudge vertically) so neighbouring patches do not overlap.
        horizontalOrigin: index % 2 ? C.HorizontalOrigin.RIGHT : C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.CENTER,
        pixelOffset: new C.Cartesian2(index % 2 ? -12 : 12, index % 4 === 1 ? -10 : index % 4 === 2 ? 10 : 0), disableDepthTestDistance: 2_000_000, distanceDisplayCondition: new C.DistanceDisplayCondition(0, m.active ? 900_000 : 250_000) } });
    }
    v.scene.requestRender();
  }, [ready, props.marks]);

  return <>
    <div ref={mount} className={`globe-canvas ${props.picking ? 'is-picking' : ''}`} aria-label="Interactive Earth globe" />
    {!ready && !error && <div className="boot"><span className="boot-word">Terra</span><span className="boot-line" /></div>}
    {error && <div className="globe-error"><strong>The 3D view stopped</strong><p>{error}</p><p>Turn on hardware acceleration in your browser, then reload.</p></div>}
    {googleLoading && props.photoreal && <div className="google-loading" role="status">Loading Google 3D · terrain preview</div>}
    {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice('')} aria-label="Dismiss notice">×</button></div>}
    <div className="map-credits" ref={credits} />
  </>;
});
export default Globe;
