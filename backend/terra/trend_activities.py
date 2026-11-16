"""Activities for the trend stack: plan dates, acquire originals, build one quantized dataset, fit tiles, publish.

The dataset is shared by many tile agents. One analysis directory holds:

- `stack.u8`: an N x H x W uint8 memmap of the 8-bit backscatter codes, one row per date, written one date at
  a time so only one float32 date array exists in memory while the stack is built.
- `sample.npz`: up to 20000 deterministic pixel positions with their float dB values for every date, used to
  measure what the 8-bit rounding does to a fitted slope.
- `meta.json`: dates, t_years, grid, dropped dates with reasons, byte counts and the quantization bounds.
- `tile_<index>.npz` and the published layers.

Every measurement here comes from a real GCOV product read through `science.inspect_gcov` and `science._read`.
The trend itself is a straight-line fit in dB against time; these activities do not decide whether a slope is a
physical change.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.format import open_memmap
from PIL import Image
import rasterio
from rasterio.enums import Resampling
from rasterio.shutil import copy as raster_copy
from rasterio.transform import Affine, array_bounds, from_bounds
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from temporalio import activity

from .config import get_settings
from .qubo import select_dates
from .science import NODATA, _colors, _pixel_km2, _read, inspect_gcov
from .trends import (
    dequantize,
    fit_trend,
    quantization_effect,
    quantization_error_db,
    quantize,
    summarize,
    tune_delta,
)

METHOD = "terra-trend-1.0.0"
MAX_TILE_PIXELS = 150_000  # keeps one agent's float64 working set to roughly 100 MB
MAX_TILES = 16
SAMPLE_PIXELS = 20_000
MIN_DATES = 4
IDENTIFIER = re.compile(r"^trend-[0-9a-f]{12}$")

LIMITATIONS = [
    "The trend is a straight-line fit of backscatter in dB against time over the dates used. A step, a change point or a "
    "non-linear trajectory is not modelled.",
    "Seasonal cycles (wet and dry seasons, vegetation growth and harvest) can look like trends when the stack spans less "
    "than a full year, and one anomalous date can dominate a short stack.",
    "Only acquisitions with the same viewing geometry, product maturity, processing version and frequency are compared. "
    "Other passes over the same ground are not mixed into this stack.",
    "Per-pixel trends are speckle limited. Each slope carries a standard error, and neighbouring radar pixels are "
    "correlated, so the count of significant pixels is not a count of independent evidence.",
    "The dates were chosen by a QUBO (quadratic unconstrained binary optimisation) and solved here with classical "
    "simulated annealing. The form is quantum ready; no quantum hardware was used.",
    "Before fitting, each date is averaged over 3 x 3 pixels in linear power (about nine times more looks), so the trend "
    "map is slightly coarser than the native pixels.",
    "At p < 0.05 about 5 % of pixels would pass by chance alone (somewhat more with the robust fit), so compare the "
    "significant share with that before reading meaning into it.",
    "The shared dataset rounds each date to 8-bit codes of 0.2 dB, which limits how small a slope can be resolved. The "
    "measured rounding effect on the fitted slopes is reported with the result.",
]


def analysis_directory(identifier: str) -> Path:
    """The one directory of this analysis; the identifier is validated so it cannot leave data_dir."""
    if not IDENTIFIER.fullmatch(str(identifier)):
        raise ValueError("Invalid trend analysis identifier.")
    root = (get_settings().data_dir / "trends").resolve()
    path = root / str(identifier)
    path.mkdir(parents=True, exist_ok=True)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Invalid trend analysis directory.")
    return path


def _safe(value: Any) -> Any:
    """JSON-safe copy: non-finite floats become None so history and result.json stay valid JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return _safe(value.tolist())
    if isinstance(value, np.generic):
        return _safe(value.item())
    return value


def _moment(value: str) -> datetime:
    """ISO date or timestamp, with a trailing Z read as UTC. Naive values are compared as they are."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _quality(observation: dict) -> float:
    """Each date's reward in the QUBO, from how much of the frame the product covers.

    ASF reports frameCoverage as a word ("Full" or "Partial"); a numeric percentage is also accepted.
    """
    word = str(observation.get("frame_coverage") or "").strip().lower()
    if word in ("full", "partial"):
        return 1.0 if word == "full" else 0.6
    try:
        number = float(observation.get("frame_coverage"))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(number):
        return 1.0
    return min(1.0, max(0.0, number / 100.0))


def _offsets(transform, ref_transform) -> tuple[int, int]:
    """Whole-pixel (row, column) shift of a date's grid origin relative to the reference grid origin.

    A GDAL/Affine transform is (pixel width, 0, x origin, 0, pixel height, y origin), so the origin is at
    indices 2 and 5 while index 4 is the (negative) pixel height.
    """
    ref_dx = float(ref_transform[0])
    ref_dy = float(ref_transform[4])
    if ref_dx == 0 or ref_dy == 0:
        raise ValueError("Grids must have non-zero pixel spacing.")
    column = int(round((float(transform[2]) - float(ref_transform[2])) / ref_dx))
    row = int(round((float(transform[5]) - float(ref_transform[5])) / ref_dy))
    return row, column


def align(values: np.ndarray, transform, ref_transform, ref_shape) -> np.ndarray:
    """Place one date's array on the reference grid by integer pixel offset, cropping and padding as needed.

    Alignment is a shift only: the caller has already established that the dates share a projection and pixel
    spacing. Ground that the date does not cover is filled with NaN for float data and with code 0 for uint8
    data, so an uncovered pixel is never mistaken for a measurement. The returned array always has `ref_shape`.
    """
    array = np.asarray(values)
    height, width = int(ref_shape[0]), int(ref_shape[1])
    fill = np.nan if np.issubdtype(array.dtype, np.floating) else 0
    if not (np.issubdtype(array.dtype, np.floating) or np.issubdtype(array.dtype, np.unsignedinteger)):
        raise ValueError("Only float and unsigned integer arrays can be aligned onto the date grid.")
    padded = np.full((height, width), fill, dtype=array.dtype)
    row, column = _offsets(transform, ref_transform)
    source_row = max(0, -row)
    source_column = max(0, -column)
    target_row = max(0, row)
    target_column = max(0, column)
    rows = min(array.shape[0] - source_row, height - target_row)
    columns = min(array.shape[1] - source_column, width - target_column)
    if rows > 0 and columns > 0:
        padded[target_row:target_row + rows, target_column:target_column + columns] = \
            array[source_row:source_row + rows, source_column:source_column + columns]
    return padded


def _edges(size: int, blocks: int) -> list[int]:
    """Block boundaries that split `size` into `blocks` nearly equal pieces; blocks <= size keeps them distinct."""
    return [int(round(index * size / blocks)) for index in range(blocks + 1)]


def plan_tiles(shape, max_pixels: int = MAX_TILE_PIXELS) -> list[dict]:
    """Split the grid into at least one and at most 16 tiles of at most `max_pixels` pixels each.

    The fewest tiles that respect the budget are used, preferring a square split, so a large area goes to
    several agents without making any single one hold an oversized array. When 16 tiles still exceed the
    budget the square 16-tile split is returned: the limit on agents wins over the per-tile budget.
    """
    height, width = int(shape[0]), int(shape[1])
    if height < 1 or width < 1:
        raise ValueError("The grid must have at least one row and one column.")
    if max_pixels < 1:
        raise ValueError("max_pixels must be at least one.")
    needed = min(MAX_TILES, max(1, math.ceil(height * width / max_pixels)))
    candidates: list[tuple[tuple[int, int, int], int, int, list[int], list[int]]] = []
    for rows in range(1, min(height, MAX_TILES) + 1):
        for columns in range(1, min(width, MAX_TILES) + 1):
            count = rows * columns
            if count < needed or count > MAX_TILES:
                continue
            row_edges, column_edges = _edges(height, rows), _edges(width, columns)
            largest = max((row_edges[i + 1] - row_edges[i]) * (column_edges[j + 1] - column_edges[j])
                          for i in range(rows) for j in range(columns))
            candidates.append(((count, abs(rows - columns), largest), rows, columns, row_edges, column_edges))
    if not candidates:
        raise ValueError("The grid cannot be split into tiles.")
    within = [candidate for candidate in candidates if candidate[0][2] <= max_pixels]
    chosen = min(within or candidates)
    _, rows, columns, row_edges, column_edges = chosen
    tiles = []
    index = 0
    for i in range(rows):
        for j in range(columns):
            tiles.append({"index": index, "rows": [row_edges[i], row_edges[i + 1]],
                          "cols": [column_edges[j], column_edges[j + 1]]})
            index += 1
    return tiles


async def plan_dates(payload: dict) -> dict:
    """Search the catalog, keep one viewing geometry, and pick k spread dates with the QUBO."""
    from .catalog import search_observations, validate_bbox

    bbox = validate_bbox([float(value) for value in payload.get("bbox") or []])
    max_dates = int(payload.get("max_dates") or 10)
    try:
        found = await search_observations(bbox, 100)
    except Exception as exc:
        return {"ok": False, "reason": f"The catalog search failed ({type(exc).__name__}); retry this analysis."}

    groups: dict[tuple, list[dict]] = {}
    for observation in found:
        if observation.get("product") != "GCOV":
            continue
        key = (str(observation.get("track")), str(observation.get("direction") or "").upper(),
               str(observation.get("frame")), str(observation.get("maturity") or "").upper(),
               str(observation.get("product_version")), str(observation.get("frequency")))
        groups.setdefault(key, []).append(observation)
    if not groups:
        return {"ok": False, "reason": "No NISAR GCOV acquisitions cover this area yet."}
    ordered = sorted(groups.values(), key=lambda group: (len(group), max(str(o.get("date") or "") for o in group)),
                     reverse=True)
    group = ordered[0]
    if len(group) < MIN_DATES:
        return {"ok": False, "reason": f"Only {len(group)} acquisition(s) share one viewing geometry; a trend needs "
                                       f"at least {MIN_DATES}."}
    group = sorted(group, key=lambda observation: str(observation.get("date") or ""))
    k = min(max_dates, len(group))
    chosen = select_dates([str(observation.get("date")) for observation in group], [_quality(o) for o in group], k)
    observations = [group[index] for index in chosen["chosen"]]
    first = group[0]
    return _safe({
        "ok": True,
        "observations": observations,
        "group": {"track": first.get("track"), "direction": first.get("direction"), "frame": first.get("frame"),
                  "maturity": first.get("maturity"), "product_version": first.get("product_version"),
                  "candidates": len(group)},
        "qubo": chosen,
    })


async def acquire_date(payload: dict) -> dict:
    """Find a previously read subset of this date or read the original product. One bad date is reported, not raised."""
    from .acquire import AcquisitionError, acquire_subset, earthdata_token, find_subset

    observation = payload.get("observation") or {}
    bbox = payload.get("bbox") or []
    date = str(observation.get("date") or "")
    try:
        path = await asyncio.to_thread(find_subset, str(observation.get("id") or ""), bbox)
    except Exception:
        path = None
    if path is not None:
        return {"ok": True, "path": str(path), "date": date}
    if not earthdata_token():
        return {"ok": False, "reason": "EARTHDATA_TOKEN is not configured, so the original GCOV products cannot be read."}
    try:
        path = await asyncio.to_thread(acquire_subset, observation, bbox, "A")
    except AcquisitionError as exc:
        return {"ok": False, "reason": str(exc)}
    except Exception as exc:
        return {"ok": False, "reason": f"Reading the original product failed ({type(exc).__name__})."}
    return {"ok": True, "path": str(path), "date": date}


def build_quantized_stack(payload: dict) -> dict:
    """Write the shared uint8 dataset for every usable date, one date in memory at a time.

    The first readable date defines the reference grid (transform, shape, projection, pixel spacing). A later date
    is dropped with a reason if its product cannot be read, its projection differs, or its pixel spacing differs.
    Dates that keep the same grid are shifted onto the reference grid by whole pixels and quantized in place, so
    the peak memory is one float32 date array plus its uint8 codes. A deterministic sample of pixel positions
    stores the float values for the quantization measurement.
    """
    directory = analysis_directory(payload["id"])
    bbox = payload.get("bbox")
    entries = sorted(payload.get("inputs") or [], key=lambda item: str(item.get("date") or ""))
    if not entries:
        return {"ok": False, "reason": "No acquisition was acquired."}

    stack_path = directory / "stack.u8"
    partial = directory / "stack.partial.u8"
    dropped: list[dict] = []
    dates: list[str] = []
    samples: np.ndarray | None = None
    rows = columns = None
    stack = None
    reference: dict = {}
    count = 0

    for entry in entries:
        date = str(entry.get("date") or "")
        path = entry.get("path")
        if not date or not path:
            dropped.append({"date": date, "reason": "The acquisition has no date or no local file."})
            continue
        try:
            _moment(date)
        except ValueError:
            dropped.append({"date": date, "reason": "The acquisition date is not a readable timestamp."})
            continue
        try:
            meta = inspect_gcov(path, "A")
        except Exception as exc:
            dropped.append({"date": date, "reason": f"Not a readable NISAR GCOV product ({type(exc).__name__})."})
            continue
        try:
            values_db, _inside, transform, _info = _read(Path(path), meta, bbox)
        except Exception as exc:
            dropped.append({"date": date, "reason": f"No quality-masked backscatter could be read ({type(exc).__name__})."})
            continue

        if not reference:
            reference = {"meta": meta, "transform": transform, "shape": values_db.shape,
                         "crs": meta["crs"], "spacing": meta["pixel_spacing"]}
            height, width = values_db.shape
            stack = open_memmap(partial, mode="w+", dtype=np.uint8, shape=(len(entries), height, width))
            capacity = min(SAMPLE_PIXELS, height * width)
            picker = np.random.default_rng(0)
            rows = picker.integers(0, height, capacity)
            columns = picker.integers(0, width, capacity)
            samples = np.full((len(entries), capacity), np.nan, dtype=np.float32)
        elif meta["crs"] != reference["crs"]:
            dropped.append({"date": date, "reason": "A different map projection from the reference date."})
            continue
        elif not np.allclose(meta["pixel_spacing"], reference["spacing"], rtol=1e-3, atol=1e-6):
            dropped.append({"date": date, "reason": "A different pixel spacing from the reference date."})
            continue

        row, column = _offsets(transform, reference["transform"])
        stack[count] = align(quantize(values_db), transform, reference["transform"], reference["shape"])
        if samples is not None and rows is not None and columns is not None:
            source_rows = rows - row
            source_columns = columns - column
            inside = ((source_rows >= 0) & (source_rows < values_db.shape[0])
                      & (source_columns >= 0) & (source_columns < values_db.shape[1]))
            column_values = np.full(rows.size, np.nan, dtype=np.float32)
            column_values[inside] = values_db[source_rows[inside], source_columns[inside]]
            samples[count] = column_values
        dates.append(date)
        count += 1

    if stack is not None:
        stack.flush()
        del stack
    if count < MIN_DATES:
        for path in (partial, stack_path):
            if path.exists():
                path.unlink()
        reason = (f"Only {count} usable date(s); a trend needs at least {MIN_DATES}. "
                  + (str(dropped[0]["reason"]) if dropped else "")).strip()
        return {"ok": False, "reason": reason, "dropped": dropped}

    height, width = int(reference["shape"][0]), int(reference["shape"][1])
    if count != len(entries):
        final = open_memmap(stack_path, mode="w+", dtype=np.uint8, shape=(count, height, width))
        source = np.load(partial, mmap_mode="r")
        for index in range(count):
            final[index] = source[index]
        final.flush()
        del final
        del source
        partial.unlink()
    else:
        partial.replace(stack_path)

    moments = [_moment(date) for date in dates]
    start = moments[0]
    t_years = [round((moment - start).total_seconds() / (365.25 * 86400.0), 6) for moment in moments]
    bytes_uint8 = int(count * height * width)
    document = {
        "id": payload["id"], "bbox": [float(value) for value in bbox] if bbox else None,
        "dates": dates, "t_years": t_years,
        "transform": [float(value) for value in list(reference["transform"])[:6]],
        "crs": reference["crs"], "shape": [height, width],
        "pixel_spacing": [float(value) for value in reference["spacing"]],
        "pixel_km2": _pixel_km2(reference["meta"]),
        "dropped": dropped,
        "bytes": {"uint8": bytes_uint8, "float32": bytes_uint8 * 4},
        "quantization": quantization_error_db(),
    }
    (directory / "meta.json").write_text(json.dumps(_safe(document), indent=2, allow_nan=False))
    if samples is not None:
        np.savez(directory / "sample.npz", values=samples[:count],
                 rows=rows if rows is not None else np.empty(0, dtype=np.int64),
                 columns=columns if columns is not None else np.empty(0, dtype=np.int64),
                 dates=np.array(dates))
    tiles = plan_tiles((height, width))
    return _safe({"ok": True, "shape": [height, width], "dates": dates, "t_years": t_years, "tiles": tiles,
                  "dropped": dropped, "bytes": document["bytes"], "pixel_km2": document["pixel_km2"]})


LOOKS = 3  # spatial averaging window (pixels) applied before the per-pixel fit


def multilook_block(stack: np.ndarray, rows: list[int], columns: list[int], looks: int = LOOKS) -> np.ndarray:
    """One tile of the shared dataset, averaged over a looks x looks window in linear power, back in dB.

    Speckle makes single radar pixels noisy; averaging 3 x 3 neighbours gives about nine times more looks per pixel,
    so a real trend separates from speckle with only a handful of dates. The tile is read with a halo so the average
    is the same on both sides of a tile edge. Missing values (code 0) are left out of each average; a window with no
    valid value stays missing.
    """
    from scipy.ndimage import uniform_filter
    halo = looks // 2
    r0, r1 = max(0, rows[0] - halo), min(stack.shape[1], rows[1] + halo)
    c0, c1 = max(0, columns[0] - halo), min(stack.shape[2], columns[1] + halo)
    db = dequantize(np.asarray(stack[:, r0:r1, c0:c1])).astype(np.float64)
    valid = np.isfinite(db)
    power = np.where(valid, 10.0 ** (db / 10.0), 0.0)
    size = (1, looks, looks)
    total = uniform_filter(power, size=size, mode="constant")
    count = uniform_filter(valid.astype(np.float64), size=size, mode="constant")
    with np.errstate(divide="ignore", invalid="ignore"):
        averaged = np.where(count > 1e-9, 10.0 * np.log10(total / count), np.nan)
    averaged[~np.isfinite(averaged)] = np.nan
    return averaged[:, rows[0] - r0: rows[0] - r0 + (rows[1] - rows[0]), columns[0] - c0: columns[0] - c0 + (columns[1] - columns[0])]


def fit_tile(payload: dict) -> dict:
    """Fit the robust trend for one tile of the shared dataset and save its arrays for assembly.

    The Huber delta is tuned on this tile by leave-one-date-out cross validation, then used for the reported
    fit. Pixels with fewer than three valid dates stay NaN, so a tile that no date covers contributes nothing.
    """
    directory = analysis_directory(payload["id"])
    tile = payload["tile"]
    meta = json.loads((directory / "meta.json").read_text())
    t_years = np.asarray(meta["t_years"], dtype=np.float64)
    rows, columns = [int(value) for value in tile["rows"]], [int(value) for value in tile["cols"]]
    stack = np.load(directory / "stack.u8", mmap_mode="r")
    height, width = rows[1] - rows[0], columns[1] - columns[0]
    y = multilook_block(stack, rows, columns).reshape(stack.shape[0], -1)
    usable = int((np.isfinite(y).sum(axis=0) >= 4).sum())

    if usable == 0:
        empty = np.full((height, width), np.nan, dtype=np.float32)
        np.savez(directory / f"tile_{int(tile['index'])}.npz", slope=empty, se=empty, p=empty, r2=empty, n_used=empty)
        return _safe({"index": int(tile["index"]), "delta": None, "cv_mae_db": {}, "pixels_fit": 0,
                      "median_slope": None, "significant_share": 0.0})

    tuned = tune_delta(t_years, y)
    fit = fit_trend(t_years, y, tuned["delta"])
    slope = fit["slope_db_per_year"].reshape(height, width)
    se = fit["slope_se"].reshape(height, width)
    p_value = fit["p_value"].reshape(height, width)
    r2 = fit["r2"].reshape(height, width)
    n_used = fit["n_used"].reshape(height, width)
    np.savez(directory / f"tile_{int(tile['index'])}.npz",
             slope=slope.astype(np.float32), se=se.astype(np.float32), p=p_value.astype(np.float32),
             r2=r2.astype(np.float32), n_used=n_used.astype(np.float32))
    finite = np.isfinite(slope)
    significant = finite & np.isfinite(p_value) & (p_value < 0.05)
    return _safe({"index": int(tile["index"]), "delta": float(tuned["delta"]), "cv_mae_db": tuned["cv_mae_db"],
                  "pixels_fit": int(finite.sum()),
                  "median_slope": float(np.median(slope[finite])) if finite.any() else None,
                  "significant_share": float(significant.sum() / finite.sum()) if finite.any() else 0.0})


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _export_trend(values: np.ndarray, transform, crs: str, out: Path, name: str, title: str) -> dict:
    """Write one trend layer as a float COG plus a small geographic PNG preview, like the change layers.

    The COG keeps the measured slope in dB/year at native pixel spacing; the preview reprojects with nearest
    sampling and colours the fixed -3..+3 dB/year diverging range, so the legend is comparable between analyses.
    """
    height, width = values.shape
    temporary = out / f".{name}.tif"
    path = out / f"{name}.tif"
    with rasterio.open(temporary, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32", crs=crs,
                       transform=transform, nodata=NODATA, compress="deflate") as dataset:
        dataset.write(np.where(np.isfinite(values), values, NODATA).astype(np.float32), 1)
        dataset.update_tags(AREA_OR_POINT="Area", method=METHOD, units="dB/yr",
                            interpretation="linear trend of gamma0 backscatter")
    raster_copy(temporary, path, driver="COG", compress="DEFLATE", blocksize=256, overview_resampling="NEAREST")
    temporary.unlink()
    source_bounds = array_bounds(height, width, transform)
    target_transform, target_width, target_height = calculate_default_transform(crs, "EPSG:4326", width, height,
                                                                                *source_bounds)
    factor = max(target_width, target_height) / 1600
    if factor > 1:
        target_width = max(1, math.ceil(target_width / factor))
        target_height = max(1, math.ceil(target_height / factor))
        target_transform = from_bounds(*transform_bounds(crs, "EPSG:4326", *source_bounds, densify_pts=21),
                                       target_width, target_height)
    preview = np.full((target_height, target_width), np.nan, dtype=np.float32)
    reproject(values, preview, src_transform=transform, src_crs=crs, src_nodata=np.nan,
              dst_transform=target_transform, dst_crs="EPSG:4326", dst_nodata=np.nan, resampling=Resampling.nearest)
    low, high = -3.0, 3.0
    preview_path = out / f"{name}.png"
    Image.fromarray(_colors(preview, low, high, True)).save(preview_path)
    return {
        "id": name, "title": title, "path": preview_path.name, "cog_path": path.name,
        "bounds": list(array_bounds(target_height, target_width, target_transform)),
        "units": "dB/yr", "min": low, "max": high, "colormap": "blue-neutral-orange",
        "preview_resampling": "nearest", "preview_shape": [target_height, target_width],
        "native_shape": [int(height), int(width)], "sha256": _sha256(path), "preview_sha256": _sha256(preview_path),
    }


def publish_trends(payload: dict) -> dict:
    """Assemble the tile fits, export the two trend layers, measure the 8-bit effect and write result.json.

    `summary` describes the full area, not a tile, and counts a pixel as significant only when p < 0.05. The
    significant layer keeps the slope where that holds and is blank elsewhere, so the two layers can be compared
    directly. Missing tiles stay NaN in both.
    """
    directory = analysis_directory(payload["id"])
    meta = json.loads((directory / "meta.json").read_text())
    height, width = int(meta["shape"][0]), int(meta["shape"][1])
    transform = Affine(*meta["transform"])
    crs = meta["crs"]
    agents = sorted(list(payload.get("tiles") or []), key=lambda agent: int(agent["index"]))
    source_tiles = (payload.get("stack") or {}).get("tiles") or plan_tiles((height, width))
    bounds_by_index = {int(tile["index"]): tile for tile in source_tiles}

    slope = np.full((height, width), np.nan, dtype=np.float32)
    standard_error = np.full((height, width), np.nan, dtype=np.float32)
    p_value = np.full((height, width), np.nan, dtype=np.float32)
    for agent in agents:
        index = int(agent["index"])
        tile = bounds_by_index.get(index)
        if tile is None:
            continue
        rows, columns = [int(value) for value in tile["rows"]], [int(value) for value in tile["cols"]]
        with np.load(directory / f"tile_{index}.npz") as stored:
            slope[rows[0]:rows[1], columns[0]:columns[1]] = stored["slope"]
            standard_error[rows[0]:rows[1], columns[0]:columns[1]] = stored["se"]
            p_value[rows[0]:rows[1], columns[0]:columns[1]] = stored["p"]

    significant = np.isfinite(p_value) & (p_value < 0.05)
    layers = [
        _export_trend(slope, transform, crs, directory, "trend", "Backscatter trend, dB per year"),
        _export_trend(np.where(significant, slope, np.nan), transform, crs, directory, "trend-significant",
                      "Backscatter trend where p < 0.05"),
    ]

    deltas = [float(agent["delta"]) for agent in agents
              if isinstance(agent.get("delta"), (int, float)) and math.isfinite(float(agent["delta"]))]
    tuned_delta = float(np.median(deltas)) if deltas else math.inf
    effect = None
    sample_path = directory / "sample.npz"
    if sample_path.is_file():
        with np.load(sample_path) as sample:
            sampled = np.asarray(sample["values"], dtype=np.float64)
        if sampled.ndim == 2 and sampled.shape[0] == len(meta["t_years"]) and sampled.shape[1] > 0:
            effect = quantization_effect(np.asarray(meta["t_years"], dtype=np.float64), sampled, tuned_delta)

    summary = summarize({"slope_db_per_year": slope.ravel(), "p_value": p_value.ravel()}, meta.get("pixel_km2"))
    plan = payload.get("plan") or {}
    qubo = plan.get("qubo") or {}
    group = plan.get("group") or {}
    quantized = meta.get("quantization") or quantization_error_db()
    result = {
        "id": payload["id"],
        "bbox": meta.get("bbox"),
        "dates": meta["dates"],
        "t_span_years": float(meta["t_years"][-1]) if meta.get("t_years") else 0.0,
        "group": group,
        "qubo": {
            "energy": qubo.get("energy"),
            "optimum_energy": qubo.get("brute_force_optimum_energy"),
            "solver": qubo.get("solver"),
            "chosen": len(qubo.get("chosen") or []),
            "candidates": group.get("candidates"),
        },
        "quantized_dataset": {
            "bytes_uint8": int(meta["bytes"]["uint8"]),
            "bytes_float32": int(meta["bytes"]["float32"]),
            "step_db": quantized["step_db"],
            "max_abs_error_db": quantized["max_abs_db"],
            "rms_error_db": quantized["rms_db"],
            "effect": effect,
        },
        "agents": agents,
        "summary": summary,
        "layers": layers,
        "limitations": LIMITATIONS,
    }
    safe = _safe(result)
    temporary = directory / "result.json.tmp"
    temporary.write_text(json.dumps(safe, indent=2, allow_nan=False))
    temporary.replace(directory / "result.json")
    return _safe({"ok": True, **safe})


trend_plan = activity.defn(name="trend_plan")(plan_dates)
trend_acquire = activity.defn(name="trend_acquire")(acquire_date)
# The numeric activities are synchronous; run them on a thread so the worker's event loop (and every other workflow)
# keeps moving while a tile is fitted.
@activity.defn(name="trend_build_stack")
async def trend_build_stack(payload: dict) -> dict:
    return await asyncio.to_thread(build_quantized_stack, payload)


@activity.defn(name="trend_fit_tile")
async def trend_fit_tile(payload: dict) -> dict:
    return await asyncio.to_thread(fit_tile, payload)


@activity.defn(name="trend_publish")
async def trend_publish(payload: dict) -> dict:
    return await asyncio.to_thread(publish_trends, payload)

ACTIVITIES = [trend_plan, trend_acquire, trend_build_stack, trend_fit_tile, trend_publish]
