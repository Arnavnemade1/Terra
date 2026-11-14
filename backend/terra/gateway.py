"""Typed same-origin geographic delivery, never an arbitrary URL proxy."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .connector import ConnectorError, diagnostics, request

router = APIRouter(prefix="/api/connector")
_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_cache_bytes = 0
MAX_CACHE_BYTES = 64 * 1024 * 1024
_locks: dict[str, asyncio.Lock] = {}
ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services"
TERRAIN = "https://elevation3d.arcgis.com/arcgis/rest/services/WorldElevation3D/Terrain3D/ImageServer"
GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"


async def _asset(provider: str, url: str, *, params: dict | None = None, max_bytes: int = 8_000_000) -> Response:
    global _cache_bytes
    cache_key = url + repr(sorted((params or {}).items()))
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < 900:
        _cache.move_to_end(cache_key)
        return Response(cached[1], media_type=cached[2], headers={"Cache-Control": "public, max-age=900", "X-Terra-Cache": "hit"})
    # An entry exists only while the corresponding fetch is in flight.
    lock = _locks.setdefault(cache_key, asyncio.Lock())
    try:
        async with lock:
            cached = _cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 900:
                return Response(cached[1], media_type=cached[2], headers={"Cache-Control": "public, max-age=900"})
            try:
                upstream = await request(provider, "GET", url, params=params, timeout=15, max_bytes=max_bytes)
            except ConnectorError as exc:
                status = 404 if exc.status_code == 404 else 502
                raise HTTPException(status, str(exc)) from None
            mime = upstream.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
            if mime in {"text/html", "application/xhtml+xml"}:
                raise HTTPException(502, "The geographic provider returned an unexpected document.")
            content = upstream.content
            previous = _cache.pop(cache_key, None)
            if previous:
                _cache_bytes -= len(previous[1])
            if len(content) <= MAX_CACHE_BYTES:
                _cache[cache_key] = (time.monotonic(), content, mime)
                _cache_bytes += len(content)
                while _cache_bytes > MAX_CACHE_BYTES or len(_cache) > 1024:
                    _, old = _cache.popitem(last=False)
                    _cache_bytes -= len(old[1])
            return Response(content, media_type=mime, headers={"Cache-Control": "public, max-age=900", "X-Terra-Cache": "miss"})
    finally:
        if not lock.locked():
            _locks.pop(cache_key, None)


@router.get("")
async def connector_status():
    return diagnostics()


@router.get("/maps/{layer}/{z}/{y}/{x}")
async def map_tile(layer: str, z: int, y: int, x: int):
    sources = {"imagery": "World_Imagery", "labels": "Reference/World_Boundaries_and_Places",
               "topo": "World_Topo_Map", "topographic": "World_Topo_Map", "light": "Canvas/World_Light_Gray_Base",
               "relief-dark": "Elevation/World_Hillshade_Dark", "dark": "Canvas/World_Dark_Gray_Base",
               "dark-labels": "Canvas/World_Dark_Gray_Reference", "terrain-base": "World_Terrain_Base",
               "hillshade": "Elevation/World_Hillshade", "transport": "Reference/World_Transportation"}
    if layer not in sources or not 0 <= z <= 23 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(404, "Map tile not found.")
    return await _asset("imagery", f"{ESRI}/{sources[layer]}/MapServer/tile/{z}/{y}/{x}")


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
