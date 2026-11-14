"""Bounded, deterministic NISAR GCOV measurements. No generated observations.

The native GCOV mask is a *subswath* mask, not a layover/shadow mask.
Only diagonal covariance terms (linear gamma0 power) are supported.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.shutil import copy as raster_copy
from rasterio.transform import Affine, array_bounds, from_bounds
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from rasterio.windows import Window, from_bounds as window_from_bounds

METHOD_VERSION = "terra-gcov-1.0.0"
DIAGONALS = ("HHHH", "HVHV", "VVVV", "VHVH", "RHRH", "RVRV")
MAX_PIXELS = 2048 * 2048
NODATA = -9999.0


class ScienceError(ValueError):
    def __init__(self, message: str, code: str = "invalid_scientific_input"):
        super().__init__(message)
        self.code = code


def _value(group: h5py.Group, key: str, default: Any = None) -> Any:
    if key not in group:
        return default
    value = group[key][()]
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.ndarray):
        return [_decode(x) for x in value.flat]
    return _decode(value)


def _decode(value: Any) -> Any:
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode("utf-8", errors="replace")
    return value.item() if isinstance(value, np.generic) else value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value: str | None) -> str:
    if not value:
        raise ScienceError("The acquisition start time is missing.")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScienceError("The acquisition start time is not an ISO timestamp.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt < datetime(2025, 7, 30, tzinfo=timezone.utc):
        raise ScienceError("Pre-launch/simulated acquisitions cannot be presented as NISAR observations.")
    if dt > datetime.now(timezone.utc):
        raise ScienceError("An acquisition time in the future is not an observation.")
    return dt.isoformat().replace("+00:00", "Z")


def _bounds(transform: Affine, shape: tuple[int, int], crs: str) -> list[float]:
    bounds = transform_bounds(crs, "EPSG:4326", *array_bounds(*shape, transform), densify_pts=21)
    if not all(math.isfinite(x) for x in bounds):
        raise ScienceError("The raster cannot be located in WGS84.")
    return list(bounds)


def _inspect_tiff(path: Path, frequency: str, polarization: str | None) -> dict:
    sidecar = Path(str(path) + ".provenance.json")
    if not sidecar.is_file():
        raise ScienceError("A derived GeoTIFF requires a .provenance.json sidecar.")
    source = json.loads(sidecar.read_text())
    required = ("source_url", "source_granule", "date", "product", "polarization", "radiometry", "mask_applied", "maturity")
    missing = [k for k in required if k not in source]
    if missing:
        raise ScienceError("Derivative provenance is incomplete: " + ", ".join(missing))
    pol = source["polarization"]
    if source["product"] != "GCOV" or source["radiometry"] != "gamma0_power" or pol not in DIAGONALS:
        raise ScienceError("Only GCOV diagonal gamma0 power derivatives are supported.")
    if source["mask_applied"] is not True:
        raise ScienceError("The derivative must record that the original subswath mask was applied.")
    if not str(source["source_granule"]).startswith("NISAR_"):
        raise ScienceError("The derivative must identify its source NISAR granule.")
    if polarization and pol != polarization:
        raise ScienceError(f"Requested {polarization}; the derivative contains {pol}.")
    if source.get("frequency", "A") != frequency:
        raise ScienceError("The requested frequency differs from the derivative provenance.")
    with rasterio.open(path) as ds:
        if not ds.crs or ds.count != 1 or ds.transform.b != 0 or ds.transform.d != 0:
            raise ScienceError("A single-band, north-up, georeferenced derivative is required.")
        if ds.transform.a <= 0 or ds.transform.e >= 0:
            raise ScienceError("Unsupported raster orientation.")
        if ds.nodata is None:
            raise ScienceError("The derivative must preserve its nodata mask.")
        return {
            "id": source["source_granule"], "date": _date(source["date"]),
            "product": "GCOV", "maturity": source["maturity"].upper(),
            "polarizations": [pol], "selected_polarization": pol, "frequency": frequency,
            "track": source.get("track"), "frame": source.get("frame"),
            "direction": source.get("direction"), "look_direction": source.get("look_direction"),
            "epsg": ds.crs.to_epsg(), "crs": ds.crs.to_string(),
            "pixel_spacing": [abs(ds.transform.a), abs(ds.transform.e)],
            "shape": [ds.height, ds.width], "transform": list(ds.transform)[:6],
            "bounds": _bounds(ds.transform, (ds.height, ds.width), ds.crs),
            "product_version": source.get("product_version"), "masks": ["derivative_nodata"],
            "processing_release": source.get("processing_release", source.get("product_version")),
            "derivative": True, "source": source, "radiometry": "gamma0_power",
        }


def inspect_gcov(path: str | Path, frequency: str = "A", polarization: str | None = None) -> dict:
    """Inspect coordinates and metadata without loading the full raster.

    TIFF inputs are explicitly labeled derivatives and require provenance.
    This function verifies format/metadata, not independent source authenticity.
    """
    path = Path(path)
    if frequency not in ("A", "B"):
        raise ScienceError("Only frequency A or B is supported.")
    if polarization is not None and polarization not in DIAGONALS:
        raise ScienceError("Only real diagonal covariance terms are supported.")
    if path.suffix.lower() in (".tif", ".tiff"):
        return _inspect_tiff(path, frequency, polarization)
    if path.suffix.lower() not in (".h5", ".hdf5"):
        raise ScienceError("Expected a GCOV HDF5 product or a documented GeoTIFF derivative.")
    with h5py.File(path, "r") as f:
        base = "/science/LSAR"
        if f"{base}/GCOV" not in f or f"{base}/identification" not in f:
            raise ScienceError("This file is not a supported NISAR L-band GCOV product.")
        identification = f[f"{base}/identification"]
        product = _value(identification, "productType")
        mission = _value(identification, "missionId")
        if product != "GCOV" or mission != "NISAR":
            raise ScienceError("The HDF5 identification must declare mission NISAR and product GCOV.")
        group_path = f"{base}/GCOV/grids/frequency{frequency}"
        if group_path not in f:
            raise ScienceError(f"Frequency {frequency} is unavailable in this observation.")
        grid = f[group_path]
        polarizations = [p for p in DIAGONALS if p in grid]
        if not polarizations:
            raise ScienceError("No supported diagonal covariance term is available.")
        pol = polarization or polarizations[0]
        if pol not in polarizations:
            raise ScienceError(f"Polarization {pol} is unavailable in this observation.")
        if "mask" not in grid:
            raise ScienceError("The GCOV subswath quality mask is missing; analysis is refused.")
        xs = np.asarray(grid["xCoordinates"][:], dtype=np.float64)
        ys = np.asarray(grid["yCoordinates"][:], dtype=np.float64)
        dx = float(_value(grid, "xCoordinateSpacing", xs[1] - xs[0] if xs.size > 1 else 0))
        dy = float(_value(grid, "yCoordinateSpacing", ys[1] - ys[0] if ys.size > 1 else 0))
        if dx <= 0 or dy >= 0 or not np.allclose(np.diff(xs), dx) or not np.allclose(np.diff(ys), dy):
            raise ScienceError("Only regular north-up GCOV grids are supported.")
        shape = grid[pol].shape
        if shape != (len(ys), len(xs)) or grid["mask"].shape != shape:
            raise ScienceError("Covariance, coordinate and quality-mask dimensions disagree.")
        if np.issubdtype(grid[pol].dtype, np.complexfloating):
            raise ScienceError("Complex covariance terms cannot be treated as backscatter power.")
        epsg = int(_value(grid, "projection"))
        transform = Affine(dx, 0, float(xs[0]) - dx / 2, 0, dy, float(ys[0]) - dy / 2)
        version = _value(identification, "productVersion")
        granule = _value(identification, "granuleId", path.stem)
        maturity = str(_value(identification, "dataMaturity", "UNKNOWN")).upper()
        sidecar = Path(str(path) + ".provenance.json")
        looks = 1
        if sidecar.is_file():
            subset = json.loads(sidecar.read_text())
            looks = int(subset.get("multilook") or 1)
            if maturity == "UNKNOWN":
                # AOI subsets record the catalog's published maturity for the source granule.
                maturity = str(subset.get("maturity") or "UNKNOWN").upper()
        release_match = re.search(r"_([A-Z]\d{5})_[A-Z]_[A-Z]_[A-Z]_\d{3}$", granule)
        return {
            "id": granule,
            "date": _date(_value(identification, "zeroDopplerStartTime")),
            "product": product, "maturity": maturity,
            "polarizations": polarizations, "selected_polarization": pol, "frequency": frequency,
            "track": _value(identification, "trackNumber"), "frame": _value(identification, "frameNumber"),
            "direction": _value(identification, "orbitPassDirection"),
            "look_direction": _value(identification, "lookDirection"),
            "epsg": epsg, "crs": f"EPSG:{epsg}", "pixel_spacing": [abs(dx), abs(dy)],
            "shape": list(shape), "transform": list(transform)[:6],
            "bounds": _bounds(transform, shape, f"EPSG:{epsg}"),
            "product_version": version, "masks": [k for k in ("mask", "inputDataExceptionMask", "layoverShadowMask") if k in grid],
            "processing_release": _value(identification, "compositeReleaseId", release_match.group(1) if release_match else None),
            "derivative": False, "radiometry": "gamma0_power", "grid_path": group_path, "multilook": looks,
        }


def _window(meta: dict, bbox: list[float] | None) -> tuple[slice, slice, Affine]:
    height, width = meta["shape"]
    transform = Affine(*meta["transform"])
    if bbox:
        if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
            raise ScienceError("The requested bounds must contain four finite coordinates.")
        west, south, east, north = bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ScienceError("Use a valid WGS84 bounding box; split antimeridian-crossing regions.")
        projected = transform_bounds("EPSG:4326", meta["crs"], *bbox, densify_pts=21)
        window = window_from_bounds(*projected, transform=transform)
        c0 = max(0, math.floor(window.col_off))
        r0 = max(0, math.floor(window.row_off))
        c1 = min(width, math.ceil(window.col_off + window.width))
        r1 = min(height, math.ceil(window.row_off + window.height))
    else:
        r0, c0, r1, c1 = 0, 0, height, width
    if r0 >= r1 or c0 >= c1:
        raise ScienceError("The selected region does not intersect this observation.", "no_coverage")
    if (r1 - r0) * (c1 - c0) > MAX_PIXELS:
        raise ScienceError("This selection exceeds 4,194,304 native pixels. Select a smaller region; scientific data is never silently downsampled.", "region_too_large")
    return slice(r0, r1), slice(c0, c1), transform * Affine.translation(c0, r0)


VALIDITY_BITS = {"HH": 256, "HV": 512, "VH": 1024, "VV": 2048, "LH": 4096, "LV": 8192, "RH": 16384, "RV": 32768}


def exception_quality(exceptions: np.ndarray, pol: str) -> np.ndarray:
    """Interpret the GCOV input-data-exception mask as documented in the product.

    The low byte holds anomaly codes (0 = no anomaly, e.g. 2 = qFSP sample slip). Recent
    releases also OR in per-polarization validity bits (256 = HH fully focused, 512 = HV, ...).
    A pixel is usable when it has no anomaly code and, when validity bits are present, the
    bit for the analysed polarization is set. 65535 is the fill value.
    """
    codes = np.asarray(exceptions, dtype=np.uint32)
    usable = ((codes & 0xFF) == 0) & (codes != 65535)
    bit = VALIDITY_BITS.get(pol[:2])
    if bit and np.any(codes[codes != 65535] & 0xFF00):
        usable &= (codes & bit) != 0
    return usable


def _read(path: Path, meta: dict, bbox: list[float] | None) -> tuple[np.ndarray, np.ndarray, Affine, dict]:
    rows, cols, transform = _window(meta, bbox)
    pol = meta["selected_polarization"]
    if meta["derivative"]:
        with rasterio.open(path) as ds:
            raw = ds.read(1, window=Window.from_slices(rows, cols), masked=True)
            power = np.asarray(raw.data, dtype=np.float32)
            quality = ~np.ma.getmaskarray(raw)
    else:
        with h5py.File(path, "r") as f:
            grid = f[meta["grid_path"]]
            power = np.asarray(grid[pol][rows, cols], dtype=np.float32)
            mask = grid["mask"][rows, cols]
            quality = (mask > 0) & (mask < 255)
            if "inputDataExceptionMask" in grid:
                quality &= exception_quality(grid["inputDataExceptionMask"][rows, cols], pol)
            if "layoverShadowMask" in grid:
                quality &= grid["layoverShadowMask"][rows, cols] == 0
            fill = grid[pol].attrs.get("_FillValue")
            if fill is not None and np.isfinite(fill):
                quality &= power != fill
    inside = np.ones(power.shape, dtype=bool)
    if bbox:
        rr, cc = np.indices(power.shape, dtype=np.float64)
        xs, ys = transform * (cc + 0.5, rr + 0.5)
        lon, lat = Transformer.from_crs(meta["crs"], "EPSG:4326", always_xy=True).transform(xs, ys)
        inside = (lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3])
    valid = quality & np.isfinite(power) & (power > 0) & inside
    if not np.any(valid):
        raise ScienceError("No finite, positive, quality-masked observations intersect the region.", "no_valid_pixels")
    values = np.full(power.shape, np.nan, dtype=np.float32)
    values[valid] = 10.0 * np.log10(power[valid])
    return values, inside, transform, {
        "pixel_window": {"row_start": rows.start, "row_stop": rows.stop, "column_start": cols.start, "column_stop": cols.stop},
        "requested_pixels": int(inside.sum()), "valid_pixels": int(valid.sum()),
    }


def check_compatibility(first: dict, second: dict) -> list[dict]:
    """Fail closed on missing or incompatible acquisition/processing metadata."""
    checks = []
    for key in ("product", "frequency", "selected_polarization", "track", "frame", "direction", "look_direction", "maturity", "product_version", "processing_release", "crs", "shape", "pixel_spacing"):
        a, b = first.get(key), second.get(key)
        if a is None or b is None or a == "UNKNOWN" or b == "UNKNOWN":
            raise ScienceError(f"Cannot compare acquisitions: {key} is unknown.", "incompatible_observations")
        if a != b:
            raise ScienceError(f"Cannot compare acquisitions: {key} differs.", "incompatible_observations")
        checks.append({"name": key, "passed": True})
    if not np.allclose(first["transform"], second["transform"], atol=1e-6, rtol=0):
        raise ScienceError("The native raster grids do not align; automatic resampling is not enabled.", "incompatible_observations")
    if first["date"] >= second["date"]:
        raise ScienceError("The second acquisition must be later than the first.", "incompatible_observations")
    checks.extend([{"name": "native_grid_alignment", "passed": True}, {"name": "chronological_order", "passed": True}])
    return checks


def _summary(values: np.ndarray, inside: np.ndarray, meta: dict, edges: tuple[float, float, float] = (-30.0, 5.0, 1.0)) -> dict:
    valid = values[np.isfinite(values)]
    lo, hi, step = edges
    bins = np.arange(lo, hi + step / 2, step)
    counts, _ = np.histogram(np.clip(valid, lo, hi - 1e-6), bins=bins)
    percentiles = np.percentile(valid, [5, 25, 50, 75, 95])
    area = abs(meta["pixel_spacing"][0] * meta["pixel_spacing"][1]) / 1e6
    crs = rasterio.crs.CRS.from_string(meta["crs"])
    projected = crs.is_projected and crs.linear_units == "metre"
    return {
        "valid_pixels": int(valid.size), "requested_pixels": int(inside.sum()),
        "coverage_pct": round(float(valid.size / max(1, inside.sum()) * 100), 4),
        "mean_db": float(valid.mean(dtype=np.float64)), "median_db": float(percentiles[2]),
        "std_db": float(valid.std(dtype=np.float64)), "p05_db": float(percentiles[0]),
        "p95_db": float(percentiles[4]), "iqr_db": float(percentiles[3] - percentiles[1]),
        "min_db": float(valid.min()), "max_db": float(valid.max()),
        "valid_projected_area_km2": float(valid.size * area) if projected else None,
        "pixel_spacing": meta["pixel_spacing"], "units": "dB",
        "histogram": {"edges": [round(float(e), 3) for e in bins], "counts": [int(c) for c in counts], "clipped_to_range": True},
    }


# Descriptive backscatter classes (dB). At L-band, calm open water usually falls below LOW_RETURN_DB;
# buildings, double bounce (including flooded vegetation) and slopes facing the radar exceed HIGH_RETURN_DB.
LOW_RETURN_DB = -18.0
HIGH_RETURN_DB = -3.0
HOTSPOT_MIN_PIXELS = 12
HOTSPOT_LIMIT = 6


def _pixel_km2(meta: dict) -> float | None:
    crs = rasterio.crs.CRS.from_string(meta["crs"])
    if not (crs.is_projected and crs.linear_units == "metre"):
        return None
    return abs(meta["pixel_spacing"][0] * meta["pixel_spacing"][1]) / 1e6


def _smoothed_db(values: np.ndarray) -> np.ndarray:
    """3×3 average in linear power, ignoring invalid samples; used only to classify and group pixels."""
    from scipy import ndimage
    finite = np.isfinite(values)
    power = np.where(finite, 10 ** (np.where(finite, values, 0) / 10), 0.0)
    weight = ndimage.uniform_filter(finite.astype(np.float64), 3, mode="constant")
    mean = ndimage.uniform_filter(power, 3, mode="constant")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(finite & (weight > 0.5), 10 * np.log10(mean / weight), np.nan)


def _surface_classes(values: np.ndarray, meta: dict) -> dict:
    smooth = _smoothed_db(values)
    valid = np.isfinite(smooth)
    n = max(1, int(valid.sum()))
    km2 = _pixel_km2(meta)
    low, high = valid & (smooth < LOW_RETURN_DB), valid & (smooth > HIGH_RETURN_DB)
    return {"low_return_threshold_db": LOW_RETURN_DB, "low_return_pct": float(low.sum() / n * 100),
            "low_return_km2": float(low.sum() * km2) if km2 else None,
            "high_return_threshold_db": HIGH_RETURN_DB, "high_return_pct": float(high.sum() / n * 100),
            "high_return_km2": float(high.sum() * km2) if km2 else None}


def _hotspots(delta: np.ndarray, transform: Affine, crs: str, meta: dict, threshold: float) -> list[dict]:
    """The largest connected patches whose smoothed change exceeds the threshold, with locations."""
    from scipy import ndimage
    from pyproj import Transformer
    finite = np.isfinite(delta)
    filled = np.where(finite, delta, 0.0).astype(np.float64)
    weight = ndimage.uniform_filter(finite.astype(np.float64), 3, mode="constant")
    with np.errstate(divide="ignore", invalid="ignore"):
        smooth = np.where(finite & (weight > 0.5), ndimage.uniform_filter(filled, 3, mode="constant") / weight, np.nan)
    km2 = _pixel_km2(meta)
    to_geo = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    patches = []
    for sign, direction in ((1, "increase"), (-1, "decrease")):
        mask = np.isfinite(smooth) & (sign * np.nan_to_num(smooth) >= threshold)
        mask = ndimage.binary_opening(mask)
        labels, count = ndimage.label(mask)
        if not count:
            continue
        index = np.arange(1, count + 1)
        sizes = ndimage.sum_labels(mask, labels, index)
        keep = index[sizes >= HOTSPOT_MIN_PIXELS]
        if not keep.size:
            continue
        slices = ndimage.find_objects(labels)
        for label in keep[np.argsort(-sizes[keep - 1])][:HOTSPOT_LIMIT]:
            region = labels[slices[label - 1]] == label
            values = delta[slices[label - 1]][region]
            values = values[np.isfinite(values)]
            rows, cols = np.nonzero(region)
            r0, c0 = slices[label - 1][0].start, slices[label - 1][1].start
            cy, cx = rows.mean() + r0 + 0.5, cols.mean() + c0 + 0.5
            x, y = transform * (cx, cy)
            lon, lat = to_geo.transform(x, y)
            corners = [transform * (c, r) for c, r in ((c0, r0), (slices[label - 1][1].stop, slices[label - 1][0].stop))]
            (w, n), (e, s) = [to_geo.transform(*corner) for corner in corners]
            patches.append({"direction": direction, "pixels": int(sizes[label - 1]),
                            "area_km2": round(float(sizes[label - 1] * km2), 4) if km2 else None,
                            "mean_change_db": round(float(values.mean()), 3) if values.size else None,
                            "peak_change_db": round(float(values[np.argmax(np.abs(values))]), 3) if values.size else None,
                            "centre": [round(float(lat), 5), round(float(lon), 5)],
                            "bounds": [round(float(min(w, e)), 5), round(float(min(s, n)), 5), round(float(max(w, e)), 5), round(float(max(s, n)), 5)]})
    patches.sort(key=lambda p: -p["pixels"])
    for rank, patch in enumerate(patches[:HOTSPOT_LIMIT], 1):
        patch["rank"] = rank
    return patches[:HOTSPOT_LIMIT]


def _colors(values: np.ndarray, low: float, high: float, diverging: bool) -> np.ndarray:
    # Fixed, documented colors. Scientific values remain unchanged in the COG.
    palette = np.array([[45, 97, 165], [116, 173, 209], [242, 242, 232], [233, 150, 90], [180, 66, 55]] if diverging else
                       [[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]], dtype=float)
    t = np.clip((np.nan_to_num(values, nan=low) - low) / (high - low), 0, 1)
    stops = np.linspace(0, 1, len(palette))
    rgb = np.stack([np.interp(t, stops, palette[:, c]) for c in range(3)], axis=-1).astype(np.uint8)
    return np.concatenate([rgb, (np.isfinite(values) * 235).astype(np.uint8)[..., None]], axis=-1)


def _export(values: np.ndarray, transform: Affine, crs: str, out: Path, name: str, title: str, diverging: bool = False) -> dict:
    height, width = values.shape
    tmp = out / f".{name}.tif"
    path = out / f"{name}.tif"
    with rasterio.open(tmp, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32", crs=crs,
                       transform=transform, nodata=NODATA, compress="deflate") as ds:
        ds.write(np.where(np.isfinite(values), values, NODATA).astype(np.float32), 1)
        ds.update_tags(AREA_OR_POINT="Area", method=METHOD_VERSION, units="dB", interpretation="backscatter change" if diverging else "gamma0 backscatter")
    raster_copy(tmp, path, driver="COG", compress="DEFLATE", blocksize=256, overview_resampling="NEAREST")
    tmp.unlink()
    source_bounds = array_bounds(height, width, transform)
    target_transform, target_width, target_height = calculate_default_transform(crs, "EPSG:4326", width, height, *source_bounds)
    factor = max(target_width, target_height) / 1600
    if factor > 1:
        target_width = max(1, math.ceil(target_width / factor))
        target_height = max(1, math.ceil(target_height / factor))
        target_transform = from_bounds(*transform_bounds(crs, "EPSG:4326", *source_bounds, densify_pts=21), target_width, target_height)
    preview = np.full((target_height, target_width), np.nan, dtype=np.float32)
    reproject(values, preview, src_transform=transform, src_crs=crs, src_nodata=np.nan,
              dst_transform=target_transform, dst_crs="EPSG:4326", dst_nodata=np.nan, resampling=Resampling.nearest)
    low, high = (-6.0, 6.0) if diverging else (-30.0, 0.0)
    preview_path = out / f"{name}.png"
    Image.fromarray(_colors(preview, low, high, diverging)).save(preview_path)
    bounds = list(array_bounds(target_height, target_width, target_transform))
    return {
        "id": name, "title": title, "path": preview_path.name, "cog_path": path.name,
        "bounds": bounds, "units": "dB", "min": low, "max": high,
        "colormap": "blue-neutral-orange" if diverging else "viridis",
        "preview_resampling": "nearest", "preview_shape": [target_height, target_width],
        "native_shape": [height, width], "sha256": _hash(path), "preview_sha256": _hash(preview_path),
    }


def run_analysis(manifest: dict, output_dir: str | Path) -> dict:
    """Compute one observation or a strictly compatible two-date dB difference."""
    inputs = manifest.get("inputs", [])
    if len(inputs) not in (1, 2):
        raise ScienceError("Provide one observation for backscatter or two for temporal comparison.")
    root = Path(manifest.get("input_root", ".data/inputs")).resolve()
    paths = []
    for item in inputs:
        supplied = Path(item["path"])
        path = supplied.resolve()
        if not path.is_relative_to(root) or supplied.is_symlink() or not path.is_file():
            raise ScienceError("Input must be an existing file within the configured input directory.", "input_not_allowed")
        paths.append(path)
    threshold = float(manifest.get("threshold_db", 3.0))
    if not math.isfinite(threshold) or not 0 < threshold <= 30:
        raise ScienceError("The descriptive change threshold must be between 0 and 30 dB.")
    frequency = manifest.get("frequency", "A")
    pol = manifest.get("polarization")
    metadata = [inspect_gcov(path, frequency, pol) for path in paths]
    for meta, entry in zip(metadata, inputs):
        observation = entry.get("observation", {})
        if observation.get("maturity") and meta["maturity"] == "UNKNOWN":
            meta["maturity"] = str(observation["maturity"]).upper()
            meta["maturity_source"] = "catalog"
    checks = [{"name": "supported_gcov_diagonal_power", "passed": True}, {"name": "acquisition_date_after_launch", "passed": True}]
    if len(inputs) == 2:
        checks += check_compatibility(*metadata)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    bbox = manifest.get("bbox")
    arrays, inside_masks, windows, transforms = [], [], [], []
    for path, meta in zip(paths, metadata):
        values, inside, transform, window = _read(path, meta, bbox)
        arrays.append(values)
        inside_masks.append(inside)
        transforms.append(transform)
        windows.append(window)
    layers = []
    observations = []
    for index, (values, inside, transform, meta) in enumerate(zip(arrays, inside_masks, transforms, metadata)):
        layers.append(_export(values, transform, meta["crs"], out, f"backscatter-{index + 1}", f"{meta['selected_polarization']} backscatter · {meta['date'][:10]}"))
        layers[-1]["date"] = meta["date"]
        observations.append({"date": meta["date"], "id": meta["id"], **_summary(values, inside, meta), **_surface_classes(values, meta)})
    measurements = {"kind": "backscatter_summary", "observations": observations}
    limitations = [
        "Computational checks are not independent scientific validation.",
        "Backscatter depends on moisture, roughness, vegetation, acquisition conditions and residual noise; it does not uniquely identify an environmental event.",
        "No speckle filter is applied. Spatially correlated samples and acquisition noise prevent interpreting pixel counts as independent statistical evidence.",
        "Pixel spacing is not equivalent to effective spatial resolution. Preview colors clip at fixed legend bounds; downloadable float rasters retain the numerical values.",
        "Area uses the native projected pixel area; no terrain-surface area correction or geodesic area integration is performed.",
    ]
    if any(m["derivative"] for m in metadata):
        limitations.append("A third-party or local derivative was analyzed. Its stated provenance and mask application have not been independently checked against the original ASF HDF5 product.")
    if any(m["maturity"] != "VALIDATED" for m in metadata):
        limitations.append("One or more inputs are not identified as VALIDATED mission products; calibration/processing limitations remain.")
    if any("inputDataExceptionMask" not in m["masks"] for m in metadata):
        limitations.append("An input-data-exception mask was not available for every input; unflagged data exceptions may remain.")
    if any("layoverShadowMask" not in m["masks"] for m in metadata):
        limitations.append("No explicit layover/shadow mask was available for every input. The GCOV subswath mask is not a layover/shadow mask.")
    looks = max(int(m.get("multilook") or 1) for m in metadata)
    if looks > 1:
        spacing = metadata[0]["pixel_spacing"][0]
        limitations.append(f"The original samples were averaged in {looks}×{looks} blocks of linear power (multilooked) to {spacing:g} m pixels so this area fits the "
                           f"analysis budget. Detail finer than {spacing:g} m is not resolved; speckle is reduced. Blocks with any invalid sample were excluded.")
    if len(arrays) == 2:
        common = np.isfinite(arrays[0]) & np.isfinite(arrays[1])
        if not np.any(common):
            raise ScienceError("The observations have no common quality-masked pixels.", "no_valid_pixels")
        delta = np.full(arrays[0].shape, np.nan, dtype=np.float32)
        delta[common] = arrays[1][common] - arrays[0][common]
        summary = _summary(delta, inside_masks[0] & inside_masks[1], metadata[0], (-10.0, 10.0, 0.5))
        increasing = int(np.count_nonzero(delta[common] >= threshold))
        decreasing = int(np.count_nonzero(delta[common] <= -threshold))
        # Low-return (water-like) area on both dates, counted only where both dates are valid.
        km2 = _pixel_km2(metadata[0])
        dark = [np.nan_to_num(_smoothed_db(a), nan=0.0) < LOW_RETURN_DB for a in arrays]
        low_before, low_after = int((common & dark[0]).sum()), int((common & dark[1]).sum())
        became, ceased = int((common & ~dark[0] & dark[1]).sum()), int((common & dark[0] & ~dark[1]).sum())
        surface = {"low_return_threshold_db": LOW_RETURN_DB,
                   "low_return_before_pct": float(low_before / common.sum() * 100), "low_return_after_pct": float(low_after / common.sum() * 100),
                   "low_return_before_km2": float(low_before * km2) if km2 else None, "low_return_after_km2": float(low_after * km2) if km2 else None,
                   "newly_low_return_km2": float(became * km2) if km2 else None, "no_longer_low_return_km2": float(ceased * km2) if km2 else None}
        measurements.update({"kind": "backscatter_change", "change": {
            **summary, "threshold_db": threshold, "increasing_pixels": increasing, "decreasing_pixels": decreasing,
            "threshold_exceedance_pct": float((increasing + decreasing) / common.sum() * 100),
            "increasing_km2": float(increasing * km2) if km2 else None, "decreasing_km2": float(decreasing * km2) if km2 else None,
            "threshold_interpretation": "Descriptive magnitude threshold; not a calibrated significance test or event classification.",
            **surface, "hotspots": _hotspots(delta, transforms[0], metadata[0]["crs"], metadata[0], threshold),
            "hotspot_method": f"Connected patches of at least {HOTSPOT_MIN_PIXELS} pixels whose 3×3-averaged change is at least {threshold:g} dB in one direction; largest first.",
        }})
        layers.append(_export(delta, transforms[0], metadata[0]["crs"], out, "change", "Backscatter change · later minus earlier", True))
        limitations.append("Change thresholds are descriptive. No statistical significance, flood extent, vegetation loss, displacement, or causal attribution is inferred.")
        limitations.append(f"Low-return area (below {LOW_RETURN_DB:g} dB after a 3×3 average) is a descriptive proxy: calm open water usually falls below it, "
                           "but so can smooth pavement, dry sand and radar shadow, while wind-roughened water and flooded vegetation may not.")
    else:
        limitations.append("Only one acquisition was analyzed. This result cannot establish temporal change.")
    checks += [{"name": "positive_finite_power_and_quality_mask", "passed": True}, {"name": "native_grid_measurements", "passed": True}, {"name": "georeferenced_artifact_export", "passed": True}]
    provenance = {
        "method": METHOD_VERSION, "formula": "gamma0_db = 10 * log10(gamma0_power); change_db = later_db - earlier_db",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {"bbox": bbox, "frequency": frequency, "polarization": metadata[0]["selected_polarization"], "threshold_db": threshold, "speckle_filter": "none", "maximum_native_pixels": MAX_PIXELS},
        "inputs": [{"filename": p.name, "sha256": _hash(p), "metadata": m, "window": w, "catalog": e.get("observation", {}),
                    "sidecar_sha256": _hash(Path(str(p) + ".provenance.json")) if m["derivative"] else None} for p, m, w, e in zip(paths, metadata, windows, inputs)],
        "software": {"numpy": np.__version__, "rasterio": rasterio.__version__, "h5py": h5py.__version__},
    }
    validation = {"status": "computational_checks_passed", "independent_validation": "not_performed", "checks": checks, "limitations": limitations}
    report = _report(measurements, metadata, limitations)
    result = {"measurements": measurements, "validation": validation, "layers": layers, "provenance": provenance, "report": report}
    (out / "report.md").write_text(report)
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2, allow_nan=False))
    (out / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False))
    return result


def _report(measurements: dict, metadata: list[dict], limitations: list[str]) -> str:
    lines = ["# TERRA observation report", "", "Status: computational checks passed; independent scientific validation has not been performed.", "", "## Evidence"]
    for meta, observation in zip(metadata, measurements["observations"]):
        lines += [f"- {meta['date']} — `{meta['id']}`; {meta['maturity']}; {meta['selected_polarization']}.",
                  f"  {observation['valid_pixels']:,} valid pixels; {observation['coverage_pct']:.2f}% coverage of requested pixel centers; median gamma0 {observation['median_db']:.3f} dB."]
    if measurements["kind"] == "backscatter_change":
        change = measurements["change"]
        lines += ["", "## Measured difference", f"Median later-minus-earlier backscatter: {change['median_db']:.3f} dB over {change['valid_pixels']:,} common valid pixels.",
                  f"{change['threshold_exceedance_pct']:.2f}% exceed an absolute {change['threshold_db']:.2f} dB descriptive threshold. This is not a significance test or an event classification."]
    else:
        lines += ["", "A single acquisition describes backscatter at one time. It cannot answer what changed."]
    lines += ["", "## Method", f"{METHOD_VERSION}. Mask invalid/nonpositive samples; transform gamma0 power with 10 log10. Compute statistics at native pixel spacing. Reproject only the presentation preview using nearest-neighbor sampling.", "", "## Limitations", *[f"- {limitation}" for limitation in limitations], "", "See provenance.json and the georeferenced float COG artifacts for inputs, checksums, parameters, and measurements."]
    return "\n".join(lines) + "\n"
