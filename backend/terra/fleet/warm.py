"""Keeps the map tiles around the fleet warm in the server's tile cache.

The workspace's globe and every drone camera draw terrain and imagery through the same-origin gateway. A camera
100 m above the ground needs tiles 15-17 levels deep, and Cesium can only refine a place after its parent terrain
has arrived, so a cold camera waits on a chain of provider round trips. This warmer fetches, ahead of time and
through the same cache the browser uses: the monitoring area once (overview to working zoom levels), and around
every airborne aircraft and the ground its camera looks at (camera zoom levels). Tiles are kept only as long as the
providers' own Cache-Control allows; nothing is fetched twice while it is fresh.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time

log = logging.getLogger(__name__)

INTERVAL_S = 6.0
CONCURRENCY = 6
AREA_BUDGET = 900                 # tiles per monitoring area
AREA_IMAGERY = range(9, 15)       # overview to working views of the area
AREA_TERRAIN = range(5, 13)
DRONE_IMAGERY = (14, 15, 16, 17)  # what a camera 50-500 m above the ground draws near its aim point
DRONE_TERRAIN = (12, 13, 14, 15)
SEEN_S = 6 * 3600


def tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Web Mercator tile (x, y) holding a point (the scheme of both Esri imagery and the Terrain3D service)."""
    lat = max(-85.0511, min(85.0511, lat)); n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def box_tiles(bbox: list[float], z: int, limit: int) -> list[tuple[int, int, int]]:
    (x0, y1), (x1, y0) = tile(bbox[0], bbox[1], z), tile(bbox[2], bbox[3], z)
    out = [(z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    return out[:limit]


def around(lon: float, lat: float, z: int, radius: int = 1) -> list[tuple[int, int, int]]:
    cx, cy = tile(lon, lat, z); n = 2 ** z
    return [(z, (cx + dx) % n, cy + dy) for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1) if 0 <= cy + dy < n]


def plan(area: dict | None, drones: list[dict], area_done: bool) -> list[tuple[str, int, int, int]]:
    """(layer, z, x, y) to warm: the area first time it is seen, then each airborne aircraft and its aim point."""
    from .observe import ground_point
    wanted: list[tuple[str, int, int, int]] = []
    if area and not area_done:
        budget = AREA_BUDGET
        for z in AREA_TERRAIN:
            for t in box_tiles(area["bbox"], z, budget):
                wanted.append(("terrain", *t)); budget -= 1
        for z in AREA_IMAGERY:
            for layer in ("imagery", "hillshade") + (("labels",) if z <= 13 else ()):
                for t in box_tiles(area["bbox"], z, max(0, budget)):
                    wanted.append((layer, *t)); budget -= 1
    for d in drones:
        s = d.get("state") or {}
        if d.get("link_state") != "online" or "lon" not in s or s.get("landed"):
            continue
        g = s.get("gimbal") or {}
        aim = ground_point(s["lon"], s["lat"], float(s.get("agl_m") or 100), float(g.get("heading_deg", s.get("heading_deg", 0))), float(g.get("pitch_deg", -45)))
        for lon, lat in ((s["lon"], s["lat"]), aim):
            for z in DRONE_TERRAIN:
                wanted += [("terrain", *t) for t in around(lon, lat, z)]
            for z in DRONE_IMAGERY:
                wanted += [(layer, *t) for layer in ("imagery", "hillshade") if not (layer == "hillshade" and z > 16) for t in around(lon, lat, z)]
    return list(dict.fromkeys(wanted))


class Warmer:
    def __init__(self) -> None:
        self.area_key: str | None = None
        self.seen: dict[tuple, float] = {}
        self.stats = {"rounds": 0, "fetched": 0, "failed": 0, "last_round_s": 0.0}

    async def _one(self, item: tuple[str, int, int, int], gate: asyncio.Semaphore) -> None:
        from ..gateway import TERRAIN, esri_tile, fetch
        layer, z, x, y = item
        url = f"{TERRAIN}/tile/{z}/{y}/{x}" if layer == "terrain" else esri_tile(layer, z, y, x)
        async with gate:
            try:
                await fetch("terrain" if layer == "terrain" else "imagery", url)
                self.stats["fetched"] += 1
            except Exception:  # a missing tile (ocean, beyond coverage) or a provider hiccup: try again later
                self.stats["failed"] += 1
        self.seen[item] = time.time()

    async def round(self) -> int:
        from .db import fleet_db
        started = time.monotonic()
        db = fleet_db()
        area, drones = await asyncio.to_thread(lambda: (db.area(), db.drones()))
        key = repr(area["bbox"]) if area else None
        now = time.time()
        todo = [i for i in plan(area, drones, area_done=key == self.area_key) if now - self.seen.get(i, 0) > SEEN_S]
        if todo:
            gate = asyncio.Semaphore(CONCURRENCY)
            await asyncio.gather(*(self._one(i, gate) for i in todo))
        self.area_key = key
        if len(self.seen) > 50_000:
            self.seen = {k: t for k, t in self.seen.items() if now - t < SEEN_S}
        self.stats["rounds"] += 1; self.stats["last_round_s"] = round(time.monotonic() - started, 2)
        return len(todo)

    async def run(self) -> None:
        await asyncio.sleep(2)
        while True:
            try:
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tile warming round failed")
            await asyncio.sleep(INTERVAL_S)


def enabled() -> bool:
    return os.getenv("TERRA_TILE_WARM", "1") != "0" and "PYTEST_CURRENT_TEST" not in os.environ
