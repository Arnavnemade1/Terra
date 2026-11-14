"""Terrain context for the relief map: land cover and named terrain features.

Land cover is ESA WorldCover 2021 (10 m), read as a bounded window from the public
Cloud-Optimized GeoTIFFs. It is a classification of the surface, not a measurement of
change. Named features (summits, volcanoes, lakes, glaciers, settlements) come from
Wikidata with the elevation the source states. Both go through the shared connector.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import time
import warnings
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from .connector import ConnectorError, RangeFile, request

WORLDCOVER = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
WIKIDATA = "https://query.wikidata.org/sparql"
# Class code: (name, display colour for the stylised relief map).
CLASSES: dict[int, tuple[str, str]] = {
    10: ("Tree cover", "#4f5f37"), 20: ("Shrubland", "#8a8350"), 30: ("Grassland", "#b3a874"),
    40: ("Cropland", "#b9a35f"), 50: ("Built-up", "#8f8a82"), 60: ("Bare or sparse vegetation", "#cdb994"),
    70: ("Snow and ice", "#eef0ee"), 80: ("Permanent water", "#4d6f86"), 90: ("Herbaceous wetland", "#6f8a6a"),
    95: ("Mangroves", "#3f5a3a"), 100: ("Moss and lichen", "#a8a487"),
}
SIZE = 560  # grid cells along the longer side
PAD = 0.35  # land cover extends this fraction beyond each side, for oblique views
PEAK_KINDS = {"Q8502": "peak", "Q207326": "peak", "Q54050": "peak", "Q8072": "volcano", "Q169358": "volcano",
              "Q212057": "volcano", "Q185113": "volcano", "Q35666": "glacier", "Q23397": "lake"}
CACHE_SECONDS = 30 * 86400
_memory: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _tiles(bbox: list[float]) -> list[tuple[str, list[float]]]:
    """WorldCover is published in 3 x 3 degree tiles named by their south-west corner."""
    west, south, east, north = bbox
    out = []
    for lat0 in range(math.floor(south / 3) * 3, math.ceil(north / 3) * 3, 3):
        for lon0 in range(math.floor(west / 3) * 3, math.ceil(east / 3) * 3, 3):
            name = f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"
            out.append((name, [lon0, lat0, lon0 + 3, lat0 + 3]))
    return out


def _shape(bbox: list[float]) -> tuple[int, int]:
    west, south, east, north = bbox
    wide = (east - west) * math.cos(math.radians((south + north) / 2))
    tall = north - south
    if wide >= tall:
        return max(8, round(SIZE * tall / wide)), SIZE
    return SIZE, max(8, round(SIZE * wide / tall))


def _read_tile(url: str, bbox: list[float], grid: np.ndarray) -> bool:
    """Fill `grid` (covering `bbox`) from one WorldCover tile. False when the tile does not exist (open ocean)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject
    from rasterio.windows import Window, from_bounds as window_from_bounds
    try:
        shared = RangeFile("worldcover", url, block=128 * 1024, workers=16, timeout=20)
    except ConnectorError as exc:
        if exc.status_code in (403, 404):
            return False
        raise

    def opener(path, mode="rb"):
        if path != url:
            raise FileNotFoundError(path)
        return shared.view()

    height, width = grid.shape
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(url, opener=opener) as full:
        native = abs(full.transform.a)
        needed = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / (SIZE * 1.5)
        factors = full.overviews(1)
    usable = [i for i, f in enumerate(factors) if native * f <= needed]
    options = {"overview_level": usable[-1]} if usable else {}
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(url, opener=opener, **options) as src:
        window = window_from_bounds(*bbox, transform=src.transform).round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, src.width, src.height))
        if window.width < 1 or window.height < 1:
            return True
        bh, bw = src.block_shapes[0]
        ranges = []
        for row in range(int(window.row_off) // bh, int(window.row_off + window.height - 1) // bh + 1):
            for col in range(int(window.col_off) // bw, int(window.col_off + window.width - 1) // bw + 1):
                offset = src.get_tag_item(f"BLOCK_OFFSET_{col}_{row}", "TIFF", bidx=1)
                size = src.get_tag_item(f"BLOCK_SIZE_{col}_{row}", "TIFF", bidx=1)
                if offset and size:
                    ranges.append((int(offset), int(size)))
        shared.prefetch(ranges)
        scale = max(1.0, max(window.width, window.height) / (SIZE * 1.5))
        out_h, out_w = max(1, int(window.height / scale)), max(1, int(window.width / scale))
        data = src.read(1, window=window, out_shape=(out_h, out_w), resampling=Resampling.nearest)
        from rasterio.transform import Affine
        source = src.window_transform(window) * Affine.scale(window.width / out_w, window.height / out_h)
        part = np.zeros_like(grid)
        warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)
        reproject(data, part, src_transform=source, src_crs=src.crs, dst_transform=from_bounds(*bbox, width, height),
                  dst_crs="EPSG:4326", src_nodata=0, dst_nodata=0, resampling=Resampling.mode)
        grid[part > 0] = part[part > 0]
    return True


def landcover(bbox: list[float], inner: list[float] | None = None) -> dict:
    """Land-cover classes on a regular longitude/latitude grid; shares are for `inner` (the area itself) when given."""
    height, width = _shape(bbox)
    grid = np.zeros((height, width), dtype=np.uint8)
    found = 0
    for name, extent in _tiles(bbox):
        part = [max(bbox[0], extent[0]), max(bbox[1], extent[1]), min(bbox[2], extent[2]), min(bbox[3], extent[3])]
        if part[2] <= part[0] or part[3] <= part[1]:
            continue
        found += _read_tile(WORLDCOVER.format(tile=name), bbox, grid)
    # Tiles are absent over open ocean; WorldCover marks water inside tiles as class 80.
    if not found:
        grid[:] = 80
    core = grid
    if inner:
        x0 = int((inner[0] - bbox[0]) / (bbox[2] - bbox[0]) * width); x1 = int(math.ceil((inner[2] - bbox[0]) / (bbox[2] - bbox[0]) * width))
        y0 = int((bbox[3] - inner[3]) / (bbox[3] - bbox[1]) * height); y1 = int(math.ceil((bbox[3] - inner[1]) / (bbox[3] - bbox[1]) * height))
        core = grid[max(0, y0):max(y0 + 1, y1), max(0, x0):max(x0 + 1, x1)]
    valid = core[core > 0]
    shares = []
    if valid.size:
        codes, counts = np.unique(valid, return_counts=True)
        for code, count in sorted(zip(codes.tolist(), counts.tolist()), key=lambda c: -c[1]):
            if code in CLASSES:
                shares.append({"code": code, "name": CLASSES[code][0], "pct": round(100 * count / valid.size, 1)})
    png = BytesIO()
    Image.fromarray(grid, mode="L").save(png, format="PNG", optimize=True)
    return {
        "bounds": bbox, "width": width, "height": height, "grid_png": base64.b64encode(png.getvalue()).decode(),
        "classes": {str(code): {"name": name, "color": color} for code, (name, color) in CLASSES.items()},
        "shares": shares, "source": "ESA WorldCover 2021 v200, 10 m", "source_url": "https://esa-worldcover.org",
        "resolution_m": round((bbox[2] - bbox[0]) * 111_320 * math.cos(math.radians((bbox[1] + bbox[3]) / 2)) / width),
    }


def _query(bbox: list[float]) -> str:
    west, south, east, north = bbox
    kinds = " ".join(f"wd:{k}" for k in PEAK_KINDS)
    return f"""SELECT ?item ?itemLabel ?coord ?elev ?kind ?pop WHERE {{
  SERVICE wikibase:box {{ ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({west} {south})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({east} {north})"^^geo:wktLiteral . }}
  {{ VALUES ?kind {{ {kinds} }} ?item wdt:P31 ?kind . OPTIONAL {{ ?item wdt:P2044 ?elev . }} }}
  UNION {{ ?item wdt:P1082 ?pop . FILTER(?pop >= 200) }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul,es,fr,de,it,pt,nl,id,ja,zh,ru,el,tr,is,sv,no,ar" . }}
}} LIMIT 600"""


async def named_features(bbox: list[float]) -> list[dict]:
    response = await request("wikidata", "GET", WIKIDATA, params={"query": _query(bbox), "format": "json"},
                             headers={"Accept": "application/sparql-results+json"}, timeout=20, max_bytes=4_000_000)
    rows = response.json().get("results", {}).get("bindings", [])
    items: dict[str, dict] = {}
    for row in rows:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        label = row.get("itemLabel", {}).get("value", "")
        point = re.fullmatch(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", row.get("coord", {}).get("value", ""))
        if not point or not label or re.fullmatch(r"Q\d+", label):
            continue
        lon, lat = float(point.group(1)), float(point.group(2))
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        kind = PEAK_KINDS.get(row.get("kind", {}).get("value", "").rsplit("/", 1)[-1]) if "kind" in row else None
        pop = row.get("pop", {}).get("value")
        kind = kind or ("place" if pop else None)
        if not kind:
            continue
        elev = row.get("elev", {}).get("value")
        try:
            elev_m = float(elev) if elev is not None else None
        except ValueError:
            elev_m = None
        if elev_m is not None and not -500 < elev_m < 9000:
            elev_m = None
        current = items.get(qid)
        record = {"id": qid, "name": label[:60], "kind": kind, "lon": round(lon, 5), "lat": round(lat, 5),
                  "elevation_m": round(elev_m) if elev_m is not None else None,
                  "population": int(float(pop)) if pop else None, "source_url": f"https://www.wikidata.org/wiki/{qid}"}
        if current:
            # Several statements per item: keep the most specific kind and the highest stated elevation.
            if current["kind"] == "place" and kind != "place":
                current["kind"] = kind
            if record["elevation_m"] is not None and (current["elevation_m"] is None or record["elevation_m"] > current["elevation_m"]):
                current["elevation_m"] = record["elevation_m"]
            current["population"] = current["population"] or record["population"]
            continue
        items[qid] = record
    values = list(items.values())
    # The same summit is often recorded twice under different names; keep the better-described one.
    ranked = sorted((v for v in values if v["kind"] in ("peak", "volcano")), key=lambda v: (v["elevation_m"] is None, -(v["elevation_m"] or 0)))
    peaks: list[dict] = []
    for v in ranked:
        if all(math.hypot((v["lon"] - k["lon"]) * math.cos(math.radians(v["lat"])), v["lat"] - k["lat"]) * 111_000 > 700 for k in peaks):
            peaks.append(v)
    peaks = peaks[:28]
    water = [v for v in values if v["kind"] in ("lake", "glacier")][:10]
    places = sorted((v for v in values if v["kind"] == "place"), key=lambda v: -(v["population"] or 0))[:14]
    return peaks + water + places


def _disk(key: str) -> Path:
    from .config import get_settings
    return get_settings().data_dir / "cache" / f"terrain-{key}.json"


async def terrain_context(bbox: list[float]) -> dict:
    """Land cover and named features for an area, cached on disk (both change slowly)."""
    bbox = [round(v, 4) for v in bbox]
    key = hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    hit = _memory.get(key)
    if not hit:
        try:
            saved = json.loads(_disk(key).read_text())
            hit = (float(saved["saved_at"]), saved["value"])
        except (OSError, ValueError, KeyError, TypeError):
            hit = None
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        dx, dy = (bbox[2] - bbox[0]) * PAD, (bbox[3] - bbox[1]) * PAD
        padded = [max(-180, bbox[0] - dx), max(-90, bbox[1] - dy), min(180, bbox[2] + dx), min(90, bbox[3] + dy)]
        padded = [round(v, 4) for v in padded]
        cover_job = asyncio.wait_for(asyncio.to_thread(landcover, padded, bbox), 30)
        names_job = named_features(bbox)
        cover, names = await asyncio.gather(cover_job, names_job, return_exceptions=True)
        unavailable = []
        if isinstance(cover, BaseException):
            unavailable.append("land cover")
            cover = None
        if isinstance(names, BaseException):
            unavailable.append("named features")
            names = []
        value = {"bbox": bbox, "landcover": cover, "features": names, "unavailable": unavailable,
                 "features_source": "Wikidata (CC0), elevations as stated by the source"}
        if not unavailable:
            _memory[key] = (time.time(), value)
            try:
                path = _disk(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"saved_at": time.time(), "value": value}))
                temporary.replace(path)
            except OSError:
                pass
        return value
