"""What a drone's camera is looking at, from real data, for the drones' field notes.

At the ground point under a camera's line of sight: Landsat 8/9 land-surface temperature (the most recent mostly
clear scene), this investigation's measured radar backscatter change, ESA WorldCover land cover, Copernicus ground
height, and the current weather from Open-Meteo. Every value names its source; nothing is invented. The data are
loaded once per area and kept in memory for ten minutes.
"""
from __future__ import annotations

import asyncio
import base64
import math
import re
import threading
import time
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

KEEP_S = 600
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _run(coro):
    """Run an async loader from a worker thread (activities call this module from threads)."""
    return asyncio.run(coro)


def _grid(png_b64: str, mode: str) -> np.ndarray:
    return np.array(Image.open(BytesIO(base64.b64decode(png_b64))).convert(mode))


def area_data(bbox: list[float], investigation_id: str | None) -> dict:
    key = f"{[round(v, 4) for v in bbox]}:{investigation_id}"
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < KEEP_S:
            return hit[1]
    data: dict = {"bbox": bbox, "investigation_id": investigation_id}
    try:
        from ..thermal import thermal
        th = _run(thermal(bbox))
        if th.get("available"):
            la = _grid(th["png"], "LA")
            celsius = th["minC"] + la[..., 0].astype(np.float32) / 255 * (th["maxC"] - th["minC"])
            valid = la[..., 1] > 0
            data["thermal"] = {"c": np.where(valid, celsius, np.nan), "bounds": th["bounds"], "date": th["date"], "platform": th.get("platform", "Landsat"),
                               "p95": float(np.nanpercentile(celsius[valid], 95)) if valid.any() else None,
                               "median": float(np.nanmedian(celsius[valid])) if valid.any() else None}
    except Exception:
        pass
    try:
        from ..terrain import terrain_context
        ctx = _run(terrain_context(bbox))
        lc = ctx.get("landcover")
        if lc:
            data["cover"] = {"codes": _grid(lc["grid_png"], "L"), "bounds": lc["bounds"], "names": {int(k): v["name"] for k, v in lc["classes"].items()}}
    except Exception:
        pass
    try:
        from ..conditions import FORECAST
        from ..connector import request
        lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        response = _run(request("openmeteo", "GET", FORECAST, timeout=8, max_bytes=50_000, params={
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "current": "temperature_2m,wind_speed_10m,wind_direction_10m,cloud_cover,relative_humidity_2m"}))
        current = response.json().get("current") or {}
        data["weather"] = {k: current.get(k) for k in ("temperature_2m", "wind_speed_10m", "wind_direction_10m", "cloud_cover", "relative_humidity_2m", "time")}
    except Exception:
        pass
    if investigation_id and re.fullmatch(r"[A-Za-z0-9-]{1,80}", investigation_id):
        from ..config import get_settings
        path = get_settings().artifacts / investigation_id / "change.tif"
        if path.exists():
            data["change_path"] = str(path)
        try:  # Sentinel-2 before/after images the investigation attached: an independent check of what changed
            from ..store import Store
            item = Store(get_settings().database).investigation(investigation_id) or {}
            optical = {}
            for o in (item.get("result") or {}).get("optical_evidence") or []:
                f = get_settings().artifacts / investigation_id / o.get("artifact_name", "")
                if o.get("kind") in ("before", "after") and f.is_file():
                    optical[o["kind"]] = {"rgb": _grid(base64.b64encode(f.read_bytes()).decode(), "RGB").astype(np.float32), "bounds": o["bounds"], "date": o["date"][:10]}
            if len(optical) == 2:
                data["optical"] = optical
        except Exception:
            pass
    with _lock:
        _cache[key] = (time.time(), data)
    return data


def _pixel(grid: np.ndarray, bounds: list[float], lon: float, lat: float):
    w, s, e, n = bounds
    if not (w <= lon <= e and s <= lat <= n):
        return None
    h, wd = grid.shape[:2]
    return grid[min(h - 1, int((n - lat) / (n - s) * h)), min(wd - 1, int((lon - w) / (e - w) * wd))]


def _change_db(path: str, lon: float, lat: float) -> float | None:
    import rasterio
    from rasterio.warp import transform as warp
    try:
        with rasterio.open(path) as ds:
            xs, ys = warp("EPSG:4326", ds.crs, [lon], [lat])
            row, col = ds.index(xs[0], ys[0])
            if not (0 <= row < ds.height and 0 <= col < ds.width):
                return None
            value = float(ds.read(1, window=((row, row + 1), (col, col + 1)))[0, 0])
            return round(value, 2) if math.isfinite(value) and (ds.nodata is None or value != ds.nodata) else None
    except Exception:
        return None


def observe(data: dict, lon: float, lat: float, elevation: float | None = None) -> dict:
    """Values at one ground point, each with its source."""
    out: dict = {"lon": round(lon, 6), "lat": round(lat, 6)}
    th = data.get("thermal")
    if th:
        v = _pixel(th["c"], th["bounds"], lon, lat)
        if v is not None and np.isfinite(v):
            out["surface_c"] = round(float(v), 1); out["surface_source"] = f"{th['platform']} surface temperature, {th['date']}"
            if th.get("p95") is not None:
                out["surface_p95_c"] = round(th["p95"], 1)
            if th.get("median") is not None:
                out["surface_median_c"] = round(th["median"], 1)
    cv = data.get("cover")
    if cv:
        code = _pixel(cv["codes"], cv["bounds"], lon, lat)
        if code:
            out["cover"] = cv["names"].get(int(code))
    if data.get("change_path"):
        db = _change_db(data["change_path"], lon, lat)
        if db is not None:
            out["change_db"] = db
    opt = data.get("optical")
    if opt:
        def sample(img: dict):
            w, s, e, n = img["bounds"]; h, wd = img["rgb"].shape[:2]
            if not (w <= lon <= e and s <= lat <= n):
                return None
            r, c = int((n - lat) / (n - s) * h), int((lon - w) / (e - w) * wd)
            patch = img["rgb"][max(0, r - 1):r + 2, max(0, c - 1):c + 2].reshape(-1, 3).mean(axis=0) / 255
            return float(patch.mean()), float((patch[1] - patch[0]) / (patch[1] + patch[0] + 0.02)), float(patch.max() - patch.min())
        b, a = sample(opt["before"]), sample(opt["after"])
        if b and a:
            out["optical"] = {"before_date": opt["before"]["date"], "after_date": opt["after"]["date"], "brightness_before": round(b[0], 3), "brightness_after": round(a[0], 3),
                              "greenness_before": round(b[1], 3), "greenness_after": round(a[1], 3), "d_brightness": round(a[0] - b[0], 3), "d_greenness": round(a[1] - b[1], 3),
                              "colour_before": round(b[2], 3), "colour_after": round(a[2], 3)}
    if elevation is not None:
        out["elevation_m"] = round(elevation)
    w = data.get("weather")
    if w and w.get("temperature_2m") is not None:
        out["weather"] = w
    return out


def ground_point(lon: float, lat: float, agl: float, heading: float, pitch: float) -> tuple[float, float]:
    """Where a camera's line of sight meets locally level ground."""
    if pitch > -2:
        pitch = -2
    d = min(6000.0, agl / math.tan(math.radians(-pitch)))
    h = math.radians(heading)
    return lon + math.sin(h) * d / (111_320 * math.cos(math.radians(lat))), lat + math.cos(h) * d / 110_574


def dm(lat: float, lon: float) -> str:
    def f(v: float, pos: str, neg: str, width: int) -> str:
        a = abs(v); d = int(a); m = (a - d) * 60
        return f"{d:0{width}d}°{m:05.2f}′{pos if v >= 0 else neg}"
    return f"{f(lat, 'N', 'S', 2)} {f(lon, 'E', 'W', 3)}"


def note_text(drone_name: str, task_label: str, o: dict, reason: str) -> str:
    """A field note in plain words, built only from the observed values."""
    parts = [f"{reason}: {task_label}." if task_label else f"{reason}."]
    where = f"Looking at {dm(o['lat'], o['lon'])}"
    if o.get("elevation_m") is not None:
        where += f", {o['elevation_m']:,} m"
    parts.append(where + ".")
    facts = []
    if o.get("surface_c") is not None:
        facts.append(f"surface {o['surface_c']:.0f} °C ({o['surface_source']})")
    if o.get("change_db") is not None:
        facts.append(f"radar backscatter {'+' if o['change_db'] >= 0 else '−'}{abs(o['change_db']):.1f} dB between the radar dates")
    if o.get("cover"):
        facts.append(o["cover"].lower())
    w = o.get("weather")
    if w:
        wind = w.get("wind_speed_10m"); direction = w.get("wind_direction_10m")
        facts.append(f"air {w['temperature_2m']:.0f} °C" + (f", wind {wind:.0f} km/h from the {COMPASS[int(((direction or 0) + 22.5) // 45) % 8]}" if wind is not None else ""))
    if facts:
        sentence = "; ".join(facts)
        parts.append(sentence[0].upper() + sentence[1:] + ".")
    return " ".join(parts)
