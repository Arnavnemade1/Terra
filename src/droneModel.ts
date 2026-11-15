/**
 * Low-poly aircraft models built in code as glTF 2.0 (no asset files): a quadcopter and a small fixed-wing.
 * Shading is baked into vertex colours and the materials are unlit, so the aircraft read the same by day or night.
 * glTF axes: +Y up, +Z forward (the camera and green lights face forward), +X to the aircraft's left.
 */
type V3 = [number, number, number];
type Part = { pos: number[]; col: number[]; idx: number[] };

const LIGHT: V3 = (() => { const v: V3 = [0.35, 1, 0.45]; const l = Math.hypot(...v); return v.map(x => x / l) as V3; })();
const hex = (h: string): V3 => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255) as V3;

function face(p: Part, corners: V3[], normal: V3, colour: V3) {
  // Baked lighting: ambient plus a soft key light from above and to the side.
  const shade = 0.58 + 0.42 * Math.max(0, normal[0] * LIGHT[0] + normal[1] * LIGHT[1] + normal[2] * LIGHT[2]);
  const base = p.pos.length / 3;
  for (const c of corners) { p.pos.push(...c); p.col.push(colour[0] * shade, colour[1] * shade, colour[2] * shade); }
  for (let i = 1; i < corners.length - 1; i++) p.idx.push(base, base + i, base + i + 1);
}
const rotY = (v: V3, a: number): V3 => [v[0] * Math.cos(a) + v[2] * Math.sin(a), v[1], -v[0] * Math.sin(a) + v[2] * Math.cos(a)];
const rotX = (v: V3, a: number): V3 => [v[0], v[1] * Math.cos(a) - v[2] * Math.sin(a), v[1] * Math.sin(a) + v[2] * Math.cos(a)];
const add = (a: V3, b: V3): V3 => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];

/** An axis-aligned box (optionally turned about the vertical axis) centred at c with full sizes s. */
function box(p: Part, c: V3, s: V3, colour: string, yaw = 0) {
  const [hx, hy, hz] = [s[0] / 2, s[1] / 2, s[2] / 2]; const col = hex(colour);
  const T = (v: V3) => add(rotY(v, yaw), c); const N = (v: V3) => rotY(v, yaw);
  face(p, [[-hx, hy, -hz], [-hx, hy, hz], [hx, hy, hz], [hx, hy, -hz]].map(v => T(v as V3)), N([0, 1, 0]), col);
  face(p, [[-hx, -hy, -hz], [hx, -hy, -hz], [hx, -hy, hz], [-hx, -hy, hz]].map(v => T(v as V3)), N([0, -1, 0]), col);
  face(p, [[-hx, -hy, hz], [hx, -hy, hz], [hx, hy, hz], [-hx, hy, hz]].map(v => T(v as V3)), N([0, 0, 1]), col);
  face(p, [[hx, -hy, -hz], [-hx, -hy, -hz], [-hx, hy, -hz], [hx, hy, -hz]].map(v => T(v as V3)), N([0, 0, -1]), col);
  face(p, [[hx, -hy, hz], [hx, -hy, -hz], [hx, hy, -hz], [hx, hy, hz]].map(v => T(v as V3)), N([1, 0, 0]), col);
  face(p, [[-hx, -hy, -hz], [-hx, -hy, hz], [-hx, hy, hz], [-hx, hy, -hz]].map(v => T(v as V3)), N([-1, 0, 0]), col);
}

/** A cylinder along +Y (or along +Z when `forward`), centred at c. */
function cylinder(p: Part, c: V3, r: number, h: number, colour: string, segments = 14, forward = false) {
  const col = hex(colour); const o = (v: V3): V3 => add(forward ? rotX(v, Math.PI / 2) : v, c); const n = (v: V3): V3 => forward ? rotX(v, Math.PI / 2) : v;
  const ring = (y: number) => Array.from({ length: segments }, (_, i) => [Math.cos(i / segments * 2 * Math.PI) * r, y, Math.sin(i / segments * 2 * Math.PI) * r] as V3);
  const top = ring(h / 2), bottom = ring(-h / 2);
  face(p, [...top].reverse().map(o), n([0, 1, 0]), col);
  face(p, bottom.map(o), n([0, -1, 0]), col);
  for (let i = 0; i < segments; i++) {
    const j = (i + 1) % segments; const mid = (i + 0.5) / segments * 2 * Math.PI;
    face(p, [bottom[i], bottom[j], top[j], top[i]].map(o), n([Math.cos(mid), 0, Math.sin(mid)]), col);
  }
}

type Model = { solid: Part; glass: Part; props: V3[]; prop: { solid: Part; glass: Part }; spinAxis: 'y' | 'z' };
const part = (): Part => ({ pos: [], col: [], idx: [] });

/** A two-blade propeller centred on its hub, blades along X, spinning about Y (quad) or Z (plane), with a faint disc. */
function propeller(radius: number, axis: 'y' | 'z'): { solid: Part; glass: Part } {
  const solid = part(), glass = part(); const blade = '#1d1f21';
  if (axis === 'y') {
    box(solid, [radius * 0.5, 0, 0], [radius, 0.004, radius * 0.16], blade, 0.12);
    box(solid, [-radius * 0.5, 0, 0], [radius, 0.004, radius * 0.16], blade, 0.12);
    cylinder(glass, [0, 0, 0], radius, 0.002, '#cfd2d4', 28);
  } else {
    box(solid, [radius * 0.5, 0, 0], [radius, radius * 0.14, 0.006], blade);
    box(solid, [-radius * 0.5, 0, 0], [radius, radius * 0.14, 0.006], blade);
    cylinder(glass, [0, 0, 0], radius, 0.003, '#cfd2d4', 28, true);
  }
  return { solid, glass };
}

function quad(): Model {
  const solid: Part = { pos: [], col: [], idx: [] }, glass: Part = { pos: [], col: [], idx: [] };
  const white = '#eceae3', light = '#d9d6cd', dark = '#2f3133', black = '#141516';
  box(solid, [0, 0.07, 0], [0.15, 0.065, 0.25], white);          // body
  box(solid, [0, 0.112, -0.01], [0.115, 0.03, 0.17], light);     // top shell
  box(solid, [0, 0.07, 0.135], [0.1, 0.045, 0.03], light);       // nose
  for (const [sx, sz] of [[1, 1], [-1, 1], [1, -1], [-1, -1]]) {
    const yaw = Math.atan2(sx, sz);
    box(solid, [sx * 0.1, 0.082, sz * 0.1], [0.026, 0.018, 0.27], dark, yaw);     // arm
    cylinder(solid, [sx * 0.19, 0.098, sz * 0.19], 0.024, 0.04, dark);           // motor
    cylinder(solid, [sx * 0.19, 0.122, sz * 0.19], 0.008, 0.012, black, 8);      // hub
    box(solid, [sx * 0.19, 0.072, sz * 0.19], [0.016, 0.012, 0.016], sz > 0 ? '#59e36b' : '#ff4b3a'); // navigation light
  }
  for (const sx of [1, -1]) {
    box(solid, [sx * 0.065, 0.0, 0], [0.012, 0.012, 0.22], dark);   // skid
    box(solid, [sx * 0.065, 0.025, 0.06], [0.01, 0.05, 0.01], dark); // struts
    box(solid, [sx * 0.065, 0.025, -0.06], [0.01, 0.05, 0.01], dark);
  }
  box(solid, [0, 0.022, 0.1], [0.05, 0.035, 0.035], black);     // gimbal yoke
  cylinder(solid, [0, 0.016, 0.125], 0.017, 0.03, '#0a0b0c', 12, true); // camera
  const props: V3[] = [[1, 1], [-1, 1], [1, -1], [-1, -1]].map(([sx, sz]) => [sx * 0.19, 0.126, sz * 0.19]);
  return { solid, glass, props, prop: propeller(0.125, 'y'), spinAxis: 'y' };
}

function plane(): Model {
  const solid: Part = { pos: [], col: [], idx: [] }, glass: Part = { pos: [], col: [], idx: [] };
  const white = '#eceae3', light = '#d5d2c9', dark = '#2f3133', black = '#141516';
  box(solid, [0, 0.12, 0.02], [0.17, 0.17, 1.25], white);         // fuselage
  box(solid, [0, 0.12, 0.7], [0.13, 0.13, 0.14], light);          // nose
  box(solid, [0, 0.215, 0.18], [0.1, 0.05, 0.32], '#3a4552');     // canopy fairing
  box(solid, [0, 0.17, 0.12], [2.3, 0.035, 0.34], white);         // wing
  for (const sx of [1, -1]) {
    box(solid, [sx * 1.12, 0.17, 0.12], [0.07, 0.04, 0.34], light);                        // wingtip
    box(solid, [sx * 1.13, 0.17, 0.2], [0.03, 0.03, 0.03], sx > 0 ? '#ff4b3a' : '#59e36b'); // nav lights (left red, right green)
  }
  box(solid, [0, 0.15, -0.6], [0.72, 0.025, 0.2], white);         // horizontal tail
  box(solid, [0, 0.3, -0.62], [0.025, 0.28, 0.22], white);        // fin
  cylinder(solid, [0, 0.12, 0.79], 0.02, 0.06, dark, 10, true);   // spinner
  box(solid, [0, 0.02, 0.32], [0.08, 0.07, 0.1], black);          // camera pod
  return { solid, glass, props: [[0, 0.12, 0.825]], prop: propeller(0.2, 'z'), spinAxis: 'z' };
}

function toGltf(model: Model): object {
  // Body parts (mesh 0) and propeller parts (mesh 1); a part with no geometry is left out (a zero-length buffer
  // makes the whole glTF invalid).
  const bodyParts = [model.solid, model.glass].map((p, m) => ({ p, m })).filter(x => x.p.idx.length);
  const propParts = [model.prop.solid, model.prop.glass].map((p, m) => ({ p, m })).filter(x => x.p.idx.length);
  const parts = [...bodyParts, ...propParts].map(x => x.p); const mats = [...bodyParts, ...propParts].map(x => x.m);
  const chunks: ArrayBuffer[] = []; const views: object[] = []; const accessors: object[] = []; let offset = 0;
  const push = (data: Float32Array | Uint16Array, target: number) => {
    const bytes = data.byteLength; const padded = Math.ceil(bytes / 4) * 4; const buf = new ArrayBuffer(padded); new Uint8Array(buf).set(new Uint8Array(data.buffer, data.byteOffset, bytes));
    chunks.push(buf); views.push({ buffer: 0, byteOffset: offset, byteLength: bytes, target }); offset += padded; return views.length - 1;
  };
  const primitives = parts.map((p, i) => {
    const pos = new Float32Array(p.pos), col = new Float32Array(p.col), idx = new Uint16Array(p.idx);
    const min = [0, 1, 2].map(k => Math.min(...p.pos.filter((_, j) => j % 3 === k))), max = [0, 1, 2].map(k => Math.max(...p.pos.filter((_, j) => j % 3 === k)));
    accessors.push({ bufferView: push(pos, 34962), componentType: 5126, count: pos.length / 3, type: 'VEC3', min, max });
    const posA = accessors.length - 1;
    accessors.push({ bufferView: push(col, 34962), componentType: 5126, count: col.length / 3, type: 'VEC3' });
    const colA = accessors.length - 1;
    accessors.push({ bufferView: push(idx, 34963), componentType: 5123, count: idx.length, type: 'SCALAR' });
    return { attributes: { POSITION: posA, COLOR_0: colA }, indices: accessors.length - 1, material: mats[i] };
  });
  const all = new Uint8Array(offset); let at = 0; for (const c of chunks) { all.set(new Uint8Array(c), at); at += c.byteLength; }
  let binary = ''; for (let i = 0; i < all.length; i += 0x8000) binary += String.fromCharCode(...all.subarray(i, i + 0x8000));
  return {
    asset: { version: '2.0', generator: 'Terra' }, extensionsUsed: ['KHR_materials_unlit'], scene: 0, scenes: [{ nodes: [0] }],
    nodes: [{ mesh: 0, name: 'aircraft', children: model.props.map((_, i) => i + 1) },
      ...model.props.map((at, i) => ({ mesh: 1, name: `prop${i}`, translation: at }))],
    meshes: [{ primitives: primitives.slice(0, bodyParts.length) }, { primitives: primitives.slice(bodyParts.length) }],
    materials: [
      { pbrMetallicRoughness: { baseColorFactor: [1, 1, 1, 1], metallicFactor: 0, roughnessFactor: 1 }, extensions: { KHR_materials_unlit: {} }, doubleSided: false },
      { pbrMetallicRoughness: { baseColorFactor: [1, 1, 1, 0.28], metallicFactor: 0, roughnessFactor: 1 }, extensions: { KHR_materials_unlit: {} }, alphaMode: 'BLEND', doubleSided: true },
    ],
    accessors, bufferViews: views, buffers: [{ byteLength: offset, uri: `data:application/octet-stream;base64,${btoa(binary)}` }],
  };
}

const cache: Partial<Record<'quad' | 'fixed-wing', object>> = {};
/** The glTF JSON for an aircraft type (built once). */
export function aircraftGltf(kind: 'quad' | 'fixed-wing'): object {
  return cache[kind] ??= toGltf(kind === 'quad' ? quad() : plane());
}
/** Propeller nodes of an aircraft type and the axis they spin about (glTF axes: Y up, Z forward). */
export function propellers(kind: 'quad' | 'fixed-wing'): { names: string[]; axis: 'y' | 'z' } {
  const m = kind === 'quad' ? 4 : 1; return { names: Array.from({ length: m }, (_, i) => `prop${i}`), axis: kind === 'quad' ? 'y' : 'z' };
}
