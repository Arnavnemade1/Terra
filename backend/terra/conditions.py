"""Modelled weather context and bounded geographic profiles.

Weather is context at an acquisition time, not a radar measurement or causal
attribution. A 72-hour total is published only if all 72 hourly amounts exist.
Elevation is Copernicus GLO-90, a surface model, not measured ground deformation.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pyproj import Geod

from .connector import request

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ELEVATION = "https://api.open-meteo.com/v1/elevation"
SOURCE = "Open-Meteo · ERA5 reanalysis or recent forecast-model context"
GEOD = Geod(ellps="WGS84")


def _parse(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Use ISO acquisition timestamps.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value, minimum=None, maximum=None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        return None
    return float(value)


def _summarize(payload: dict, when: datetime, recent: bool) -> dict:
    hourly = payload.get("hourly") or {}
    soil = "soil_moisture_1_to_3cm" if recent else "soil_moisture_0_to_7cm"
    raw_times = hourly.get("time") or []
    if not raw_times or len(raw_times) > 200:
        raise ValueError("The provider returned an unexpected hourly time axis.")
    times = [_parse(value) for value in raw_times]
    index = min(range(len(times)), key=lambda i: abs((times[i] - when).total_seconds()))
    offset = abs((times[index] - when).total_seconds())

    def value(name: str, minimum=None, maximum=None) -> float | None:
        series = hourly.get(name) or []
        if offset > 3600 or index >= len(series):
            return None
        return _number(series[index], minimum, maximum)

    # Hourly precipitation is the amount for the preceding hour. Use the last
    # 72 completed model hours, and report the exact window instead of silently
    # interpolating a partial hour at a satellite's acquisition time.
    end_hour = when.replace(minute=0, second=0, microsecond=0)
    expected = [end_hour - timedelta(hours=i) for i in range(72)]
    by_time: dict[datetime, float | None] = {}
    amounts = hourly.get("precipitation") or []
    duplicates = set()
    for i, timestamp in enumerate(times):
        if timestamp in by_time:
            duplicates.add(timestamp)
        by_time[timestamp] = _number(amounts[i], 0) if i < len(amounts) else None
    for timestamp in duplicates:
        by_time[timestamp] = None
    rain = [by_time.get(timestamp) for timestamp in expected]
    valid_hours = sum(amount is not None for amount in rain)
    total = round(sum(amount for amount in rain if amount is not None), 1) if valid_hours == 72 else None
    soil_value = value(soil, 0, 1)
    temperature, wind, humidity = value("temperature_2m"), value("wind_speed_10m", 0), value("relative_humidity_2m", 0, 100)
    measurements = {"soil_moisture_m3m3": round(soil_value, 3) if soil_value is not None else None,
                    "temperature_c": round(temperature, 1) if temperature is not None else None,
                    "wind_kmh": round(wind, 1) if wind is not None else None,
                    "relative_humidity_pct": round(humidity) if humidity is not None else None}
    return {"date": when.isoformat(timespec="seconds"), "model_time": times[index].isoformat(),
            "model_time_offset_minutes": round(offset / 60, 1),
            "model": "Open-Meteo best-match forecast models" if recent else "ERA5 reanalysis",
            "model_kind": "forecast_model_context" if recent else "reanalysis",
            "resolution": "Model-dependent regional/global grid" if recent else "0.25 degree ERA5 grid (about 25 km)",
            "source_url": FORECAST if recent else ARCHIVE,
            "grid_centre": [_number(payload.get("latitude")), _number(payload.get("longitude"))],
            "precipitation_72h_mm": total, "rain_72h_mm": total,
            "precipitation_definition": "Total precipitation, including snow water equivalent; not liquid rain alone",
            "rain_observed_hours": valid_hours, "rain_expected_hours": 72, "rain_coverage_pct": round(valid_hours / 72 * 100, 1),
            "precipitation_window_start": (end_hour - timedelta(hours=72)).isoformat(),
            "precipitation_window_end": end_hour.isoformat(),
            "soil_layer": "1–3 cm" if recent else "0–7 cm", **measurements,
            "missing_variables": [name for name, measurement in measurements.items() if measurement is None],
            "complete": valid_hours == 72 and all(measurement is not None for measurement in measurements.values())}


async def _at(lat: float, lon: float, when: datetime) -> dict:
    recent = datetime.now(timezone.utc) - when < timedelta(days=7)
    soil = "soil_moisture_1_to_3cm" if recent else "soil_moisture_0_to_7cm"
    start, end = (when - timedelta(days=3)).date().isoformat(), when.date().isoformat()
    params = {"latitude": round(lat, 4), "longitude": round(lon, 4), "start_date": start, "end_date": end, "timezone": "UTC",
              "hourly": f"precipitation,{soil},temperature_2m,wind_speed_10m,relative_humidity_2m",
              "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"}
    if not recent:
        params["models"] = "era5"
    response = await request("openmeteo", "GET", FORECAST if recent else ARCHIVE, params=params, timeout=8, max_bytes=300_000)
    return _summarize(response.json(), when, recent)


async def surface_conditions(bbox: list[float], dates: list[str]) -> dict:
    """≤8 s concurrent model queries; failures become explicit unavailable dates."""
    if len(bbox) != 4 or not all(_number(v) is not None for v in bbox):
        raise ValueError("Use four finite WGS84 coordinates.")
    w, s, e, n = bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90) or e - w > 30 or n - s > 30:
        raise ValueError("Choose a non-crossing regional bounding box spanning at most 30 degrees.")
    if not 1 <= len(dates) <= 2:
        raise ValueError("Supply one or two acquisition dates.")
    parsed = [_parse(value) for value in dates]
    now = datetime.now(timezone.utc)
    if any(when < datetime(1940, 1, 4, tzinfo=timezone.utc) or when > now for when in parsed):
        raise ValueError("Acquisition dates must be in the observed past and within the archive coverage.")
    lat, lon = (s + n) / 2, (w + e) / 2
    results = await asyncio.gather(*(_at(lat, lon, when) for when in parsed), return_exceptions=True)
    out = [result for result in results if isinstance(result, dict)]
    missing = [date for date, result in zip(dates, results) if isinstance(result, BaseException)]
    complete = not missing and all(item["complete"] for item in out)
    return {"source": SOURCE, "centre": [round(lat, 4), round(lon, 4)], "acquisitions": out,
            "unavailable": missing, "status": "available" if complete else "partial" if out else "unavailable",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "note": "Modelled conditions at the area centre, not NISAR measurements. Moisture, roughness, vegetation, water and wind can affect radar returns; these values do not establish a cause. ERA5 and recent forecast soil layers differ and are not directly interchangeable."}


def path_points(vertices: list[tuple[float, float]], n: int) -> tuple[list[tuple[float, float]], list[float]]:
    """Even WGS84 geodesic-distance samples along a bounded lon/lat polyline."""
    if not isinstance(n, int) or isinstance(n, bool) or not 2 <= n <= 256:
        raise ValueError("Choose 2–256 profile samples.")
    if not 2 <= len(vertices) <= 16:
        raise ValueError("A profile needs 2–16 vertices.")
    if any(len(point) != 2 or _number(point[0], -180, 180) is None or _number(point[1], -90, 90) is None for point in vertices):
        raise ValueError("Profile vertices must be finite WGS84 longitude/latitude pairs.")
    if any(abs(a[0] - b[0]) > 180 for a, b in zip(vertices, vertices[1:])):
        raise ValueError("Split antimeridian-crossing profiles into two paths.")
    legs = [GEOD.inv(*a, *b) for a, b in zip(vertices, vertices[1:])]
    total = sum(leg[2] for leg in legs)
    if total < 1 or total > 200_000:
        raise ValueError("A profile must be between 1 metre and 200 kilometres long.")
    points, distances = [], []
    for i in range(n):
        target = total * i / (n - 1)
        cumulative = 0.0
        for j, (azimuth, _, distance) in enumerate(legs):
            if cumulative + distance >= target or j == len(legs) - 1:
                lon, lat, _ = GEOD.fwd(*vertices[j], azimuth, min(distance, max(0, target - cumulative)))
                points.append((lon, lat))
                distances.append(round(target / 1000, 4))
                break
            cumulative += distance
    return points, distances


async def elevations(points: list[tuple[float, float]]) -> list[float | None]:
    """Copernicus GLO-90 surface heights; missing chunks retain null positions."""
    if not 1 <= len(points) <= 256 or any(len(p) != 2 or _number(p[0], -180, 180) is None or _number(p[1], -90, 90) is None for p in points):
        raise ValueError("Use at most 256 finite WGS84 points.")
    async def chunk(part: list[tuple[float, float]]) -> list[float | None]:
        try:
            response = await request("openmeteo", "GET", ELEVATION, timeout=8, max_bytes=100_000, params={
                "latitude": ",".join(f"{p[1]:.5f}" for p in part), "longitude": ",".join(f"{p[0]:.5f}" for p in part)})
            values = response.json().get("elevation")
            if not isinstance(values, list) or len(values) != len(part):
                return [None] * len(part)
            return [_number(value, -12000, 10000) for value in values]
        except Exception:
            return [None] * len(part)
    parts = await asyncio.gather(*(chunk(points[i:i + 100]) for i in range(0, len(points), 100)))
    return [value for part in parts for value in part]


def sample_profile_rasters(directory: Path, points: list[tuple[float, float]], declared_layers: list[dict]) -> list[dict]:
    """Nearest native-pixel values of declared computed artifacts; no interpolation."""
    import rasterio
    from rasterio.warp import transform
    allowed = {Path(str(layer.get("raster_path") or layer.get("path") or "")).stem: layer for layer in declared_layers}
    output = []
    for path in sorted(directory.glob("*.tif"))[:8]:
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            continue
        metadata = allowed.get(path.stem)
        # Science layers expose PNG paths; their paired same-stem TIFF is quantitative.
        if metadata is None:
            metadata = next((layer for layer in declared_layers if layer.get("id") == path.stem), None)
        if metadata is None:
            continue
        try:
            with rasterio.open(path) as raster:
                xs, ys = transform("EPSG:4326", raster.crs, [p[0] for p in points], [p[1] for p in points])
                values = []
                for x, y in zip(xs, ys):
                    if not math.isfinite(x) or not math.isfinite(y):
                        values.append(None)
                        continue
                    row, col = raster.index(x, y)
                    if not (0 <= row < raster.height and 0 <= col < raster.width):
                        values.append(None)
                        continue
                    value = float(raster.read(1, window=((row, row + 1), (col, col + 1)), masked=True).filled(float("nan"))[0, 0])
                    values.append(round(value, 4) if math.isfinite(value) else None)
                output.append({"id": metadata.get("id") or path.stem, "title": metadata.get("title") or path.stem,
                               "units": metadata.get("units", "dB"), "values": values,
                               "sampling": "nearest native pixel; no interpolated measurements"})
        except (rasterio.errors.RasterioError, OSError, ValueError):
            continue
    return output
