"""Land-surface temperature for the drone cameras' IR mode: Landsat 8/9 Collection 2 Level-2 (band 10).

Scenes are found in Microsoft Planetary Computer's public STAC catalogue and read as bounded windows of the
cloud-optimized GeoTIFFs through the shared connector. Reading needs a short-lived, keyless access token that
the catalogue issues; it is cached server-side and never leaves the server. Clouds, cloud shadows and fill are
masked with the scene's QA_PIXEL band. The values are measured surface temperature at about 100 m resolution
(delivered on a 30 m grid) at the scene's overpass time, not live temperature.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from .connector import request

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2"
SCALE, OFFSET = 0.00341802, 149.0  # band 10 digital number to kelvin
CLOUDY = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 4)  # fill, dilated cloud, cloud, cloud shadow
CACHE_SECONDS = 12 * 3600
_token: dict = {"value": None, "expires": 0.0}
_memory: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


async def _access_token() -> str:
    if _token["value"] and time.time() < _token["expires"] - 300:
        return _token["value"]
    payload = (await request("planetary", "GET", TOKEN, timeout=15, max_bytes=100_000)).json()
    expiry = datetime.fromisoformat(payload["msft:expiry"].replace("Z", "+00:00")).timestamp()
    _token.update(value=payload["token"], expires=expiry)
    return payload["token"]


async def _scenes(bbox: list[float], days: int) -> list[dict]:
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    body = {"collections": ["landsat-c2-l2"], "bbox": bbox, "limit": 8, "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
            "query": {"eo:cloud_cover": {"lt": 60}, "platform": {"in": ["landsat-8", "landsat-9"]}},
            "sortby": [{"field": "properties.datetime", "direction": "desc"}]}
    response = await request("planetary", "POST", STAC, json=body, timeout=20, max_bytes=5_000_000)
    return response.json().get("features", [])


def _render(scene: dict, token: str, bbox: list[float]) -> dict | None:
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from .optical import _read_cog, _shape
    # Band 10 is ~100 m data on a 30 m grid; about 600 cells across is plenty.
    full_h, full_w = _shape(bbox); k = 600 / max(full_h, full_w)
    height, width = max(8, round(full_h * k)), max(8, round(full_w * k))
    transform = from_bounds(*bbox, width, height)
    href = lambda name: f"{scene['assets'][name]['href']}?{token}"  # noqa: E731 - signed for this read only
    band = _read_cog(href("lwir11"), [1], bbox, transform, np.uint16, Resampling.bilinear, provider="landsat", shape=(height, width))[0]
    qa = _read_cog(href("qa_pixel"), [1], bbox, transform, np.uint16, Resampling.nearest, provider="landsat", shape=(height, width))[0]
    valid = (band > 0) & (qa > 0) & ((qa & CLOUDY) == 0)
    share = float(valid.mean()) if valid.size else 0.0
    if share < 0.5:
        return None
    celsius = band.astype(np.float32) * SCALE + OFFSET - 273.15
    values = celsius[valid]
    low, high = (float(v) for v in np.percentile(values, [1, 99.5]))
    low, high = float(np.floor(low)), float(np.ceil(max(high, low + 1)))
    grey = np.clip((celsius - low) / (high - low) * 255, 0, 255).astype(np.uint8)
    rgba = np.zeros((height, width, 2), dtype=np.uint8); rgba[..., 0] = grey; rgba[..., 1] = np.where(valid, 255, 0)
    png = BytesIO(); Image.fromarray(rgba, mode="LA").save(png, format="PNG", optimize=True)
    props = scene["properties"]
    return {
        "png": base64.b64encode(png.getvalue()).decode(), "bounds": bbox, "minC": low, "maxC": high,
        "date": props["datetime"][:10], "time_utc": props["datetime"][11:16], "scene": scene["id"],
        "platform": props.get("platform", "landsat").replace("landsat-", "Landsat "), "scene_cloud_pct": props.get("eo:cloud_cover"),
        "valid_pct": round(share * 100, 1),
        "stats_c": {"p5": round(float(np.percentile(values, 5)), 1), "median": round(float(np.median(values)), 1),
                    "p95": round(float(np.percentile(values, 95)), 1), "max": round(float(values.max()), 1)},
        "source": f"{props.get('platform', 'Landsat').replace('landsat-', 'Landsat ')} surface temperature (Collection 2 Level-2, band 10)",
    }


def _disk(key: str) -> Path:
    from .config import get_settings
    return get_settings().data_dir / "cache" / f"thermal-{key}.json"


async def thermal(bbox: list[float]) -> dict:
    """The most recent mostly cloud-free surface-temperature scene over the area (searching back 120 days)."""
    bbox = [round(v, 4) for v in bbox]
    key = hashlib.sha256(json.dumps(bbox).encode()).hexdigest()[:20]
    hit = _memory.get(key)
    if not hit:
        try:
            saved = json.loads(_disk(key).read_text()); hit = (float(saved["saved_at"]), saved["value"])
        except (OSError, ValueError, KeyError, TypeError):
            hit = None
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    async with _locks.setdefault(key, asyncio.Lock()):
        scenes = await _scenes(bbox, 120)
        token = await _access_token()
        value: dict = {"available": False, "reason": "No mostly cloud-free Landsat scene of this area in the last 120 days.", "bounds": bbox}
        for scene in scenes[:4]:  # newest first
            if not {"lwir11", "qa_pixel"} <= set(scene.get("assets", {})):
                continue
            result = await asyncio.wait_for(asyncio.to_thread(_render, scene, token, bbox), 45)
            if result:
                value = {"available": True, **result}
                break
        _memory[key] = (time.time(), value)
        try:
            path = _disk(key); path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp"); temporary.write_text(json.dumps({"saved_at": time.time(), "value": value})); temporary.replace(path)
        except OSError:
            pass
        return value
