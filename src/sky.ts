import * as C from 'cesium';

/**
 * A fine-grained star field drawn into six cube faces at start-up. It replaces
 * Cesium's default Tycho sky, whose upsampled texture reads as blocky blobs. The
 * star field is decoration behind the planet, not an astronomical catalogue.
 */
export function createStarSky(size = 1024): C.SkyBox {
  const faces = ['positiveX', 'negativeX', 'positiveY', 'negativeY', 'positiveZ', 'negativeZ'] as const;
  let seed = 1337;
  const rand = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
  const sources: Record<string, HTMLCanvasElement> = {};
  faces.forEach((face, f) => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = size;
    const g = canvas.getContext('2d')!;
    g.fillStyle = '#000'; g.fillRect(0, 0, size, size);
    // A faint band so the sky is not a flat void; strongest on the ±Y faces.
    if (f === 2 || f === 3 || f === 4) {
      const band = g.createLinearGradient(0, size * 0.2, size, size * 0.8);
      band.addColorStop(0, 'rgba(0,0,0,0)'); band.addColorStop(0.5, 'rgba(70,90,120,0.07)'); band.addColorStop(1, 'rgba(0,0,0,0)');
      g.fillStyle = band; g.fillRect(0, 0, size, size);
    }
    const count = 1600;
    for (let i = 0; i < count; i++) {
      const x = rand() * size, y = rand() * size;
      // Magnitude-like distribution: many faint, few bright.
      const m = Math.pow(rand(), 5.5);
      const r = 0.35 + m * 1.5;
      const temp = rand();
      const color = temp < 0.15 ? '255,214,180' : temp < 0.3 ? '190,210,255' : '235,240,255';
      const a = 0.18 + m * 0.82;
      if (m > 0.35) {
        const glow = g.createRadialGradient(x, y, 0, x, y, r * 4);
        glow.addColorStop(0, `rgba(${color},${a * 0.35})`); glow.addColorStop(1, `rgba(${color},0)`);
        g.fillStyle = glow; g.beginPath(); g.arc(x, y, r * 4, 0, Math.PI * 2); g.fill();
      }
      g.fillStyle = `rgba(${color},${a})`; g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
    }
    sources[face] = canvas;
  });
  return new C.SkyBox({ sources: sources as unknown as ConstructorParameters<typeof C.SkyBox>[0]['sources'] });
}
