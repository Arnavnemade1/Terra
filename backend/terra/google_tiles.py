"""Google Map Tiles API: Photorealistic 3D Tiles served through the same-origin connector.

The browser never sees the key. Cesium asks this gateway for the tileset; the gateway
attaches the key as a request header (never in a URL), forwards only the documented
tileset paths, and rewrites the tileset's absolute child URIs so every follow-up request
comes back through here.

Google's terms for this API (developers.google.com/maps/documentation/tile/policies):
- No pre-fetching, storing or caching beyond what the response's Cache-Control allows.
  Tiles arrive as `private`, so this server keeps no copy; the browser may cache them
  for the stated max-age, and ETags are passed through for revalidation.
- Per-tile attribution (glTF `asset.copyright`) and the Google Maps logo must be shown.
  The client does that; see `src/Globe.tsx`.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .config import get_settings
from .connector import ConnectorError, request

router = APIRouter(prefix="/api/connector/google3d")
UPSTREAM = "https://tile.googleapis.com/v1/3dtiles"
PUBLIC = "/api/connector/google3d"
KEY_NAMES = ("GOOGLE_MAPS_TILES_KEY", "GOOGLE_MAPS_KEY", "VITE_GOOGLE_MAPS_KEY")
_FILE = re.compile(r"datasets/[A-Za-z0-9_-]{1,64}/files/[A-Za-z0-9_-]{1,512}\.(json|glb)")
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,256}")
_PASS_HEADERS = ("cache-control", "etag", "expires", "last-modified")
_status: dict = {"checked": 0.0, "value": None, "referer": None}
_status_lock = asyncio.Lock()
STATUS_TTL = 600


def api_key() -> str | None:
    """The server-side key: environment first, then the project's .env (never a VITE_ build input)."""
    for name in KEY_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return value
    env = get_settings().root / ".env"
    if env.is_file():
        values = {}
        for line in env.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep:
                values[name.strip()] = value.strip().strip('"').strip("'")
        for name in KEY_NAMES:
            if values.get(name):
                return values[name]
    return None


def referer(request_headers: dict) -> str | None:
    """The page Terra is open on, sent to Google so keys restricted to websites (HTTP referrers) work.

    Google checks a referrer-restricted key against the Referer of each request. The browser's own request to
    this gateway carries it, so forwarding it gives the same result as a direct browser request.
    GOOGLE_MAPS_TILES_REFERER overrides it (for example the public address of a deployment).
    Only scheme, host and path are sent: no query string, which can hold an investigation id.
    """
    for value in (os.getenv("GOOGLE_MAPS_TILES_REFERER", "").strip(), request_headers.get("referer", ""),
                  (request_headers.get("origin", "") + "/") if request_headers.get("origin") else ""):
        if not value or len(value) > 512 or any(ord(c) < 33 for c in value):
            continue
        try:
            parts = urlsplit(value)
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and parts.hostname:
            return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"
    return None


REFUSED = ("Google refused this key (HTTP 403). If the key is restricted to websites, allow the address Terra runs on "
           "(locally http://localhost:5173/* and http://127.0.0.1:5173/*) or set GOOGLE_MAPS_TILES_REFERER. Otherwise check "
           "that the key's API restrictions include the Map Tiles API, that the API is enabled and that billing is on.")


def rewrite(document: bytes) -> bytes:
    """Point the tileset's absolute child URIs back at this gateway."""
    return document.replace(b'"/v1/3dtiles/', f'"{PUBLIC}/'.encode())


async def _fetch(path: str, params: dict | None, request_headers: dict, max_bytes: int) -> Response:
    key = api_key()
    if not key:
        raise HTTPException(503, "Photorealistic 3D tiles need a Google Maps Platform key with the Map Tiles API enabled.")
    headers = {"X-Goog-Api-Key": key}
    page = referer(request_headers)
    if page:
        headers["Referer"] = page
    if request_headers.get("if-none-match"):
        headers["If-None-Match"] = request_headers["if-none-match"]
    try:
        upstream = await request("google", "GET", f"{UPSTREAM}/{path}", params=params, headers=headers,
                                 timeout=20, max_bytes=max_bytes)
    except ConnectorError as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(502, REFUSED) from None
        if exc.status_code == 429:
            raise HTTPException(429, "Google Map Tiles quota reached. Try again later.") from None
        raise HTTPException(404 if exc.status_code == 404 else 502, str(exc)) from None
    out = {k: upstream.headers[k] for k in _PASS_HEADERS if k in upstream.headers}
    # Never let a shared cache (a proxy or CDN in front of Terra) keep Google content.
    out.setdefault("cache-control", "private, no-store")
    if "private" not in out["cache-control"] and "no-store" not in out["cache-control"]:
        out["cache-control"] = "private, " + out["cache-control"]
    if upstream.status_code == 304:
        return Response(status_code=304, headers=out)
    mime = upstream.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
    content = upstream.content
    if path.endswith(".json"):
        if mime != "application/json":
            raise HTTPException(502, "Google returned an unexpected tileset document.")
        content = rewrite(content)
    elif mime != "model/gltf-binary":
        raise HTTPException(502, "Google returned an unexpected tile.")
    return Response(content, media_type=mime, headers=out)


@router.get("/status")
async def status(req: Request):
    return await availability(dict(req.headers))


async def availability(request_headers: dict | None = None) -> dict:
    """Whether photorealistic tiles can be served from the asking page, probed at most every ten minutes."""
    if not api_key():
        return {"configured": False, "available": False, "reason": "No Google Maps Platform key is configured on the server."}
    headers = {k: v for k, v in (request_headers or {}).items() if k in ("referer", "origin")}
    page = referer(headers)
    async with _status_lock:
        if _status["value"] and _status["referer"] == page and time.monotonic() - _status["checked"] < STATUS_TTL:
            return _status["value"]
        try:
            await _fetch("root.json", None, headers, 2_000_000)
            value = {"configured": True, "available": True, "reason": None}
        except HTTPException as exc:
            value = {"configured": True, "available": False, "reason": exc.detail}
        _status.update(checked=time.monotonic(), value=value, referer=page)
        return value


@router.get("/root.json")
async def root(req: Request):
    if req.query_params:
        raise HTTPException(422, "Unsupported tileset parameter.")
    return await _fetch("root.json", None, dict(req.headers), 2_000_000)


@router.get("/{path:path}")
async def tile(path: str, req: Request):
    if not _FILE.fullmatch(path):
        raise HTTPException(404, "Tile not found.")
    session = req.query_params.get("session", "")
    if set(req.query_params) != {"session"} or not _SESSION.fullmatch(session):
        raise HTTPException(422, "Unsupported tile parameter.")
    return await _fetch(path, {"session": session}, dict(req.headers), 2_000_000 if path.endswith(".json") else 16_000_000)
