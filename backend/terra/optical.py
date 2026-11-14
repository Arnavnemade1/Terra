"""Archive authentic, dated Sentinel-2 optical context for an investigation.

These pictures are visual evidence from a different sensor. They do not become
NISAR measurements and they cannot establish the cause of a radar change.
Only the fixed public Earth Search STAC API and its public Sentinel COG host are
used. No user-supplied URL is opened.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import warnings
from io import BytesIO
import math
from pathlib import Path
import time

import httpx
from .connector import ProviderClient, ConnectorError, RangeFile, validate_url
import numpy as np
from PIL import Image
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_bounds
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds as window_from_bounds

STAC_SEARCH = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-l2a"
COG_PREFIX = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/"
MAX_SIDE_DEGREES = 1.0
SIZE = 1024  # long side of the rendered image, in pixels


def _shape(bbox: list[float]) -> tuple[int, int]:
    """(height, width) with the same degree aspect as the radar previews, so the two can be swiped."""
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if width >= height:
        return max(64, round(SIZE * height / width)), SIZE
    return SIZE, max(64, round(SIZE * width / height))
WINDOW_DAYS = 21


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _bbox_from_result(result: dict) -> list[float] | None:
    values = result.get("provenance", {}).get("parameters", {}).get("bbox")
    if not isinstance(values, list) or len(values) != 4:
        return None
    if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in values):
        return None
    w, s, e, n = values
    if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
        return None
    if e - w > MAX_SIDE_DEGREES or n - s > MAX_SIDE_DEGREES:
        return None
    return [float(v) for v in values]


def _cog_url(item: dict, name: str) -> str | None:
    url = item.get("assets", {}).get(name, {}).get("href")
    if not isinstance(url, str) or not url.startswith(COG_PREFIX) or not url.endswith(".tif"):
        return None
    try:
        return validate_url("sentinel", url)
    except ConnectorError:
        return None


def _candidates(features: list[dict], target: datetime, count: int = 6) -> list[list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in features:
        try:
            stamp = _date(item["properties"]["datetime"])
        except (KeyError, ValueError, TypeError):
            continue
        if not _cog_url(item, "visual") or not _cog_url(item, "scl"):
            continue
        grouped.setdefault(f"{stamp.date().isoformat()}_{item.get('id', '').split('_')[0]}_{stamp.strftime('%H:%M')}", []).append(item)
    # Scene-level cloud scores can be misleading when the AOI straddles two
    # tiles. Rank by the clearer constituent plus temporal distance, then use
    # the AOI's actual classification pixels to accept/reject the mosaic.
    ranked = sorted(grouped.values(), key=lambda group: (
        min(float(item["properties"].get("eo:cloud_cover", 100)) for item in group)
        + 2 * abs((_date(group[0]["properties"]["datetime"]) - target).total_seconds()) / 86400,
        abs((_date(group[0]["properties"]["datetime"]) - target).total_seconds()),
    ))
    return ranked[:count]


async def _search(client: httpx.AsyncClient, bbox: list[float], start: datetime, end: datetime) -> list[dict]:
    request = {"collections": [COLLECTION], "bbox": bbox,
               "datetime": f"{start.isoformat().replace('+00:00','Z')}/{end.isoformat().replace('+00:00','Z')}",
               "limit": 100}
    response = await client.post(STAC_SEARCH, json=request)
    response.raise_for_status()
    return response.json().get("features", [])


def _read_cog(url: str, bands: list[int], bbox: list[float], transform: Affine, dtype, resampling: Resampling, provider: str = "sentinel",
              shape: tuple[int, int] | None = None) -> np.ndarray:
    """Read only the area's window, at roughly the output resolution, through the shared connector.

    The COG's overviews make this a few hundred kilobytes instead of warping the full 10 m band.
    """
    shared = RangeFile(provider, url, block=128 * 1024, workers=16, timeout=20)

    def opener(path, mode="rb"):
        # GDAL also probes sibling paths (.aux.xml, .ovr, "test"); only the COG itself exists.
        if path != url:
            raise FileNotFoundError(path)
        return shared.view()
    # Pick the overview whose pixel size is just finer than the output needs.
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(url, opener=opener) as full:
        bounds = transform_bounds("EPSG:4326", full.crs, *bbox, densify_pts=21)
        native = abs(full.transform.a)
        needed = max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / (SIZE * 1.5)
        factors = full.overviews(1)
    usable = [i for i, f in enumerate(factors) if native * f <= needed]
    options = {"overview_level": usable[-1]} if usable else {}
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(url, opener=opener, **options) as src:
        if max(bands) > src.count:
            raise ValueError("Unexpected band layout")
        window = window_from_bounds(*bounds, transform=src.transform).round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, src.width, src.height))
        # Prefetch exactly the stored tiles under the window, in parallel, from the TIFF tile index.
        bh, bw = src.block_shapes[0]
        ranges = []
        for row in range(int(window.row_off) // bh, int(window.row_off + window.height - 1) // bh + 1):
            for col in range(int(window.col_off) // bw, int(window.col_off + window.width - 1) // bw + 1):
                for band in (bands if src.interleaving and src.interleaving.name == "BAND" else bands[:1]):
                    offset = src.get_tag_item(f"BLOCK_OFFSET_{col}_{row}", "TIFF", bidx=band)
                    size = src.get_tag_item(f"BLOCK_SIZE_{col}_{row}", "TIFF", bidx=band)
                    if offset and size:
                        ranges.append((int(offset), int(size)))
        shared.prefetch(ranges)
        scale = max(1.0, max(window.width, window.height) / (SIZE * 1.5))
        out_h, out_w = max(1, int(window.height / scale)), max(1, int(window.width / scale))
        data = src.read(bands, window=window, out_shape=(len(bands), out_h, out_w), resampling=resampling)
        source_transform = src.window_transform(window) * Affine.scale(window.width / out_w, window.height / out_h)
        destination = np.zeros((len(bands), *(shape or _shape(bbox))), dtype=dtype)
        warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)
        reproject(data, destination, src_transform=source_transform, src_crs=src.crs, dst_transform=transform,
                  dst_crs="EPSG:4326", src_nodata=0, dst_nodata=0, resampling=resampling)
        return destination


def _render(items: list[dict], bbox: list[float]) -> tuple[bytes, dict] | None:
    """Read only the bounded AOI from each public COG, plus its 20 m cloud mask."""
    height, width = _shape(bbox)
    transform = from_bounds(*bbox, width, height)
    rgb = np.zeros((3, height, width), dtype=np.uint8)
    scl = np.zeros((height, width), dtype=np.uint8)
    contributed: list[dict] = []
    failed = 0
    for item in sorted(items, key=lambda i: float(i["properties"].get("eo:cloud_cover", 100)), reverse=True):
        visual, mask = _cog_url(item, "visual"), _cog_url(item, "scl")
        if not visual or not mask:
            continue
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                visual_job = pool.submit(_read_cog, visual, [1, 2, 3], bbox, transform, np.uint8, Resampling.bilinear)
                mask_job = pool.submit(_read_cog, mask, [1], bbox, transform, np.uint8, Resampling.nearest)
                candidate, classification = visual_job.result(), mask_job.result()[0]
            valid = (candidate.max(axis=0) > 0) & (classification > 0)
            rgb[:, valid] = candidate[:, valid]
            scl[valid] = classification[valid]
            if np.any(valid):
                contributed.append({"item_id": item["id"], "visual_url": visual, "classification_url": mask,
                                    "scene_cloud_cover_pct": item["properties"].get("eo:cloud_cover")})
        except (rasterio.errors.RasterioError, OSError, ValueError, ConnectorError):
            failed += 1
            continue
    valid = scl > 0
    coverage = float(valid.mean())
    if coverage < 0.95:
        if failed:
            raise OSError("One or more optical tiles could not be read; retry this date")
        return None
    cloudy = np.isin(scl, [3, 8, 9, 10]) & valid  # shadow, cloud, cirrus
    snow = (scl == 11) & valid  # visible surface cover, not atmospheric obscuration
    cloud_pct = float(cloudy.sum() / valid.sum() * 100)
    rgba = np.moveaxis(np.concatenate([rgb, (valid * 255)[None].astype(np.uint8)], axis=0), 0, -1)
    output = BytesIO()
    Image.fromarray(rgba).save(output, format="PNG", optimize=True)
    return output.getvalue(), {"coverage_pct": round(coverage*100, 2), "obscured_pct": round(cloud_pct, 2),
                               "snow_ice_pct": round(float(snow.sum()/valid.sum()*100), 2),
                               "classification": "Sentinel-2 L2A SCL classes 3, 8, 9, 10 for cloud/shadow/cirrus; class 11 snow/ice reported separately",
                               "items": contributed}


async def collect_optical_evidence(result: dict, directory: Path) -> list[dict]:
    """Return archived optical images, or [] when the data/service is unavailable.

    Before and after search ±21 days around the NISAR acquisitions. The recent
    image is from a clear available day in the last two weeks, never a live stream.
    The result list contains source IDs, dates, quality, and checksums.
    """
    bbox = _bbox_from_result(result)
    if not bbox:
        return []
    inputs = result.get("provenance", {}).get("inputs", [])
    dates = []
    for source in inputs[:2]:
        try:
            dates.append(_date(source["metadata"]["date"]))
        except (KeyError, ValueError, TypeError):
            return []
    if not dates:
        return []
    now = datetime.now(timezone.utc)
    requests = [("before", dates[0])]
    if len(dates) == 2:
        requests.append(("after", dates[1]))
    requests.append(("recent", now))
    output: list[dict] = []
    previous: dict[str, dict] = {}
    for entry in result.get("optical_evidence", []):
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in {"before", "after", "recent"} or entry.get("artifact_name") != f"optical-{kind}.png":
            continue
        quality = entry.get("quality", {})
        path = directory / entry["artifact_name"]
        try:
            if (entry.get("bounds") == bbox and quality["coverage_pct"] >= 95
                    and quality["obscured_pct"] <= (25 if kind == "recent" else 35)
                    and entry.get("source_url", "").startswith("https://earth-search.aws.element84.com/v1/collections/")
                    and hashlib.sha256(path.read_bytes()).hexdigest() == entry.get("sha256")):
                previous[kind] = entry
        except (KeyError, OSError, TypeError, ValueError):
            continue
    deadline = time.monotonic() + 60

    async def one(client, kind: str, target: datetime) -> list[dict]:
        output: list[dict] = []
        if True:  # noqa: SIM108 - kept flat for readability of the search loop
            kind_deadline = min(deadline, time.monotonic() + (50 if kind != "recent" else 35))
            if kind == "recent":
                start, end = now - timedelta(days=14), now
            else:
                start, end = target - timedelta(days=WINDOW_DAYS), min(target + timedelta(days=WINDOW_DAYS), now)
            try:
                items = await _search(client, bbox, start, end)
            except (httpx.HTTPError, ConnectorError, ValueError, KeyError):
                if kind in previous:
                    output.append(previous[kind])
                return output
            groups = _candidates(items, target, count=4 if kind == "recent" else 6)
            if kind == "recent":
                groups.sort(key=lambda group: group[0]["properties"]["datetime"], reverse=True)
            best: tuple[list[dict], bytes, dict] | None = None
            for group in groups:
                if time.monotonic() >= kind_deadline:
                    break
                rendered = None
                for attempt in range(2):
                    remaining = kind_deadline - time.monotonic()
                    if remaining < 2:
                        break
                    try:
                        rendered = await asyncio.wait_for(asyncio.to_thread(_render, group, bbox), timeout=min(20, remaining))
                        break
                    except (TimeoutError, OSError):
                        if attempt:
                            break
                if not rendered:
                    continue
                blob, quality = rendered
                obscured = quality["obscured_pct"]
                ceiling = 25 if kind == "recent" else 35
                if obscured > ceiling:
                    continue
                if best is None or obscured < best[2]["obscured_pct"]:
                    best = group, blob, quality
                # A mostly clear scene close to the radar acquisition is sufficient.
                if obscured <= 12:
                    break
            old = previous.get(kind)
            if old is not None and (best is None or
                    old["quality"]["obscured_pct"] + 2 * (100 - old["quality"]["coverage_pct"])
                    <= best[2]["obscured_pct"] + 2 * (100 - best[2]["coverage_pct"])):
                output.append(old)
                return output
            if best is not None:
                group, blob, quality = best
                name = f"optical-{kind}.png"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / name).write_bytes(blob)
                item = group[0]
                output.append({"kind": kind, "date": item["properties"]["datetime"],
                    "sensor": "Copernicus Sentinel-2 MSI, L2A true color", "resolution_m": 10,
                    "source_url": f"https://earth-search.aws.element84.com/v1/collections/{COLLECTION}/items/{item['id']}",
                    "artifact_name": name, "bounds": bbox,
                    "cloud_note": "Cloud/obscuration estimated from the AOI's SCL pixels, not the whole scene. Optical context only; not used in radar computation.",
                    "quality": quality, "sha256": hashlib.sha256(blob).hexdigest(),
                    "temporal_offset_days": round(( _date(item["properties"]["datetime"]) - target).total_seconds()/86400, 2)})
        return output

    # Before, after and recent images are independent; search and render them concurrently.
    async with ProviderClient("stac", timeout=12) as client:
        found = await asyncio.gather(*(one(client, kind, target) for kind, target in requests))
    return [entry for group in found for entry in group]
