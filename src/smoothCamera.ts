import * as C from 'cesium';

/**
 * Smoother globe navigation.
 *
 * Wheel zoom: Cesium moves the camera a fixed step per wheel event, which feels like stepping. Here each wheel event
 * adds to a pending zoom (in log-distance, so it is scale-independent), and every frame the camera eases a share of
 * it toward the ground under the cursor (time constant ~140 ms). Trackpads, mice and pinches all glide.
 * Motion-adaptive detail: while the camera moves, the globe asks for coarser tiles (less work per frame, fewer
 * requests for views that are only passed through) and sharpens again when it settles.
 */
export function installSmoothCamera(v: C.Viewer, opts: { baseError: () => number }): () => void {
  const scene = v.scene, camera = v.camera, ctrl = scene.screenSpaceCameraController, canvas = v.canvas;
  ctrl.zoomEventTypes = [C.CameraEventType.RIGHT_DRAG, C.CameraEventType.PINCH, { eventType: C.CameraEventType.LEFT_DRAG, modifier: C.KeyboardEventModifier.CTRL }];
  ctrl.inertiaSpin = 0.93; ctrl.inertiaTranslate = 0.92; ctrl.inertiaZoom = 0.85;
  let pending = 0; let cursor = new C.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2); let last = performance.now();
  const onWheel = (e: WheelEvent) => {
    if (!ctrl.enableInputs || !ctrl.enableZoom) return;
    e.preventDefault();
    const unit = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1;  // lines and pages to pixels
    pending += -e.deltaY * unit * 0.0021;  // about 20 % closer per mouse notch
    pending = Math.max(-3, Math.min(3, pending));
    const r = canvas.getBoundingClientRect(); cursor = new C.Cartesian2(e.clientX - r.left, e.clientY - r.top);
    scene.requestRender();
  };
  canvas.addEventListener('wheel', onWheel, { passive: false });
  const toward = new C.Cartesian3();
  const step = scene.preUpdate.addEventListener(() => {
    const now = performance.now(); const dt = Math.min(0.05, (now - last) / 1000); last = now;
    if (Math.abs(pending) < 1e-4) { pending = 0; return; }
    const share = 1 - Math.exp(-dt / 0.14); const dl = pending * share; pending -= dl;
    const following = !C.Matrix4.equals(camera.transform, C.Matrix4.IDENTITY);
    if (following) {  // flying with an aircraft: zoom toward it (the chase camera keeps the new range)
      const d = C.Cartesian3.magnitude(camera.position);
      camera.moveForward(Math.min(d - 40, d * (1 - Math.exp(-dl))));
    } else {
      const ray = camera.getPickRay(cursor);
      const hit = ray && (scene.globe.pick(ray, scene) ?? camera.pickEllipsoid(cursor));
      if (!hit) { pending = 0; return; }
      const d = C.Cartesian3.distance(camera.positionWC, hit);
      const move = d * (1 - Math.exp(-dl));
      if (d - move < ctrl.minimumZoomDistance || d - move > ctrl.maximumZoomDistance) { pending = 0; return; }
      C.Cartesian3.normalize(C.Cartesian3.subtract(hit, camera.positionWC, toward), toward);
      camera.move(toward, move);
    }
    scene.requestRender();
  });
  // Coarser while moving, full detail when still.
  const start = camera.moveStart.addEventListener(() => { scene.globe.maximumScreenSpaceError = opts.baseError() * 1.75; });
  const end = camera.moveEnd.addEventListener(() => { scene.globe.maximumScreenSpaceError = opts.baseError(); scene.requestRender(); });
  camera.percentageChanged = 0.05;
  return () => { canvas.removeEventListener('wheel', onWheel); step(); start(); end(); };
}
