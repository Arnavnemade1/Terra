"""Independent context for a finished analysis, read through the server connector.

JRC Global Surface Water (Pekel et al. 2016, Copernicus programme / JRC, 1984-2021) records how often each 30 m
pixel was open water. Splitting the area that newly looks like water by that history separates seasonal or
permanent water from water that appears where none was ever mapped. It never changes the radar measurements.
"""
from __future__ import annotations

import math
import warnings
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine, array_bounds
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds as window_from_bounds

from .connector import ConnectorError, RangeFile

JRC_OCCURRENCE = "https://global-surface-water.storage.googleapis.com/downloads2021/occurrence/occurrence_{lon}_{lat}v1_4_2021.tif"
NO_DATA = 255
PERMANENT_MIN_PCT = 90
JRC_SOURCE = "JRC Global Surface Water v1.4 occurrence, 1984-2021, 30 m"
JRC_URL = "https://global-surface-water.appspot.com"


def _tile_urls(bounds: list[float]) -> list[str]:
    """Occurrence tiles are 10° × 10° and named by their west and north edges (80W_0N covers 80°W to 70°W, 10°S to 0°)."""
    west, south, east, north = bounds
    urls = []
    for lat in range(math.ceil(north / 10) * 10, math.floor(south / 10) * 10, -10):
        for lon in range(math.floor(west / 10) * 10, math.ceil(east / 10) * 10, 10):
            urls.append(JRC_OCCURRENCE.format(lon=f"{abs(lon)}{'E' if lon >= 0 else 'W'}", lat=f"{abs(lat)}{'N' if lat >= 0 else 'S'}"))
    return urls


def _read_tile(url: str, lonlat: list[float], shape: tuple[int, int], transform: Affine, crs: str) -> np.ndarray | None:
    """One tile's occurrence resampled (nearest) onto the analysis grid; None when the tile does not exist."""
    try:
        shared = RangeFile("jrc", url, block=128 * 1024, workers=16, timeout=20)
    except ConnectorError as exc:
        if exc.status_code in (403, 404):
            return None
        raise

    def opener(path, mode="rb"):
        if path != url:
            raise FileNotFoundError(path)
        return shared.view()

    part = np.full(shape, NO_DATA, dtype=np.uint8)
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(url, opener=opener) as src:
        window = window_from_bounds(*lonlat, transform=src.transform).round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, src.width, src.height))
        if window.width < 1 or window.height < 1:
            return part
        bh, bw = src.block_shapes[0]
        ranges = []
        for row in range(int(window.row_off) // bh, int(window.row_off + window.height - 1) // bh + 1):
            for col in range(int(window.col_off) // bw, int(window.col_off + window.width - 1) // bw + 1):
                offset = src.get_tag_item(f"BLOCK_OFFSET_{col}_{row}", "TIFF", bidx=1)
                size = src.get_tag_item(f"BLOCK_SIZE_{col}_{row}", "TIFF", bidx=1)
                if offset and size:
                    ranges.append((int(offset), int(size)))
        shared.prefetch(ranges)
        data = src.read(1, window=window)
        warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)
        reproject(data, part, src_transform=src.window_transform(window), src_crs=src.crs, dst_transform=transform, dst_crs=crs,
                  src_nodata=NO_DATA, dst_nodata=NO_DATA, resampling=Resampling.nearest)
    return part


def occurrence_on_grid(shape: tuple[int, int], transform: Affine, crs: str) -> np.ndarray:
    """JRC occurrence (0-100 % of observations that were water; 255 = no data) on the given grid."""
    lonlat = list(transform_bounds(crs, "EPSG:4326", *array_bounds(*shape, transform), densify_pts=21))
    out = np.full(shape, NO_DATA, dtype=np.uint8)
    for url in _tile_urls(lonlat):
        part = _read_tile(url, lonlat, shape, transform, crs)
        if part is not None:
            out[part != NO_DATA] = part[part != NO_DATA]
    return out


def split_by_occurrence(mask: np.ndarray, occurrence: np.ndarray, km2_per_pixel: float | None) -> dict:
    """Count the masked pixels that were permanent (>= 90 %), seasonal (1-89 %), never water (0 %) or unmapped."""
    seen = occurrence[mask]
    classes = {"permanent": (seen >= PERMANENT_MIN_PCT) & (seen <= 100), "seasonal": (seen > 0) & (seen < PERMANENT_MIN_PCT),
               "never": seen == 0, "unmapped": seen > 100}
    mapped = int(sum(c.sum() for k, c in classes.items() if k != "unmapped"))
    out: dict = {"newly_water_like_pixels": int(seen.size), "mapped_pixels": mapped}
    for key, flag in classes.items():
        count = int(flag.sum())
        out[key] = {"pixels": count, "km2": round(count * km2_per_pixel, 3) if km2_per_pixel else None,
                    **({"pct_of_mapped": round(count / mapped * 100, 1) if mapped else None} if key != "unmapped" else {})}
    return out


def water_context(mask_path: Path) -> dict:
    """Split the newly water-like area (a 0/1 raster written by the analysis) by JRC surface-water history."""
    with rasterio.open(mask_path) as ds:
        mask = ds.read(1) > 0
        transform, crs = ds.transform, ds.crs
    projected = crs.is_projected and crs.linear_units == "metre"
    if not mask.any():
        return {"status": "none", "note": "No pixels became water-like between the two dates, so there is nothing to split."}
    split = split_by_occurrence(mask, occurrence_on_grid(mask.shape, transform, crs.to_string()), abs(transform.a * transform.e) / 1e6 if projected else None)
    return {"status": "available", **split, "permanent_min_pct": PERMANENT_MIN_PCT, "source": JRC_SOURCE, "source_url": JRC_URL,
            "note": ("Occurrence is the share of valid observations from 1984 to 2021 in which a 30 m pixel was open water. 'Never' means never mapped as water, "
                     "so water-like returns there are more likely a new or unusual wetting than seasonal water. The history is context only; "
                     "low radar return is still a proxy for water.")}


def water_report(context: dict | None) -> str:
    """Report section for the split; empty when there was no newly water-like area to split."""
    if not context or context.get("status") == "none":
        return ""
    lines = ["", "## Water history of the newly water-like area (JRC Global Surface Water)", ""]
    if context.get("status") != "available":
        return "\n".join(lines + [context.get("note", "Unavailable."), ""])
    for key, label in (("permanent", f"permanent water (at least {context['permanent_min_pct']}% of observations)"),
                       ("seasonal", "seasonal water (some observations)"), ("never", "never mapped as water"), ("unmapped", "outside JRC coverage")):
        item = context[key]
        area = f"{item['km2']:.2f} km², " if item.get("km2") is not None else ""
        share = f", {item['pct_of_mapped']:.1f}% of mapped" if item.get("pct_of_mapped") is not None else ""
        lines.append(f"- {label.capitalize()}: {area}{item['pixels']:,} pixels{share}")
    return "\n".join([*lines, "", context["note"], "", f"Source: {context['source']} ({context['source_url']})", ""])
