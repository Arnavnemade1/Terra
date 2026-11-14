"""The server-side boundary for external services.

HTTP pools are shared per process/event loop (and one thread-safe sync pool).
Provider hosts are fixed here; callers cannot turn this into an arbitrary proxy.
Credentials are attached per request and never logged, returned in diagnostics,
or forwarded to a different origin. GeoTIFF/HDF5 range readers remain specialist
transports, but must validate their source through this same policy boundary.
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict
import logging
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit
from weakref import WeakKeyDictionary

import httpx

PROVIDERS: dict[str, tuple[str, ...]] = {
    "openai": ("api.openai.com",),
    "asf": ("api.daac.asf.alaska.edu", "nisar.asf.earthdatacloud.nasa.gov"),
    "earthdata": ("nisar.asf.earthdatacloud.nasa.gov", "datapool.asf.alaska.edu", "sentinel1.asf.alaska.edu", "urs.earthdata.nasa.gov", "d1mv8zhcvry6x4.cloudfront.net"),
    "celestrak": ("celestrak.org",),
    "tle": ("tle.ivanstanojevic.me",),
    "usgs": ("earthquake.usgs.gov", "volcanoes.usgs.gov"),
    "gdacs": ("www.gdacs.org",),
    "eonet": ("eonet.gsfc.nasa.gov",),
    "firms": ("firms.modaps.eosdis.nasa.gov",),
    "geocode": ("photon.komoot.io",),
    "stac": ("earth-search.aws.element84.com",),
    "sentinel": ("sentinel-cogs.s3.us-west-2.amazonaws.com",),
    "imagery": ("services.arcgisonline.com",),
    "terrain": ("elevation3d.arcgis.com",),
    "gibs": ("gibs.earthdata.nasa.gov",),
    "openmeteo": ("api.open-meteo.com", "archive-api.open-meteo.com"),
    "worldcover": ("esa-worldcover.s3.eu-central-1.amazonaws.com",),
    "wikidata": ("query.wikidata.org",),
    "google": ("tile.googleapis.com",),
}
LABELS = {"openai": "Language model", "asf": "NASA/ASF catalog", "earthdata": "NASA Earthdata",
          "celestrak": "CelesTrak", "tle": "TLE mirror", "usgs": "USGS", "eonet": "NASA EONET", "firms": "NASA FIRMS",
          "gdacs": "GDACS alerts", "geocode": "Place search", "stac": "Optical catalog", "sentinel": "Sentinel imagery",
          "imagery": "World imagery", "terrain": "Terrain", "gibs": "NASA imagery", "openmeteo": "Weather and elevation models",
          "worldcover": "ESA WorldCover land cover", "wikidata": "Wikidata named places",
          "google": "Google Map Tiles"}
_pools: WeakKeyDictionary = WeakKeyDictionary()
_sync_pool: httpx.Client | None = None
_sync_lock = threading.Lock()
_stats: dict[str, dict] = defaultdict(lambda: {"requests": 0, "failures": 0, "last_ms": None, "last_status": None})
_stats_lock = threading.Lock()

# httpx INFO contains full request URLs, which can contain temporary signed URLs.
for _logger in ("httpx", "httpcore", "fsspec", "aiohttp"):
    logging.getLogger(_logger).setLevel(logging.WARNING)


class ConnectorError(RuntimeError):
    def __init__(self, provider: str, reason: str, status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"{LABELS.get(provider, 'External service')}: {reason}")


def validate_url(provider: str, url: str) -> str:
    """Reject private, credential-bearing, non-HTTPS and unregistered destinations."""
    try:
        parsed = urlsplit(url)
        valid = (provider in PROVIDERS and parsed.scheme == "https" and parsed.hostname in PROVIDERS[provider]
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment
                 and not any(ord(character) < 32 for character in url) and "\\" not in url)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ConnectorError(provider, "destination is not permitted.")
    return url


def provider_for(url: str) -> str:
    host = urlsplit(url).hostname
    for provider, hosts in PROVIDERS.items():
        if host in hosts:
            return provider
    raise ConnectorError("unknown", "destination is not permitted.")


def _options() -> dict:
    return {"limits": httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=45),
            "timeout": httpx.Timeout(20, connect=5, pool=5), "follow_redirects": False,
            "trust_env": False, "headers": {"User-Agent": "TERRA/0.1 (Earth observation research client)"}}


def _async_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _pools.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(**_options())
        _pools[loop] = client
    return client


def _sync_client() -> httpx.Client:
    global _sync_pool
    with _sync_lock:
        if _sync_pool is None or _sync_pool.is_closed:
            _sync_pool = httpx.Client(**_options())
    return _sync_pool


async def close() -> None:
    """Close the pool belonging to the current API/worker event loop."""
    client = _pools.pop(asyncio.get_running_loop(), None)
    if client:
        await client.aclose()


def _record(provider: str, started: float, status: int | None, failed: bool) -> None:
    with _stats_lock:
        item = _stats[provider]
        item.update(requests=item["requests"] + 1, failures=item["failures"] + int(failed),
                    last_ms=round((time.monotonic() - started) * 1000), last_status=status)


def diagnostics() -> dict:
    with _stats_lock:
        services = {name: {"label": LABELS[name], **dict(_stats[name])} for name in PROVIDERS}
    return {"boundary": "server", "transport": "shared connection pools", "credentials": "server only",
            "scope": "this process", "services": services,
            "raster_transport": "connector range reader (HDF5 and Cloud-Optimized GeoTIFF)"}


def _headers(provider: str, values: dict | None) -> dict:
    headers = dict(values or {})
    if any(key.lower() in {"cookie", "host", "proxy-authorization"} for key in headers):
        raise ConnectorError(provider, "request headers are not permitted.")
    if any(key.lower() == "authorization" for key in headers) and provider not in {"openai", "earthdata"}:
        raise ConnectorError(provider, "credentials are not permitted for this service.")
    if any(key.lower() == "x-goog-api-key" for key in headers) and provider != "google":
        raise ConnectorError(provider, "credentials are not permitted for this service.")
    return headers


def _redirect(provider: str, current: str, response: httpx.Response, headers: dict) -> tuple[str, dict]:
    destination = urljoin(current, response.headers.get("location", ""))
    validate_url(provider, destination)
    if urlsplit(destination).netloc != urlsplit(current).netloc:
        headers = {key: value for key, value in headers.items() if key.lower() not in {"authorization", "cookie", "x-api-key", "x-goog-api-key"}}
    return destination, headers


async def request(provider: str, method: str, url: str, *, params=None, json=None,
                  headers: dict | None = None, timeout: float = 20, max_bytes: int = 20_000_000,
                  follow_redirects: bool = False) -> httpx.Response:
    validate_url(provider, url)
    request_headers = _headers(provider, headers)
    started, status, failed = time.monotonic(), None, True
    try:
        async with asyncio.timeout(timeout):
            for hop in range(4):
                async with _async_client().stream(method, url, params=params, json=json, headers=request_headers,
                                                  timeout=httpx.Timeout(timeout, connect=min(5, timeout), pool=5)) as response:
                    status = response.status_code
                    if response.is_redirect and status != 304:  # 304 Not Modified is a cache answer, not a redirect
                        if not follow_redirects or hop == 3:
                            raise ConnectorError(provider, "unexpected redirect.", status)
                        url, request_headers = _redirect(provider, str(response.url), response, request_headers)
                        params = None
                        continue
                    if response.status_code >= 400:
                        raise ConnectorError(provider, f"upstream request failed (HTTP {status}).", status)
                    length = response.headers.get("content-length")
                    if length and int(length) > max_bytes:
                        raise ConnectorError(provider, "response exceeds the size limit.", status)
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            raise ConnectorError(provider, "response exceeds the size limit.", status)
                    failed = False
                    # Do not carry upstream cookies into application responses.
                    clean_headers = {k: v for k, v in response.headers.items() if k.lower() not in {"set-cookie", "content-encoding", "content-length"}}
                    return httpx.Response(status, content=bytes(content), headers=clean_headers, request=response.request)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise ConnectorError(provider, "request timed out or the service is unreachable.") from None
    except ValueError:
        raise ConnectorError(provider, "upstream response was malformed.") from None
    finally:
        _record(provider, started, status, failed)
    raise ConnectorError(provider, "request failed.")


def request_sync(provider: str, method: str, url: str, *, headers: dict | None = None,
                 timeout: float = 20, max_bytes: int = 20_000_000, follow_redirects: bool = False) -> httpx.Response:
    """Thread-safe bounded synchronous requests for scientific acquisition."""
    validate_url(provider, url)
    request_headers = _headers(provider, headers)
    started, status, failed = time.monotonic(), None, True
    try:
        for hop in range(4):
            with _sync_client().stream(method, url, headers=request_headers, timeout=timeout) as response:
                status = response.status_code
                if response.is_redirect and status != 304:  # 304 Not Modified is a cache answer, not a redirect
                    if not follow_redirects or hop == 3:
                        raise ConnectorError(provider, "unexpected redirect.", status)
                    url, request_headers = _redirect(provider, str(response.url), response, request_headers)
                    continue
                if status >= 400:
                    raise ConnectorError(provider, f"upstream request failed (HTTP {status}).", status)
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=min(65536, max_bytes + 1)):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ConnectorError(provider, "response exceeds the size limit.", status)
                failed = False
                return httpx.Response(status, content=bytes(content), headers=response.headers, request=response.request)
    except httpx.HTTPError:
        raise ConnectorError(provider, "request timed out or the service is unreachable.") from None
    finally:
        _record(provider, started, status, failed)
    raise ConnectorError(provider, "request failed.")


class ProviderClient:
    """Small compatibility adapter; context management does not close the pool."""
    def __init__(self, provider: str, timeout: float = 20):
        self.provider, self.timeout = provider, timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await request(self.provider, "GET", url, timeout=self.timeout, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await request(self.provider, "POST", url, timeout=self.timeout, **kwargs)


class RangeFile(io.RawIOBase):
    """A read-only, seekable remote file served through this connector with HTTP byte ranges.

    Blocks are cached; `prefetch` fetches many byte ranges concurrently so libraries such as
    h5py and rasterio read from memory instead of issuing one small request at a time.
    """
    def __init__(self, provider: str, url: str, *, headers: dict | None = None, block: int = 1 << 20, workers: int = 12,
                 timeout: float = 30, size: int | None = None):
        super().__init__()
        validate_url(provider, url)
        self.provider, self.url, self.headers, self.block, self.workers, self.timeout = provider, url, dict(headers or {}), block, workers, timeout
        self.blocks: dict[int, bytes] = {}
        self.lock = threading.Lock()
        self.pos = 0
        self.bytes_fetched = 0
        self.requests = 0
        if size is None:  # callers that already know the size (from an earlier range response) skip the probe
            probe = request_sync(provider, "GET", url, headers={**self.headers, "Range": "bytes=0-0"}, timeout=timeout, max_bytes=1)
            total = (probe.headers.get("content-range") or "").rsplit("/", 1)[-1]
            if probe.status_code != 206 or not total.isdigit():
                raise ConnectorError(provider, "the source does not support byte-range reads.", probe.status_code)
            size = int(total)
        self.size = int(size)

    def view(self) -> "RangeFile":
        """Another independent handle onto the same remote file and cache (no extra probe request)."""
        clone = RangeFile.__new__(RangeFile)
        io.RawIOBase.__init__(clone)
        clone.__dict__.update({k: v for k, v in self.__dict__.items() if k != "pos"})
        clone.pos = 0
        return clone

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self.pos = offset if whence == io.SEEK_SET else self.pos + offset if whence == io.SEEK_CUR else self.size + offset
        return self.pos

    def _fetch(self, index: int) -> None:
        start = index * self.block
        end = min(self.size, start + self.block) - 1
        response = request_sync(self.provider, "GET", self.url, headers={**self.headers, "Range": f"bytes={start}-{end}"},
                                timeout=self.timeout, max_bytes=self.block)
        if response.status_code != 206:
            raise ConnectorError(self.provider, "range request was not honoured.", response.status_code)
        with self.lock:
            self.blocks[index] = response.content
            self.bytes_fetched += len(response.content)
            self.requests += 1

    def prefetch(self, ranges: list[tuple[int, int]]) -> None:
        """Fetch every block covering the given (offset, length) ranges, in parallel."""
        from concurrent.futures import ThreadPoolExecutor
        wanted = sorted({i for offset, length in ranges if length > 0
                         for i in range(offset // self.block, (offset + length - 1) // self.block + 1)} - set(self.blocks))
        if not wanted:
            return
        with ThreadPoolExecutor(max_workers=min(self.workers, len(wanted))) as pool:
            for future in [pool.submit(self._fetch, i) for i in wanted]:
                future.result()

    def readinto(self, buffer) -> int:
        if self.pos >= self.size:
            return 0
        n = min(len(buffer), self.size - self.pos)
        first, last = self.pos // self.block, (self.pos + n - 1) // self.block
        missing = [i for i in range(first, last + 1) if i not in self.blocks]
        if len(missing) > 1:
            self.prefetch([(i * self.block, self.block) for i in missing])
        elif missing:
            self._fetch(missing[0])
        view, written = memoryview(buffer), 0
        while written < n:
            index, offset = divmod(self.pos, self.block)
            chunk = self.blocks[index][offset: offset + n - written]
            view[written: written + len(chunk)] = chunk
            written += len(chunk)
            self.pos += len(chunk)
        return written
