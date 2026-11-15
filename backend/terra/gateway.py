"""Typed same-origin geographic delivery, never an arbitrary URL proxy."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .connector import ConnectorError, diagnostics, request

router = APIRouter(prefix="/api/connector")
ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services"
TERRAIN = "https://elevation3d.arcgis.com/arcgis/rest/services/WorldElevation3D/Terrain3D/ImageServer"
GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"

# Tiles are kept exactly as long as the provider's own Cache-Control allows (Esri: max-age 86400 with ETags), never
# when it says private or no-store, in memory and on disk, and revalidated with If-None-Match once they expire.
DEFAULT_TTL, MAX_TTL = 900, 86400
MEMORY_BYTES = 64 * 1024 * 1024
DISK_BYTES = int(os.getenv("TERRA_TILE_CACHE_MB", "2048")) * 1024 * 1024
TILE_TIMEOUT = 8.0  # a stalled upstream tile gives up quickly instead of holding a browser connection


@dataclass
class Entry:
    expires: float  # wall-clock time
    content: bytes
    mime: str
    etag: str | None

    def fresh(self) -> bool:
        return time.time() < self.expires


_memory: OrderedDict[str, Entry] = OrderedDict()
_memory_bytes = 0
_locks: dict[str, asyncio.Lock] = {}
_disk_state = {"written": 0, "evicting": False}


def _ttl(headers) -> int | None:
    """Seconds the provider allows this response to be reused, or None if it may not be stored."""
    control = (headers.get("cache-control") or "").lower()
    if "no-store" in control or "private" in control:
        return None
    match = re.search(r"max-age=(\d+)", control)
    return min(int(match.group(1)), MAX_TTL) if match else DEFAULT_TTL


def _directory() -> Path:
    from .config import get_settings
    return get_settings().data_dir / "tiles"


def _path(key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return _directory() / digest[:2] / f"{digest}.tile"


def _remember(key: str, entry: Entry) -> None:
    global _memory_bytes
    previous = _memory.pop(key, None)
    if previous:
        _memory_bytes -= len(previous.content)
    _memory[key] = entry; _memory_bytes += len(entry.content)
    while _memory_bytes > MEMORY_BYTES or len(_memory) > 8192:
        _, old = _memory.popitem(last=False); _memory_bytes -= len(old.content)


def _read_disk(key: str) -> Entry | None:
    try:
        with open(_path(key), "rb") as handle:
            meta = json.loads(handle.readline()); content = handle.read()
        return Entry(float(meta["expires"]), content, meta["mime"], meta.get("etag"))
    except (OSError, ValueError, KeyError):
        return None


def _write_disk(key: str, entry: Entry) -> None:
    path = _path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        with open(temporary, "wb") as handle:
            handle.write(json.dumps({"expires": entry.expires, "mime": entry.mime, "etag": entry.etag}).encode() + b"\n"); handle.write(entry.content)
        temporary.replace(path)
    except OSError:
        return
    _disk_state["written"] += len(entry.content)
    if _disk_state["written"] > 64 * 1024 * 1024 and not _disk_state["evicting"]:
        _disk_state["written"] = 0; _evict_disk()


def _evict_disk() -> None:
    """Keep the disk cache under its cap: drop the least recently used tiles until it is at 90 %."""
    _disk_state["evicting"] = True
    try:
        files = [(f.stat().st_atime, f.stat().st_size, f) for f in _directory().glob("*/*.tile")]
        total = sum(size for _, size, _ in files)
        if total > DISK_BYTES:
            for _, size, f in sorted(files, key=lambda item: item[0]):
                f.unlink(missing_ok=True); total -= size
                if total <= DISK_BYTES * 0.9:
                    break
    except OSError:
        pass
    finally:
        _disk_state["evicting"] = False


def _response(entry: Entry, state: str) -> Response:
    remaining = max(60, int(entry.expires - time.time()))
    return Response(entry.content, media_type=entry.mime, headers={"Cache-Control": f"public, max-age={remaining}", "X-Terra-Cache": state})


async def fetch(provider: str, url: str, *, params: dict | None = None, max_bytes: int = 8_000_000, timeout: float = TILE_TIMEOUT) -> tuple[Entry, str]:
    """A tile from memory, disk or the provider (revalidating an expired copy). Returns the entry and where it came from."""
    key = url + repr(sorted((params or {}).items()))
    entry = _memory.get(key)
    if entry and entry.fresh():
        _memory.move_to_end(key)
        return entry, "hit"
    lock = _locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:  # one upstream request per tile, however many people ask for it at once
            entry = _memory.get(key)
            if entry and entry.fresh():
                return entry, "hit"
            stored = entry or await asyncio.to_thread(_read_disk, key)
            if stored and stored.fresh():
                _remember(key, stored)
                return stored, "disk"
            headers = {"If-None-Match": stored.etag} if stored and stored.etag else None
            try:
                upstream = await request(provider, "GET", url, params=params, headers=headers, timeout=timeout, max_bytes=max_bytes)
            except ConnectorError as exc:
                if stored:  # the provider is down or slow: an expired copy beats an error
                    return stored, "stale"
                raise HTTPException(404 if exc.status_code == 404 else 502, str(exc)) from None
            ttl = _ttl(upstream.headers)
            if upstream.status_code == 304 and stored:
                fresh = Entry(time.time() + (ttl or DEFAULT_TTL), stored.content, stored.mime, upstream.headers.get("etag") or stored.etag)
                state = "revalidated"
            else:
                mime = upstream.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
                if mime in {"text/html", "application/xhtml+xml"}:
                    raise HTTPException(502, "The geographic provider returned an unexpected document.")
                fresh = Entry(time.time() + (ttl or 0), upstream.content, mime, upstream.headers.get("etag"))
                state = "miss"
            if ttl:
                _remember(key, fresh)
                await asyncio.to_thread(_write_disk, key, fresh)
            return fresh, state
    finally:
        if not lock.locked():
            _locks.pop(key, None)


def cached(provider_url: str, params: dict | None = None) -> bool:
    """Whether a fresh copy is already held in memory (used by the warmer to skip work)."""
    entry = _memory.get(provider_url + repr(sorted((params or {}).items())))
    return bool(entry and entry.fresh())


async def _asset(provider: str, url: str, *, params: dict | None = None, max_bytes: int = 8_000_000) -> Response:
    entry, state = await fetch(provider, url, params=params, max_bytes=max_bytes)
    return _response(entry, state)


def esri_tile(layer: str, z: int, y: int, x: int) -> str:
    return f"{ESRI}/{SOURCES[layer]}/MapServer/tile/{z}/{y}/{x}"


SOURCES = {"imagery": "World_Imagery", "labels": "Reference/World_Boundaries_and_Places",
           "topo": "World_Topo_Map", "topographic": "World_Topo_Map", "light": "Canvas/World_Light_Gray_Base",
           "relief-dark": "Elevation/World_Hillshade_Dark", "dark": "Canvas/World_Dark_Gray_Base",
           "dark-labels": "Canvas/World_Dark_Gray_Reference", "terrain-base": "World_Terrain_Base",
           "hillshade": "Elevation/World_Hillshade", "transport": "Reference/World_Transportation"}


@router.get("")
async def connector_status():
    return diagnostics()


@router.get("/maps/{layer}/{z}/{y}/{x}")
async def map_tile(layer: str, z: int, y: int, x: int):
    if layer not in SOURCES or not 0 <= z <= 23 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(404, "Map tile not found.")
    return await _asset("imagery", esri_tile(layer, z, y, x))


@router.get("/gibs/{path:path}")
async def gibs_tile(path: str):
    patterns = (
        r"BlueMarble_ShadedRelief_Bathymetry/default/GoogleMapsCompatible_Level8/(\d+)/(\d+)/(\d+)\.jpg",
        r"VIIRS_Black_Marble/default/2016-01-01/GoogleMapsCompatible_Level8/(\d+)/(\d+)/(\d+)\.png",
    )
    match = next((m for pattern in patterns if (m := re.fullmatch(pattern, path))), None)
    if not match:
        raise HTTPException(404, "NASA imagery tile not found.")
    z, y, x = map(int, match.groups())
    if not 0 <= z <= 8 or not 0 <= y < 2**z or not 0 <= x < 2**z:
        raise HTTPException(404, "NASA imagery tile not found.")
    return await _asset("gibs", f"{GIBS}/{path}")


@router.get("/terrain")
@router.get("/terrain/{path:path}")
async def terrain_asset(request: Request, path: str = ""):
    # Cesium asks only for ImageServer metadata and LERC height tiles.
    if path:
        match = re.fullmatch(r"tile/(\d+)/(\d+)/(\d+)", path)
        availability = re.fullmatch(r"tilemap/(\d+)/(\d+)/(\d+)/(\d+)/(\d+)", path)
        if not match and not availability:
            raise HTTPException(404, "Terrain resource not found.")
        z, y, x = map(int, (match or availability).groups()[:3])
        if not 0 <= z <= 23 or not 0 <= y < 2**z or not 0 <= x < 2**z:
            raise HTTPException(404, "Terrain tile not found.")
        if availability:
            height, width = map(int, availability.groups()[3:])
            if not 1 <= height <= 128 or not 1 <= width <= 128 or y + height > 2**z or x + width > 2**z:
                raise HTTPException(404, "Terrain availability region not found.")
    allowed = {"f": {"json", "pjson"}, "blankTile": {"false", "true"}}
    params = {}
    for key, value in request.query_params.items():
        if key not in allowed or value not in allowed[key]:
            raise HTTPException(422, "Unsupported terrain parameter.")
        params[key] = value
    return await _asset("terrain", TERRAIN + ("/" + path if path else ""), params=params,
                        max_bytes=8_000_000)
