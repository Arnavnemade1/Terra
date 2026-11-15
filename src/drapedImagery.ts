import * as C from 'cesium';

/**
 * One georeferenced image (a radar or true-colour result) served as a world-tiled imagery pyramid, so it can be
 * draped onto Google Photorealistic 3D Tiles (`tileset.imageryLayers`).
 *
 * Cesium's single-tile provider uses a tiling scheme that only covers the image's own rectangle; 3D tiles reaching past
 * that rectangle get no imagery coverage and the drape silently disappears. Here the tiling scheme is the whole
 * globe and each requested tile is cut from the source image (transparent outside it). Radar keeps hard pixel edges
 * (nearest-neighbour); photographs are smoothed. Display only: nothing is read back from the Google tiles.
 *
 * `magnitudeAlpha` is for the diverging change ramp: on the photoreal ground an opaque sheet would hide the terrain, so
 * each pixel's opacity follows its colour saturation. The ramp's neutral middle (no change) becomes see-through and
 * strong increases (orange) and decreases (blue) stay solid. The colours themselves are untouched.
 */
export class DrapedImageProvider {
  readonly tilingScheme = new C.GeographicTilingScheme();
  readonly tileWidth = 256;
  readonly tileHeight = 256;
  readonly minimumLevel = 0;
  readonly maximumLevel = 16;
  readonly rectangle: C.Rectangle;
  readonly hasAlphaChannel = true;
  readonly errorEvent = new C.Event();
  readonly tileDiscardPolicy = undefined;
  readonly proxy = undefined;
  readonly credit: C.Credit;
  readonly ready = true;
  private image: Promise<ImageBitmap | HTMLImageElement>;
  private blank: HTMLCanvasElement;

  readonly source: string;  // the image URL, so a changed result replaces the drape

  constructor(url: string, bounds: C.Rectangle, private smooth: boolean, credit: C.Credit, private magnitudeAlpha = false) {
    this.rectangle = bounds; this.credit = credit; this.source = url;
    this.image = C.Resource.fetchImage({ url }) as Promise<ImageBitmap | HTMLImageElement>;
    this.blank = document.createElement('canvas'); this.blank.width = this.blank.height = 1;
  }

  getTileCredits() { return [] as C.Credit[]; }
  pickFeatures() { return undefined; }

  async requestImage(x: number, y: number, level: number): Promise<HTMLCanvasElement> {
    const tile = this.tilingScheme.tileXYToRectangle(x, y, level);
    const r = this.rectangle;
    if (tile.east <= r.west || tile.west >= r.east || tile.north <= r.south || tile.south >= r.north) return this.blank;
    const img = await this.image; const W = img.width, H = img.height;
    // Source pixels covered by this tile (image x runs west to east, y north to south).
    const sx = (tile.west - r.west) / r.width * W, sw = tile.width / r.width * W;
    const sy = (r.north - tile.north) / r.height * H, sh = tile.height / r.height * H;
    const c = document.createElement('canvas'); c.width = this.tileWidth; c.height = this.tileHeight;
    const g = c.getContext('2d'); if (!g) return this.blank;
    g.imageSmoothingEnabled = this.smooth;
    g.drawImage(img, sx, sy, sw, sh, 0, 0, this.tileWidth, this.tileHeight);
    if (this.magnitudeAlpha) {
      const d = g.getImageData(0, 0, c.width, c.height); const px = d.data;
      for (let i = 0; i < px.length; i += 4) {
        const hi = Math.max(px[i], px[i + 1], px[i + 2]), lo = Math.min(px[i], px[i + 1], px[i + 2]);
        const sat = hi === 0 ? 0 : (hi - lo) / hi;               // 0 for white/grey (no change), toward 1 for strong colour
        const k = Math.min(1, Math.max(0, (sat - 0.5) / 0.3)); px[i + 3] = Math.round(px[i + 3] * k * k * (3 - 2 * k));  // smoothstep
      }
      g.putImageData(d, 0, 0);
    }
    return c;
  }
}
