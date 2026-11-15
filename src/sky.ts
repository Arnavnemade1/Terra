import * as C from 'cesium';

/**
 * The night sky behind the planet, drawn once into the six faces of a cube map at start-up. It replaces Cesium's
 * default Tycho sky, whose upsampled texture reads as blocky blobs.
 *
 * Stars are placed on the celestial sphere and projected into whichever face they fall on, so nothing breaks at the
 * cube's seams; they thicken toward a soft Milky Way laid along the galactic plane, with its bulge toward the galactic
 * centre and a dust lane through it. Cesium draws the sky box in the equatorial frame (x toward the vernal equinox,
 * z toward the celestial pole), so the band runs where the real one does. The stars themselves are random:
 * decoration behind the planet, not an astronomical catalogue.
 */
type V3 = [number, number, number];
const FACES = ['positiveX', 'negativeX', 'positiveY', 'negativeY', 'positiveZ', 'negativeZ'] as const;
const radec = (ra: number, dec: number): V3 => { const a = ra * Math.PI / 180, d = dec * Math.PI / 180; return [Math.cos(d) * Math.cos(a), Math.cos(d) * Math.sin(a), Math.sin(d)]; };
const dot = (a: V3, b: V3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: V3, b: V3): V3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = (a: V3): V3 => { const k = 1 / Math.hypot(a[0], a[1], a[2]); return [a[0] * k, a[1] * k, a[2] * k]; };
// The galactic frame in J2000 equatorial coordinates: x toward the galactic centre, z toward the galactic north pole.
const GZ = radec(192.859, 27.128), GX = unit(radec(266.405, -28.936)), GY = unit(cross(GZ, GX));
const toSky = (l: number, b: number): V3 => { const cb = Math.cos(b); return unit([0, 1, 2].map(i => cb * Math.cos(l) * GX[i] + cb * Math.sin(l) * GY[i] + Math.sin(b) * GZ[i]) as V3); };

/** The direction a face texel looks along: WebGL's cube-map convention, rows uploaded flipped (Cesium's default). */
function direction(face: number, s: number, t: number): V3 {
  const sc = 2 * s - 1, tc = 2 * t - 1;
  switch (face) {
    case 0: return [1, -tc, -sc];
    case 1: return [-1, -tc, sc];
    case 2: return [sc, 1, tc];
    case 3: return [sc, -1, -tc];
    case 4: return [sc, -tc, 1];
    default: return [-sc, -tc, -1];
  }
}
/** The face a direction falls on and its pixel there (the inverse of `direction`). */
function project([x, y, z]: V3, size: number) {
  const ax = Math.abs(x), ay = Math.abs(y), az = Math.abs(z); let face: number, sc: number, tc: number, ma: number;
  if (ax >= ay && ax >= az) { ma = ax; if (x > 0) { face = 0; sc = -z; tc = -y; } else { face = 1; sc = z; tc = -y; } }
  else if (ay >= az) { ma = ay; if (y > 0) { face = 2; sc = x; tc = z; } else { face = 3; sc = x; tc = -z; } }
  else { ma = az; if (z > 0) { face = 4; sc = x; tc = -y; } else { face = 5; sc = -x; tc = -y; } }
  return { face, x: (sc / ma + 1) / 2 * size, y: (1 - (tc / ma + 1) / 2) * size };
}

/** Smooth 3D value noise in [0, 1], a few octaves, for the band's mottling. */
function noise3(p: V3) {
  const h = (x: number, y: number, z: number) => { let n = (x * 374761393 + y * 668265263 + z * 1274126177) | 0; n = (n ^ (n >>> 13)) * 1274126177; return ((n ^ (n >>> 16)) >>> 0) / 4294967296; };
  const f = (v: number) => v * v * (3 - 2 * v);
  const one = (x: number, y: number, z: number) => {
    const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z), xf = f(x - xi), yf = f(y - yi), zf = f(z - zi);
    const l = (a: number, b: number, t: number) => a + (b - a) * t;
    return l(l(l(h(xi, yi, zi), h(xi + 1, yi, zi), xf), l(h(xi, yi + 1, zi), h(xi + 1, yi + 1, zi), xf), yf),
      l(l(h(xi, yi, zi + 1), h(xi + 1, yi, zi + 1), xf), l(h(xi, yi + 1, zi + 1), h(xi + 1, yi + 1, zi + 1), xf), yf), zf);
  };
  let sum = 0, amp = 0.55, freq = 3.2, norm = 0;
  for (let o = 0; o < 4; o++) { sum += amp * one(p[0] * freq + 11, p[1] * freq + 7, p[2] * freq + 3); norm += amp; amp *= 0.5; freq *= 2.1; }
  return sum / norm;
}

/** The Milky Way's diffuse light toward a direction: a band, brighter and thicker toward the centre, split by dust. */
function milkyWay(d: V3): [number, number] {
  const b = Math.asin(Math.max(-1, Math.min(1, dot(d, GZ)))), l = Math.atan2(dot(d, GY), dot(d, GX));
  const toward = Math.exp(-((l / 1.1) ** 2));                         // 1 toward the galactic centre, falling away
  const width = 0.075 + 0.07 * toward;
  const band = Math.exp(-((b / width) ** 2)) * (0.32 + 0.68 * toward) + Math.exp(-((l / 0.32) ** 2 + (b / 0.16) ** 2)) * 0.55;
  const dust = 1 - 0.62 * Math.exp(-(((b - 0.012) / 0.022) ** 2)) * (0.35 + 0.65 * toward);
  const mottle = 0.45 + 0.9 * noise3(d);
  return [Math.max(0, band * dust * mottle), toward];
}

export function createStarSky(size = 1024): C.SkyBox {
  let seed = 1337;
  const rand = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
  const gauss = () => Math.sqrt(-2 * Math.log(1 - rand())) * Math.cos(2 * Math.PI * rand());
  const canvases = FACES.map(() => { const c = document.createElement('canvas'); c.width = c.height = size; const g = c.getContext('2d')!; g.fillStyle = '#000'; g.fillRect(0, 0, size, size); return c; });

  // The diffuse band, computed at low resolution per face and scaled up smoothly (it has no fine detail to lose).
  const low = 160;
  canvases.forEach((canvas, face) => {
    const small = document.createElement('canvas'); small.width = small.height = low;
    const sg = small.getContext('2d')!; const img = sg.createImageData(low, low);
    for (let y = 0; y < low; y++) for (let x = 0; x < low; x++) {
      const [glow, toward] = milkyWay(unit(direction(face, (x + 0.5) / low, 1 - (y + 0.5) / low)));
      const a = Math.min(1, glow) * 0.16; const i = (y * low + x) * 4;
      // Warmer toward the bulge, cooler along the arms.
      img.data[i] = 205 + 40 * toward; img.data[i + 1] = 210 + 20 * toward; img.data[i + 2] = 235 - 15 * toward; img.data[i + 3] = Math.round(a * 255);
    }
    sg.putImageData(img, 0, 0);
    const g = canvas.getContext('2d')!; g.imageSmoothingEnabled = true; g.imageSmoothingQuality = 'high'; g.drawImage(small, 0, 0, size, size);
  });

  // Stars: an even field plus a population that crowds the galactic plane. Many faint, few bright; no halos that
  // would read as out-of-focus blobs.
  const COLORS = ['255,206,166', '255,234,208', '244,246,255', '244,246,255', '214,226,255'];
  const draw = (d: V3, m: number) => {
    const { face, x, y } = project(d, size); const g = canvases[face].getContext('2d')!;
    const rgb = COLORS[Math.min(COLORS.length - 1, Math.floor(rand() * COLORS.length))];
    if (m < 0.18) { g.fillStyle = `rgba(${rgb},${0.22 + m * 2.2})`; g.fillRect(Math.floor(x), Math.floor(y), 1, 1); return; }
    const r = 0.55 + m * 1.05;
    if (m > 0.55) { g.fillStyle = `rgba(${rgb},${0.08 + 0.1 * m})`; g.beginPath(); g.arc(x, y, r * 2.2, 0, Math.PI * 2); g.fill(); }
    g.fillStyle = `rgba(${rgb},${Math.min(1, 0.45 + m * 0.6)})`; g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
  };
  for (let i = 0; i < 9000; i++) {
    const z = 2 * rand() - 1, a = 2 * Math.PI * rand(), k = Math.sqrt(1 - z * z);
    draw([k * Math.cos(a), k * Math.sin(a), z], Math.pow(rand(), 6));
  }
  for (let i = 0; i < 7000; i++) {
    const l = gauss() * 1.4, b = gauss() * (0.06 + 0.05 * Math.exp(-((l / 1.1) ** 2)));
    draw(toSky(l, b), Math.pow(rand(), 9));
  }
  const sources = Object.fromEntries(FACES.map((f, i) => [f, canvases[i]]));
  return new C.SkyBox({ sources: sources as unknown as ConstructorParameters<typeof C.SkyBox>[0]['sources'] });
}
