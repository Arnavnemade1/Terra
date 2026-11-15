"""Acquire an area-of-interest subset of an original NISAR GCOV granule.

Reads only the needed HDF5 chunks from the authoritative ASF/Earthdata Cloud copy
using HTTP byte ranges (Earthdata bearer token), then writes a local HDF5 that keeps
the original group layout, identification metadata, quality masks and covariance
values unchanged — only cropped. A provenance sidecar records exactly what was read.
"""
from __future__ import annotations

from contextlib import contextmanager
import gc
import hashlib
import json
import math
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timezone
from pathlib import Path

import h5py
import httpx
import numpy as np
from rasterio.warp import transform_bounds

from .config import get_settings
from .connector import RangeFile, request_sync, validate_url, ConnectorError

BASE = "/science/LSAR"
DIAGONALS = ("HHHH", "HVHV", "VVVV", "VHVH")
MASKS = ("mask", "inputDataExceptionMask", "layoverShadowMask", "numberOfLooks", "rtcGammaToSigmaFactor")
MAX_PIXELS = 2048 * 2048
ASF_HOSTS = ("nisar.asf.earthdatacloud.nasa.gov", "datapool.asf.alaska.edu", "sentinel1.asf.alaska.edu")


class AcquisitionError(RuntimeError):
    pass


MAX_LOOKS = 4
HEAD_BYTES = 6 << 20
TAIL_BYTES = 16 << 20
_hdf5_read_lock = threading.Lock()


@contextmanager
def _hdf5_reader():
    # h5py holds its global HDF5 lock while RangeFile calls its HTTP threads.
    # Automatic cyclic GC on a service thread can then wait for that same lock.
    # https://docs.h5py.org/en/stable/high/file.html#python-file-like-objects
    # Serialize these regions so overlapping acquisitions cannot restore GC
    # while another HDF5 file is still open. HTTP chunk prefetch stays parallel.
    with _hdf5_read_lock:
        collecting = gc.isenabled()
        gc.disable()
        try:
            yield
        finally:
            if collecting:
                gc.enable()


def _prefetch_window(handle: RangeFile, datasets: list, r0: int, r1: int, c0: int, c1: int) -> None:
    """Fetch, in parallel, the stored chunks of each dataset that intersect the window."""
    ranges = []
    for ds in datasets:
        if ds.chunks is None or ds.ndim != 2:
            continue
        ch, cw = ds.chunks
        for r in range((r0 // ch) * ch, r1, ch):
            for c in range((c0 // cw) * cw, c1, cw):
                try:
                    info = ds.id.get_chunk_info_by_coord((r, c))
                except Exception:
                    continue
                if info.byte_offset is not None and info.size:
                    ranges.append((info.byte_offset, info.size))
    handle.prefetch(ranges)


def _looks(values: np.ndarray, k: int, name: str) -> np.ndarray:
    """Average a 2-D field in k×k blocks, conservatively for quality layers.

    Covariance power is averaged in linear units only where the whole block is valid;
    any invalid sample masks the block. Quality masks keep the strictest state.
    """
    if k == 1:
        return values
    h, w = values.shape[0] // k, values.shape[1] // k
    blocks = values[: h * k, : w * k].reshape(h, k, w, k).swapaxes(1, 2).reshape(h, w, k * k)
    if name in DIAGONALS:
        b = blocks.astype(np.float64)
        ok = np.isfinite(b) & (b > 0)
        out = np.where(ok.all(axis=2), b.mean(axis=2, where=ok) if ok.any() else np.nan, np.nan)
        return out.astype(np.float32)
    if name == "mask":
        ok = (blocks > 0) & (blocks < 255)
        return np.where(ok.all(axis=2), blocks.max(axis=2), 255).astype(values.dtype)
    if name == "inputDataExceptionMask":
        b = blocks.astype(np.uint32)
        low = np.bitwise_or.reduce(b & 0xFF, axis=2)
        high = np.bitwise_and.reduce(b & 0xFF00, axis=2)
        return np.where((b == 65535).any(axis=2), 65535, low | high).astype(values.dtype)
    if name == "layoverShadowMask":
        return np.bitwise_or.reduce(blocks.astype(np.uint32), axis=2).astype(values.dtype)
    if name == "numberOfLooks":
        return blocks.astype(np.float64).sum(axis=2).astype(values.dtype)
    return np.nanmean(blocks.astype(np.float64), axis=2).astype(values.dtype)


def earthdata_token() -> str | None:
    token = os.getenv("EARTHDATA_TOKEN", "").strip()
    if not token:
        env = get_settings().root / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.startswith("EARTHDATA_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
    return token or None


def subset_key(granule: str, bbox: list[float]) -> str:
    digest = hashlib.sha256(json.dumps([granule, [round(v, 5) for v in bbox]]).encode()).hexdigest()[:10]
    return f"{granule}__aoi-{digest}"


def find_subset(granule: str, bbox: list[float]) -> Path | None:
    """A previously acquired subset of this granule that covers the requested box."""
    root = get_settings().inputs
    for sidecar in root.glob(f"{granule}__aoi-*.h5.provenance.json"):
        try:
            source = json.loads(sidecar.read_text())
        except (OSError, ValueError):
            continue
        if not source.get("all_terms"):
            continue  # cached before every polarization was read
        b = source.get("covered_bbox") or []
        data = Path(str(sidecar)[: -len(".provenance.json")])
        if len(b) == 4 and data.is_file() and b[0] <= bbox[0] and b[1] <= bbox[1] and b[2] >= bbox[2] and b[3] >= bbox[3]:
            return data
    return None


_SIGNED: dict[str, tuple[str, int, float]] = {}
_SIGNED_LOCK = threading.Lock()


_URL_LOCKS: dict[str, threading.Lock] = {}


def _signed(url: str, token: str) -> tuple[str, int]:
    """Signed download URL and file size, reused in memory until shortly before the signature expires.

    A per-product lock makes a reader wait for a signing request already in flight instead of
    starting a second one."""
    with _SIGNED_LOCK:
        lock = _URL_LOCKS.setdefault(url, threading.Lock())
    with lock:
        with _SIGNED_LOCK:
            hit = _SIGNED.get(url)
        if hit and hit[2] > time.time():
            return hit[0], hit[1]
        signed, size = _signed_url(url, token)
        expires = parse_qs(urlsplit(signed).query).get("Expires", [""])[0]
        valid_until = time.time() + 1800
        if expires.isdigit():
            valid_until = min(valid_until, int(expires) - 300)
        with _SIGNED_LOCK:
            _SIGNED[url] = (signed, size, valid_until)
        return signed, size


def presign(observations: list[dict]) -> None:
    """Warm signed URLs for the products an investigation is about to read (best effort)."""
    token = earthdata_token()
    if not token:
        return
    from concurrent.futures import ThreadPoolExecutor
    urls = [o.get("download_url") for o in observations if isinstance(o.get("download_url"), str)]
    with ThreadPoolExecutor(max_workers=max(1, len(urls))) as pool:
        for future in [pool.submit(_signed, url, token) for url in urls]:
            try:
                future.result()
            except Exception:
                pass


def _signed_url(url: str, token: str) -> tuple[str, int]:
    # Authorization is attached only to the original NASA host, stripped at
    # the independently allowlisted CloudFront redirect, and never persisted.
    validate_url("earthdata", url)
    if httpx.URL(url).host not in ASF_HOSTS:
        raise AcquisitionError("Earthdata credentials can only be sent to the original ASF product host.")
    try:
        response = request_sync("earthdata", "GET", url,
                                headers={"Authorization": f"Bearer {token}", "Range": "bytes=0-7"},
                                timeout=15, max_bytes=8, follow_redirects=True)
    except ConnectorError as exc:
        if exc.status_code in (401, 403):
            raise AcquisitionError("Earthdata authorization was rejected. Update the server-side Earthdata credential.") from None
        raise AcquisitionError(str(exc)) from None
    if response.status_code not in (200, 206) or not response.content.startswith(b"\x89HDF"):
        raise AcquisitionError("ASF did not return a supported HDF5 product.")
    total = (response.headers.get("content-range") or "").rsplit("/", 1)[-1]
    if response.status_code != 206 or not total.isdigit():
        raise AcquisitionError("The product host does not support byte-range reads.")
    return str(response.url), int(total)


def _acquire_subset(observation: dict, bbox: list[float], frequency: str = "A") -> Path:
    token = earthdata_token()
    if not token:
        raise AcquisitionError("Set EARTHDATA_TOKEN in .env to read original NISAR products.")
    url = observation.get("download_url")
    granule = observation.get("id")
    if not url or not isinstance(granule, str) or not re.fullmatch(r"NISAR_[A-Za-z0-9_]{1,190}", granule):
        raise AcquisitionError("The catalog record has no original product URL.")
    started = time.monotonic()
    signed, size = _signed(url, token)
    validate_url("earthdata", signed)
    # Every byte is read through the shared connector: the signed URL is validated against the
    # Earthdata allowlist, and the HDF5 chunks covering the area are fetched in parallel.
    handle = RangeFile("earthdata", signed, block=1 << 20, workers=16, timeout=40, size=size)
    # NISAR GCOV keeps its HDF5 metadata at the start and end of the file. Reading both ends at once
    # replaces several one-at-a-time round trips while h5py walks the superblock and object headers.
    handle.prefetch([(0, HEAD_BYTES), (max(0, size - TAIL_BYTES), TAIL_BYTES)])
    root = get_settings().inputs
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{subset_key(granule, bbox)}.h5"
    temporary = target.with_suffix(".partial")
    with _hdf5_reader(), h5py.File(handle, "r") as src:
        group = f"{BASE}/GCOV/grids/frequency{frequency}"
        if group not in src or f"{BASE}/identification" not in src:
            raise AcquisitionError(f"Frequency {frequency} GCOV grid is missing from this granule.")
        grid = src[group]
        xs = np.asarray(grid["xCoordinates"][:], dtype=np.float64)
        ys = np.asarray(grid["yCoordinates"][:], dtype=np.float64)
        epsg = int(np.asarray(grid["projection"][()]).item())
        west, south, east, north = transform_bounds("EPSG:4326", f"EPSG:{epsg}", *bbox, densify_pts=21)
        dx, dy = xs[1] - xs[0], ys[1] - ys[0]
        c0 = max(0, math.floor((west - xs[0]) / dx) - 2); c1 = min(len(xs), math.ceil((east - xs[0]) / dx) + 3)
        r0 = max(0, math.floor((north - ys[0]) / dy) - 2); r1 = min(len(ys), math.ceil((south - ys[0]) / dy) + 3)
        if r0 >= r1 or c0 >= c1:
            raise AcquisitionError("The area of interest does not intersect this granule's grid.")
        # 10 m products (40 MHz) reach the native-pixel budget at about 20 km. Larger areas are
        # multilooked (k×k block averages of linear power), recorded in provenance and limitations.
        k = max(1, math.ceil(math.sqrt((r1 - r0) * (c1 - c0) / MAX_PIXELS)))
        if k > MAX_LOOKS:
            raise AcquisitionError("The area of interest is too large for this product's resolution. Draw an area smaller than about 70 km across.")
        r1 = r0 + ((r1 - r0) // k) * k
        c1 = c0 + ((c1 - c0) // k) * k
        # Every diagonal term the product carries (HH and HV, say) plus the quality masks, so change
        # can be measured and compared in each polarization.
        terms = [t for t in DIAGONALS if t in grid]
        if not terms or "mask" not in grid:
            raise AcquisitionError("The granule lacks diagonal covariance terms or its quality mask.")
        with h5py.File(temporary, "w") as dst:
            science = dst.require_group(BASE)
            src.copy(src[f"{BASE}/identification"], science, name="identification")
            out = dst.require_group(f"{BASE}/GCOV/grids/frequency{frequency}")
            needed = terms + [m for m in ("mask", "inputDataExceptionMask", "layoverShadowMask") if m in grid]
            _prefetch_window(handle, [grid[n] for n in needed], r0, r1, c0, c1)
            for name in needed:
                dataset = grid[name]
                if dataset.ndim != 2:
                    continue
                values = _looks(dataset[r0:r1, c0:c1], k, name)
                copy = out.create_dataset(name, data=values, compression="gzip", compression_opts=4, chunks=True)
                for key, value in dataset.attrs.items():
                    copy.attrs[key] = value
                if k > 1:
                    copy.attrs["terra_multilook"] = k
            for name, values in (("xCoordinates", xs[c0:c1].reshape(-1, k).mean(axis=1)), ("yCoordinates", ys[r0:r1].reshape(-1, k).mean(axis=1))):
                copy = out.create_dataset(name, data=values)
                for key, value in grid[name].attrs.items():
                    copy.attrs[key] = value
            for name in ("projection", "xCoordinateSpacing", "yCoordinateSpacing", "listOfPolarizations", "listOfCovarianceTerms"):
                if name in grid:
                    data = grid[name][()]
                    if name == "xCoordinateSpacing":
                        data = dx * k
                    elif name == "yCoordinateSpacing":
                        data = dy * k
                    copy = out.create_dataset(name, data=data)
                    for key, value in grid[name].attrs.items():
                        copy.attrs[key] = value
            dst.attrs["terra_subset_of"] = url
            dst.attrs["terra_subset_window"] = json.dumps({"row": [r0, r1], "col": [c0, c1], "multilook": k})
    temporary.replace(target)
    covered = transform_bounds(f"EPSG:{epsg}", "EPSG:4326", xs[c0] - dx / 2, ys[r1 - 1] + dy / 2, xs[c1 - 1] + dx / 2, ys[r0] - dy / 2, densify_pts=21)
    provenance = {
        "id": granule, "granule_id": granule, "source_url": url, "source": "NASA / ASF DAAC (Earthdata Cloud)",
        "method": ("HTTP byte-range read of the original HDF5; covariance, mask and identification values copied unmodified, spatially cropped only"
                   if k == 1 else f"HTTP byte-range read of the original HDF5, cropped and averaged in {k}×{k} blocks of linear power (multilooked); "
                   "blocks containing any invalid sample are masked; identification copied unmodified"),
        "multilook": k, "native_spacing_m": [abs(float(dx)), abs(float(dy))], "pixel_spacing_m": [abs(float(dx)) * k, abs(float(dy)) * k],
        "frequency": frequency, "epsg": epsg, "pixel_window": {"row_start": r0, "row_stop": r1, "column_start": c0, "column_stop": c1},
        "requested_bbox": bbox, "covered_bbox": list(covered), "terms": terms, "all_terms": True,
        "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": round(time.monotonic() - started, 1),
        "bytes_read": handle.bytes_fetched, "source_size_bytes": handle.size,
        "local_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "maturity": observation.get("maturity"),
    }
    Path(str(target) + ".provenance.json").write_text(json.dumps(provenance, indent=2))
    return target


def acquire_subset(observation: dict, bbox: list[float], frequency: str = "A") -> Path:
    """Keep transport exceptions (including signed URLs) out of workflow history."""
    try:
        if frequency not in ("A", "B"):
            raise AcquisitionError("Unsupported radar frequency.")
        if len(bbox) != 4 or not all(math.isfinite(x) for x in bbox):
            raise AcquisitionError("Invalid analysis bounds.")
        w, s, e, n = bbox
        if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
            raise AcquisitionError("Invalid analysis bounds.")
        return _acquire_subset(observation, bbox, frequency)
    except AcquisitionError:
        raise
    except Exception:
        raise AcquisitionError("The original radar product could not be read. Retry the investigation; credentials and signed download links are kept on the server.") from None
