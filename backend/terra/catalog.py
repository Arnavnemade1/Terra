"""Public ASF NISAR discovery; browse imagery is never numerical science data.

Only public catalog and browse resources are requested here. HDF5 download
requires Earthdata authentication and belongs in the acquisition service.
"""
from __future__ import annotations

import asyncio
import json
import math
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import httpx
from .connector import ProviderClient, ConnectorError

ASF_SEARCH = "https://api.daac.asf.alaska.edu/services/search/param"
BROWSE_HOST = "nisar.asf.earthdatacloud.nasa.gov"


class CatalogError(RuntimeError):
    """Upstream discovery failed; callers must not interpret this as no data."""


def validate_bbox(bbox: list[float]) -> list[float]:
    if len(bbox) != 4 or any(not math.isfinite(float(v)) for v in bbox):
        raise ValueError("Bounding box must contain four finite coordinates.")
    west, south, east, north = map(float, bbox)
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ValueError("Longitude must be between -180 and 180 degrees.")
    if not (-90 <= south < north <= 90) or west == east:
        raise ValueError("Bounding box must have positive height and width.")
    return [west, south, east, north]


def _points(coordinates: Any):
    if isinstance(coordinates, list) and coordinates:
        if isinstance(coordinates[0], (float, int)):
            yield coordinates[:2]
        else:
            for child in coordinates:
                yield from _points(child)


def normalize_observation(feature: dict) -> dict:
    p = feature["properties"]
    name = p.get("sceneName") or p.get("fileID")
    if not name or p.get("platform") != "NISAR" or p.get("processingLevel") != "GCOV":
        raise ValueError("Expected a NISAR GCOV catalog feature.")
    collection = p.get("collectionName", "")
    if "PROVISIONAL" in collection:
        maturity = "PROVISIONAL"
    elif "BETA" in collection:
        maturity = "BETA"
    elif collection == "NISAR_L2_GCOV_V1":
        maturity = "VALIDATED"
    else:
        maturity = "UNKNOWN"
    points = list(_points(feature.get("geometry", {}).get("coordinates", [])))
    bounds = [min(c[0] for c in points), min(c[1] for c in points),
              max(c[0] for c in points), max(c[1] for c in points)] if points else None
    browse = next((u for u in (p.get("browse") or []) if u.endswith("_LATLON.png")), None)
    kml = next((u for u in (p.get("additionalUrls") or []) if u.endswith("_LATLON.kml")), None)
    size = p.get("bytes", {}).get(p.get("fileName"), {}).get("bytes")
    primary = p.get("mainBandPolarization") or []
    secondary = p.get("sideBandPolarization") or []
    return {
        "id": name,
        "title": name,
        "date": p.get("startTime"),
        "end_date": p.get("stopTime"),
        "product": "GCOV",
        "maturity": maturity,
        "polarizations": primary or secondary,
        "frequency": "A" if primary else "B",
        "polarizations_by_frequency": {"A": primary, "B": secondary},
        "track": p.get("pathNumber"),
        "frame": p.get("frameNumber"),
        "direction": p.get("flightDirection"),
        "bbox": bounds,
        "geometry": feature.get("geometry"),
        "browse_url": browse,
        "browse_kml_url": kml,
        "download_url": p.get("url"),
        "size_mb": round(size / 1_000_000, 2) if size else None,
        "collection": collection,
        "collection_id": p.get("conceptID"),
        "product_version": p.get("crid"),
        "processor_version": p.get("pgeVersion"),
        "bandwidth": p.get("rangeBandwidth"),
        "frame_coverage": p.get("frameCoverage"),
        "source": "NASA / ASF DAAC",
        "access": "earthdata_login_required",
        "browse_kind": "Rendered browse composite — not calibrated pixel values",
    }


async def _request(client: httpx.AsyncClient, params: dict) -> dict:
    for attempt in range(3):
        try:
            response = await client.get(ASF_SEARCH, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
            response.raise_for_status()
            payload = response.json()
            if payload.get("type") != "FeatureCollection":
                raise CatalogError("ASF returned an unexpected catalog response.")
            return payload
        except (httpx.HTTPError, ConnectorError, ValueError) as exc:
            if attempt == 2 or isinstance(exc, httpx.HTTPStatusError):
                raise CatalogError("ASF discovery is unavailable. Retry this search shortly.") from exc
            await asyncio.sleep(0.5 * (2 ** attempt))
    raise CatalogError("ASF discovery is unavailable.")


CACHE_SECONDS = 600


def _cache_file(bbox: list[float], limit: int):
    from hashlib import sha256
    from .config import get_settings
    key = sha256(json.dumps([[round(v, 4) for v in bbox], limit]).encode()).hexdigest()[:16]
    folder = get_settings().data_dir / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"catalog-{key}.json"


async def search_observations(bbox: list[float], limit: int = 24) -> list[dict]:
    """Catalog search with a short shared cache, so the planner reuses the search the UI just made."""
    import time
    path = _cache_file(bbox, limit)
    try:
        if time.time() - path.stat().st_mtime < CACHE_SECONDS:
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    for wider in (100, 60, 40):  # a larger cached search for the same box also answers a smaller one
        if wider > limit:
            try:
                other = _cache_file(bbox, wider)
                if time.time() - other.stat().st_mtime < CACHE_SECONDS:
                    return json.loads(other.read_text())[:limit]
            except (OSError, ValueError):
                pass
    items = await _search_observations(bbox, limit)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(items))
    temporary.replace(path)
    return items


async def _search_observations(bbox: list[float], limit: int = 24) -> list[dict]:
    """Find latest standard-production GCOV observations for a WGS84 region.

    Supports antimeridian regions (west > east) as two bounded requests. Public
    ASF metadata are authoritative; availability errors are distinct from [].
    No acquisition dates, measurements, or provider URLs are fabricated.
    """
    west, south, east, north = validate_bbox(bbox)
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Catalog limit must be an integer from 1 to 100.")
    boxes = [[west, south, east, north]] if west < east else [
        [west, south, 180, north], [-180, south, east, north]]
    async with ProviderClient("asf", timeout=15) as client:
        payloads = await asyncio.gather(*[_request(client, {
            "dataset": "NISAR", "processingLevel": "GCOV",
            "productionConfiguration": "PR", "output": "geojson",
            "maxResults": limit, "bbox": ",".join(map(str, box)),
        }) for box in boxes])
    observations = {}
    for payload in payloads:
        for feature in payload.get("features", []):
            item = normalize_observation(feature)
            observations[item["id"]] = item
    return sorted(observations.values(), key=lambda i: i.get("date") or "", reverse=True)[:limit]


async def get_observation(observation_id: str) -> dict | None:
    """Resolve a provider granule ID through ASF, not a client-supplied URL."""
    if not observation_id.startswith("NISAR_") or len(observation_id) > 200 or not all(
        c.isalnum() or c == "_" for c in observation_id
    ):
        raise ValueError("Invalid NISAR observation identifier.")
    async with ProviderClient("asf", timeout=15) as client:
        payload = await _request(client, {
            "dataset": "NISAR", "output": "geojson", "granule_list": observation_id,
        })
    for feature in payload.get("features", []):
        observation = normalize_observation(feature)
        if observation["id"] == observation_id:
            return observation
    return None


async def browse_georeference(observation: dict) -> dict:
    """Read the publisher's KML bounds, never stretch a browse to its footprint.

    The footprint polygon and image extent are different. This method must
    succeed before placing the public browse image on a globe.
    """
    url = observation.get("browse_kml_url")
    parsed = urlparse(url or "")
    if (parsed.scheme != "https" or parsed.hostname != BROWSE_HOST
            or not parsed.path.startswith("/BROWSE/") or not parsed.path.endswith("_LATLON.kml")):
        raise ValueError("No trusted georeferenced browse is available for this product.")
    async with ProviderClient("asf", timeout=12) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > 100_000:
            raise ValueError("Unexpected browse georeference size.")
        root = ET.fromstring(response.content)
    def value(name: str) -> float:
        element = root.find(f".//{{*}}LatLonBox/{{*}}{name}")
        if element is None or element.text is None:
            raise ValueError("Publisher KML has no rectangular browse extent.")
        return float(element.text)
    bounds = validate_bbox([value("west"), value("south"), value("east"), value("north")])
    rotation = root.find(".//{*}LatLonBox/{*}rotation")
    angle = float(rotation.text) if rotation is not None and rotation.text else 0
    if abs(angle) > 0.000001:
        raise ValueError("Rotated browse overlays require a different renderer.")
    return {"url": observation.get("browse_url"), "bbox": bounds,
            "kind": "browse", "units": None, "quantitative": False,
            "attribution": "NASA / JPL-Caltech / ASF DAAC",
            "description": "Publisher-rendered GCOV composite; unsuitable for pixel measurements."}
