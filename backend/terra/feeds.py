"""Live context feeds for the globe: real public sources, cached, never invented.

Each feed returns compact records plus provenance (source, fetched_at, status).
When a provider fails, the last good copy is served and marked stale; with no
copy at all, the caller receives FeedError and the UI shows the feed offline.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from .connector import request, provider_for

USER_AGENT = "TERRA/0.1 (Earth observation research client)"


class FeedError(RuntimeError):
    pass


_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _disk(name: str) -> Path:
    from .config import get_settings
    return get_settings().data_dir / "cache" / f"feed-{re.sub(r'[^a-z0-9_]', '', name)}.json"


def _remembered(name: str) -> tuple[float, dict] | None:
    """The in-memory copy, or the copy saved on disk by any Terra process (API or worker)."""
    hit = _cache.get(name)
    if hit:
        return hit
    try:
        saved = json.loads(_disk(name).read_text())
        hit = (float(saved["saved_at"]), saved["value"])
        _cache[name] = hit
        return hit
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def _cached(name: str, ttl: float, loader: Callable[[], Awaitable[dict]]) -> dict:
    # Wall-clock ages, so a copy saved before a restart still counts. Providers such as CelesTrak
    # block clients that re-download unchanged data, so restarts must not trigger fresh fetches.
    hit = _remembered(name)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        hit = _remembered(name)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            value = await loader()
            value = {**value, "status": "live", "fetched_at": _now()}
            _cache[name] = (time.time(), value)
            try:
                path = _disk(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"saved_at": time.time(), "value": value}))
                temporary.replace(path)
            except OSError:
                pass
            return value
        except Exception as exc:  # serve the last good copy, clearly marked
            if hit:
                return {**hit[1], "status": "stale", "error": "Provider unavailable; showing the last successful update."}
            raise FeedError(f"{name} feed is temporarily unavailable.") from None


async def _get(url: str, params: dict | None = None, timeout: float = 30) -> httpx.Response:
    return await request(provider_for(url), "GET", url, params=params, timeout=min(timeout, 18), follow_redirects=True)


def _fleet_role(name: str) -> str:
    name = re.sub(r"[ _]+", "-", name.upper())
    if "NISAR" in name or "SENTINEL-1" in name or any(part in name for part in ("RADARSAT", "SAOCOM", "ALOS", "ICEYE", "CAPELLA", "TERRASAR", "TANDEM-X", "COSMO-SKYMED")):
        return "Radar imaging"
    if "SENTINEL-2" in name or "LANDSAT" in name:
        return "Optical land imaging"
    if "ICESAT" in name:
        return "Laser altimetry"
    if "SWOT" in name or "JASON" in name or "SENTINEL-6" in name:
        return "Water-surface altimetry"
    if "SMAP" in name:
        return "Soil moisture"
    if name in {"TERRA", "AQUA"} or name.startswith(("TERRA-(", "AQUA-(")):
        return "Multispectral Earth observations"
    if "SENTINEL-3" in name:
        return "Ocean and land observations"
    if "NOAA" in name or "SUOMI" in name or "JPSS" in name:
        return "Weather and environmental observations"
    return "Earth observation"


def _parse_tle(text: str) -> list[dict]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    out = []
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        if not (l1.startswith("1 ") and l2.startswith("2 ") and l1[2:7].isdigit() and l2[2:7] == l1[2:7]):
            continue
        try:
            year, day = int(l1[18:20]), float(l1[20:32])
            year += 2000 if year < 57 else 1900
            epoch = (datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day-1)).isoformat()
        except (ValueError, OverflowError):
            continue
        out.append({"name": name, "norad": int(l1[2:7]), "line1": l1, "line2": l2,
                    "role": _fleet_role(name), "element_epoch": epoch})
    return out


# A bounded set of established Earth-observing mission IDs. Names and elements
# always come from the provider; absent records are omitted, never synthesized.
MIRROR_FLEET_IDS = (39634, 40697, 42063, 39084, 49260, 25994, 27424, 37849,
                    43013, 54234, 43688, 54754, 40376, 41335)


async def _mirrored_satellite(norad: int) -> dict | None:
    try:
        response = await _get(f"https://tle.ivanstanojevic.me/api/tle/{norad}", timeout=5)
        mirror = response.json()
        elements = _parse_tle("\n".join([mirror.get("name", ""), mirror["line1"], mirror["line2"]]))
        return elements[0] if elements and elements[0]["norad"] == norad else None
    except Exception:
        return None


async def satellites() -> dict:
    """Real element sets plus sensor roles; no claim of operational mission status."""
    async def load() -> dict:
        nisar, fleet = await asyncio.gather(
            _get("https://celestrak.org/NORAD/elements/gp.php", {"CATNR": "65053", "FORMAT": "TLE"}),
            _get("https://celestrak.org/NORAD/elements/gp.php", {"GROUP": "resource", "FORMAT": "TLE"}),
            return_exceptions=True,
        )
        ok = lambda response: not isinstance(response, BaseException) and response.status_code == 200
        primary = _parse_tle(nisar.text) if ok(nisar) else []
        primary = [item for item in primary if item["norad"] == 65053]
        source = "CelesTrak GP (NORAD 65053)"
        if not primary:
            record = await _mirrored_satellite(65053)
            primary = [record] if record else []
            source = "TLE API mirror of CelesTrak (NORAD 65053)"
        if not primary:
            raise FeedError("No NISAR orbital elements are available")
        others = [item for item in _parse_tle(fleet.text) if item["norad"] != 65053] if ok(fleet) else []
        fleet_source = "CelesTrak Earth-resources group"
        missing = []
        if not others:
            semaphore = asyncio.Semaphore(4)
            async def one(norad):
                async with semaphore:
                    return await _mirrored_satellite(norad)
            mirrored = await asyncio.gather(*(one(norad) for norad in MIRROR_FLEET_IDS))
            others = [record for record in mirrored if record]
            missing = [norad for norad, record in zip(MIRROR_FLEET_IDS, mirrored) if not record]
            fleet_source = "TLE API mirror of CelesTrak · selected Earth-observing missions"
        return {"source": source, "fleet_source": fleet_source, "nisar": primary[0], "fleet": others,
                "unavailable_fleet_ids": missing,
                "note": "Positions are propagated from dated orbital elements. Sensor roles describe missions, not operational status or a confirmed acquisition over your region."}
    result = await _cached("satellites_v2", 6 * 3600, load)
    # Roles are local mission descriptions; updating them does not re-fetch TLEs.
    return {**result, "nisar": {**result["nisar"], "role": _fleet_role(result["nisar"]["name"])},
            "fleet": [{**item, "role": _fleet_role(item["name"])} for item in result["fleet"]]}


async def earthquakes() -> dict:
    async def load() -> dict:
        data = (await _get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_week.geojson")).json()
        items = []
        for f in data.get("features", []):
            p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
            if len(g) < 2 or p.get("mag") is None:
                continue
            items.append({"id": f.get("id"), "mag": round(float(p["mag"]), 1), "place": p.get("place") or "Unlocated event",
                          "time": p.get("time"), "lon": g[0], "lat": g[1], "depth": g[2] if len(g) > 2 else None,
                          "url": p.get("url"), "tsunami": bool(p.get("tsunami")), "alert": p.get("alert")})
        items.sort(key=lambda q: q["time"] or 0, reverse=True)
        return {"source": "USGS Earthquake Hazards Program · M2.5+ past 7 days", "items": items}
    return await _cached("earthquakes", 300, load)


def _event_point(geometry: list[dict]) -> tuple[float, float, str] | None:
    for geo in reversed(geometry or []):
        coords, kind = geo.get("coordinates"), geo.get("type")
        if kind == "Point" and coords and len(coords) >= 2:
            return float(coords[0]), float(coords[1]), geo.get("date") or ""
        if kind == "Polygon" and coords and coords[0]:
            ring = coords[0]
            return sum(c[0] for c in ring) / len(ring), sum(c[1] for c in ring) / len(ring), geo.get("date") or ""
    return None


async def eonet() -> dict:
    async def load() -> dict:
        data = (await _get("https://eonet.gsfc.nasa.gov/api/v3/events", {"status": "open", "days": 45})).json()
        items = []
        for e in data.get("events", []):
            point = _event_point(e.get("geometry") or [])
            if not point:
                continue
            cats = e.get("categories") or [{}]
            items.append({"id": e.get("id"), "title": e.get("title"), "category": cats[0].get("id"), "category_title": cats[0].get("title"),
                          "lon": point[0], "lat": point[1], "date": point[2], "provider": "NASA EONET", "current": True,
                          "source": ((e.get("sources") or [{}])[0]).get("url")})
        return {"source": "NASA EONET v3 · open events, past 45 days", "items": items}
    return await _cached("eonet", 900, load)


# GDACS event codes: tropical cyclones, floods, volcanoes, droughts and (orange/red only) earthquakes.
GDACS_TYPES = {"TC": ("severeStorms", "Tropical cyclone"), "FL": ("floods", "Flood"), "VO": ("volcanoes", "Volcano"),
               "DR": ("drought", "Drought"), "EQ": ("earthquakes", "Earthquake")}


async def gdacs() -> dict:
    """UN/EC Global Disaster Alert and Coordination System: current and recent alerts with levels."""
    async def load() -> dict:
        now = datetime.now(timezone.utc)
        start, end = (now - timedelta(days=60)).strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")
        recent = (now - timedelta(days=21)).strftime("%Y-%m-%d")

        async def one(code: str) -> list[dict]:
            levels = "orange;red" if code == "EQ" else "green;orange;red"  # USGS already lists smaller earthquakes
            response = await _get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
                                  {"eventlist": code, "fromDate": start, "toDate": end, "alertlevel": levels})
            return response.json().get("features", []) if response.content else []

        results = await asyncio.gather(*(one(code) for code in GDACS_TYPES), return_exceptions=True)
        items, failed = [], []
        for code, features in zip(GDACS_TYPES, results):
            if isinstance(features, BaseException):
                failed.append(GDACS_TYPES[code][1])
                continue
            category, label = GDACS_TYPES[code]
            for feature in features:
                p, g = feature.get("properties") or {}, (feature.get("geometry") or {}).get("coordinates") or []
                if len(g) < 2:
                    continue
                last = p.get("todate") or p.get("fromdate") or ""
                current = str(p.get("iscurrent")).lower() == "true"
                if not current and last[:10] < recent:
                    continue
                severity = (p.get("severitydata") or {}).get("severitytext")
                if isinstance(severity, str) and re.fullmatch(r"\s*Magnitude\s+0(\.0+)?\s*", severity):
                    severity = None  # GDACS floods carry a placeholder magnitude of 0
                title = re.sub(r"\s+,", ",", re.sub(r"(,\s*)+,", ",", re.sub(r"\s+", " ", p.get("name") or label))).strip(" ,")
                items.append({"id": f"gdacs-{code}-{p.get('eventid')}", "title": title,
                              "category": category, "category_title": label, "lon": float(g[0]), "lat": float(g[1]),
                              "date": last, "started": p.get("fromdate"), "alert": (p.get("alertlevel") or "").lower() or None,
                              "severity": severity.strip() if isinstance(severity, str) else None, "country": p.get("country"),
                              "current": current, "provider": "GDACS", "source": (p.get("url") or {}).get("report")})
        if failed and not items:
            raise FeedError("GDACS is temporarily unavailable.")
        return {"source": "GDACS (United Nations / European Commission) · current and recent alerts", "items": items, "partial": failed}
    return await _cached("gdacs", 900, load)


async def volcanoes() -> dict:
    """USGS volcano observatories: volcanoes at elevated alert levels, with the latest notice."""
    async def load() -> dict:
        data = (await _get("https://volcanoes.usgs.gov/hans-public/api/volcano/getCapElevated")).json()
        items = []
        for v in data if isinstance(data, list) else []:
            try:
                lat, lon = float(v["latitude"]), float(v["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            color, level = (v.get("color_code") or "").lower(), (v.get("alert_level") or "").title()
            items.append({"id": f"usgs-volcano-{v.get('vnum')}", "title": v.get("volcano_name_appended") or "Volcano",
                          "category": "volcanoes", "category_title": "Volcano", "lon": lon, "lat": lat,
                          "date": v.get("sent_date_cap") or "", "alert": color if color in {"yellow", "orange", "red"} else "green",
                          "severity": f"Volcano alert level {level} · aviation colour code {color.title()}" if level else None,
                          "summary": v.get("synopsis"), "observatory": v.get("obs_fullname"), "current": True,
                          "provider": "USGS", "source": v.get("notice_url")})
        return {"source": "USGS volcano observatories · elevated alert levels", "items": items}
    return await _cached("volcanoes", 900, load)


def _merge_hazards(groups: list[list[dict]]) -> list[dict]:
    """One record per hazard: alerts with levels (USGS, GDACS) win over the same event from EONET."""
    merged: list[dict] = []
    for group in groups:
        for item in group:
            twin = next((m for m in merged if m["category"] == item["category"] and _km(m["lat"], m["lon"], item["lat"], item["lon"]) < 60), None)
            if twin is None:
                merged.append({**item, "also": []})
            elif item.get("source") and item["source"] != twin.get("source"):
                twin["also"].append({"provider": item.get("provider"), "url": item["source"]})
    rank = {"red": 0, "orange": 1, "yellow": 2, "green": 3}
    return sorted(merged, key=lambda e: (not e.get("current"), rank.get(e.get("alert"), 4), _iso_key(e.get("date"))))


def _iso_key(value: str | None) -> float:
    try:
        return -datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return 6371 * math.acos(max(-1.0, min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl))))


async def events() -> dict:
    """Natural hazards from three independent public sources, deduplicated; a failed source is named, not hidden."""
    names = ("USGS volcanoes", "GDACS", "NASA EONET")
    results = await asyncio.gather(volcanoes(), gdacs(), eonet(), return_exceptions=True)
    groups, missing, stale = [], [], False
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            missing.append(name)
            continue
        groups.append(result["items"])
        stale = stale or result.get("status") == "stale"
    if not groups:
        raise FeedError("Hazard feeds are temporarily unavailable.")
    return {"source": "USGS volcano observatories · GDACS · NASA EONET", "items": _merge_hazards(groups),
            "status": "stale" if stale else "live", "fetched_at": _now(), "unavailable": missing}


async def fires() -> dict:
    async def load() -> dict:
        text = (await _get("https://firms.modaps.eosdis.nasa.gov/data/active_fire/modis-c6.1/csv/MODIS_C6_1_Global_24h.csv", timeout=60)).text
        rows = []
        for r in csv.DictReader(io.StringIO(text)):
            try:
                rows.append([round(float(r["longitude"]), 4), round(float(r["latitude"]), 4), round(float(r.get("frp") or 0), 1),
                             int(float(r.get("confidence") or 0)), f'{r.get("acq_date", "")} {r.get("acq_time", "")}'])
            except (KeyError, ValueError):
                continue
        return {"source": "NASA FIRMS · MODIS C6.1 active fires, past 24 h", "fields": ["lon", "lat", "frp_mw", "confidence", "acquired"], "items": rows}
    return await _cached("fires", 1800, load)


async def nisar_recent(days: int = 4) -> dict:
    """Recent NISAR GCOV frames worldwide, from the ASF catalog (footprints as published)."""
    async def load() -> dict:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = (await _get("https://api.daac.asf.alaska.edu/services/search/param", {
            "dataset": "NISAR", "processingLevel": "GCOV", "output": "geojson", "maxResults": 600, "start": start}, timeout=60)).json()
        items = []
        for f in data.get("features", []):
            p, g = f.get("properties") or {}, f.get("geometry") or {}
            if g.get("type") != "Polygon" or not g.get("coordinates"):
                continue
            items.append({"id": p.get("sceneName") or p.get("fileID"), "start": p.get("startTime"), "direction": p.get("flightDirection"),
                          "track": p.get("pathNumber"), "frame": p.get("frameNumber"), "config": p.get("productionConfiguration"),
                          "ring": [[round(c[0], 4), round(c[1], 4)] for c in g["coordinates"][0]]})
        return {"source": f"NASA/ASF catalog · NISAR GCOV, past {days} days", "items": items}
    return await _cached(f"nisar_recent_{days}", 1200, load)


async def geocode(query: str) -> dict:
    query = query.strip()
    if not 2 <= len(query) <= 120:
        raise ValueError("Search for a place using 2–120 characters.")
    data = (await _get("https://photon.komoot.io/api/", {"q": query, "limit": 15, "lang": "en"}, timeout=15)).json()
    items, seen = [], set()
    for index, f in enumerate(data.get("features", [])):
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        if len(g) < 2:
            continue
        # Photon also returns shops, roads and amenities that share a name; places and landforms are what people mean.
        if p.get("osm_key") in SKIP_KEYS or p.get("osm_value") in SKIP_VALUES:
            continue
        kind = p.get("osm_value") or p.get("type") or "place"
        parts: list[str] = []
        for part in (p.get("state"), p.get("country")):
            if part and part != p.get("name") and not any(part in earlier for earlier in parts):
                parts.append(part)
        context = ", ".join(parts)
        key = (p.get("name"), context)
        if key in seen:
            continue
        seen.add(key)
        extent = p.get("extent")  # [west, north, east, south]
        bbox = [extent[0], extent[3], extent[2], extent[1]] if extent and len(extent) == 4 else None
        items.append({"name": p.get("name") or context or "Unnamed place", "context": context, "kind": KIND_LABELS.get(kind, kind.replace("_", " ")),
                      "lon": g[0], "lat": g[1], "bbox": bbox, "_rank": (KIND_RANK.get(kind, 5), index)})
    items.sort(key=lambda item: item.pop("_rank"))
    return {"source": "Photon geocoder · OpenStreetMap data", "items": items[:8]}


SKIP_KEYS = {"shop", "amenity", "highway", "craft", "office", "healthcare", "building", "railway", "public_transport",
             "emergency", "club", "aeroway", "power", "barrier", "advertising", "telecom"}

SKIP_VALUES = {"artwork", "hotel", "motel", "hostel", "guest_house", "apartment", "industrial", "commercial", "retail", "information",
               "museum", "gallery", "camp_site", "caravan_site", "picnic_site", "chalet", "house", "residential", "garages", "construction"}

# Real places first: a capital, a volcano or a glacier outranks a same-named street or district.
KIND_RANK = {"country": 0, "state": 1, "city": 1, "volcano": 1, "glacier": 1, "island": 2, "peak": 2, "town": 2, "lake": 2, "region": 2,
             "desert": 2, "wetland": 2, "bay": 3, "river": 3, "county": 3, "village": 3, "municipality": 3, "national_park": 2,
             "nature_reserve": 2, "protected_area": 2, "forest": 3, "suburb": 4, "hamlet": 4}
KIND_LABELS = {"city": "City", "town": "Town", "village": "Village", "country": "Country", "state": "State or province", "volcano": "Volcano",
               "glacier": "Glacier", "peak": "Mountain", "island": "Island", "lake": "Lake", "river": "River", "county": "County",
               "national_park": "National park", "nature_reserve": "Nature reserve", "protected_area": "Protected area", "wetland": "Wetland",
               "desert": "Desert", "bay": "Bay", "region": "Region", "suburb": "Neighbourhood", "hamlet": "Hamlet", "administrative": "Administrative area"}


FEEDS: dict[str, Callable[[], Awaitable[dict]]] = {
    "satellites": satellites, "earthquakes": earthquakes, "events": events, "fires": fires, "nisar": nisar_recent,
}


async def keep_warm(interval: float = 240) -> None:
    """Refresh every feed in the background so investigations never wait on a cold provider."""
    while True:
        await asyncio.gather(*(loader() for loader in FEEDS.values() if loader is not satellites), return_exceptions=True)
        await asyncio.sleep(interval)


def feed_summary() -> dict[str, Any]:
    return {name: {"status": value[1].get("status"), "fetched_at": value[1].get("fetched_at")} for name, value in _cache.items()}
