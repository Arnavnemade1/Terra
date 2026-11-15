"""The tile gateway's cache: kept only as long as the provider allows, on disk, revalidated, never for private tiles."""
import asyncio
import time

import httpx
import pytest
from fastapi import HTTPException

from terra import gateway
from terra.connector import ConnectorError
from terra.fleet import warm


class Upstream:
    """A fake provider: answers with the given headers, honours If-None-Match, counts requests."""

    def __init__(self, headers=None, body=b"tile-bytes", etag='"v1"'):
        self.headers = {"content-type": "image/jpeg", "cache-control": "max-age=86400", **({"etag": etag} if etag else {}), **(headers or {})}
        self.body, self.calls, self.conditional, self.down = body, 0, 0, False

    async def __call__(self, provider, method, url, *, params=None, headers=None, timeout=20, max_bytes=0, **_):
        self.calls += 1
        if self.down:
            raise ConnectorError(provider, "request timed out or the service is unreachable.")
        if headers and headers.get("If-None-Match") == self.headers.get("etag"):
            self.conditional += 1
            return httpx.Response(304, headers=self.headers)
        return httpx.Response(200, content=self.body, headers=self.headers)


@pytest.fixture
def upstream(monkeypatch, tmp_path):
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))
    from terra.config import get_settings
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    monkeypatch.setattr(gateway, "_directory", lambda: tmp_path / "tiles")
    gateway._memory.clear(); gateway._memory_bytes = 0
    fake = Upstream()
    monkeypatch.setattr(gateway, "request", fake)
    return fake


def run(coro):
    return asyncio.run(coro)


def test_tiles_are_served_from_memory_then_disk_without_asking_again(upstream):
    entry, state = run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/1"))
    assert state == "miss" and entry.content == b"tile-bytes" and upstream.calls == 1
    assert entry.expires == pytest.approx(time.time() + 86400, abs=5)  # exactly the provider's max-age
    assert run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/1"))[1] == "hit"
    gateway._memory.clear()  # a restarted server still has the tile on disk
    assert run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/1"))[1] == "disk" and upstream.calls == 1


def test_expired_tiles_are_revalidated_with_their_etag(upstream):
    entry, _ = run(gateway.fetch("terrain", "https://elevation3d.arcgis.com/t/2"))
    entry.expires = time.time() - 1; gateway._write_disk("https://elevation3d.arcgis.com/t/2[]", entry)
    again, state = run(gateway.fetch("terrain", "https://elevation3d.arcgis.com/t/2"))
    assert state == "revalidated" and upstream.conditional == 1 and again.content == b"tile-bytes" and again.fresh()


def test_private_or_no_store_tiles_are_never_kept(upstream):
    upstream.headers["cache-control"] = "private, max-age=3600"
    run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/3"))
    run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/3"))
    assert upstream.calls == 2 and not list((gateway._directory()).glob("*/*.tile"))


def test_an_expired_copy_is_served_when_the_provider_is_down_and_errors_otherwise(upstream):
    entry, _ = run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/4"))
    entry.expires = time.time() - 1
    upstream.down = True
    assert run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/4"))[1] == "stale"
    with pytest.raises(HTTPException) as failure:
        run(gateway.fetch("imagery", "https://services.arcgisonline.com/t/never-seen"))
    assert failure.value.status_code == 502


def test_disk_cache_stays_under_its_cap(upstream, monkeypatch):
    for i in range(12):
        run(gateway.fetch("imagery", f"https://services.arcgisonline.com/t/cap/{i}"))
    one = next(gateway._directory().glob("*/*.tile")).stat().st_size
    monkeypatch.setattr(gateway, "DISK_BYTES", 6 * one)
    gateway._evict_disk()
    kept = list(gateway._directory().glob("*/*.tile"))
    assert 0 < len(kept) <= 6


def test_browser_is_told_the_remaining_lifetime(upstream):
    response = run(gateway._asset("imagery", "https://services.arcgisonline.com/t/5"))
    max_age = int(response.headers["cache-control"].split("max-age=")[1])
    assert 86000 < max_age <= 86400 and response.headers["x-terra-cache"] == "miss"


def test_warmer_plans_the_area_once_and_the_ground_each_camera_sees():
    area = {"bbox": [15.0, 37.7, 15.1, 37.8], "launch": [15.05, 37.75]}
    drone = {"link_state": "online", "state": {"lon": 15.05, "lat": 37.75, "agl_m": 120, "heading_deg": 90, "gimbal": {"heading_deg": 90, "pitch_deg": -30}}}
    first = warm.plan(area, [drone], area_done=False)
    later = warm.plan(area, [drone], area_done=True)
    assert len(first) <= warm.AREA_BUDGET + len(later) and {t[0] for t in first} >= {"terrain", "imagery", "hillshade", "labels"}
    assert later and all(z in warm.DRONE_IMAGERY or z in warm.DRONE_TERRAIN for _, z, _, _ in later)
    # A close camera needs level 19 imagery, and must not wait behind the overview batch.
    assert any(layer == "imagery" and z == 19 for layer, z, _, _ in later)
    assert any(layer == "terrain" and z == 17 for layer, z, _, _ in later)
    assert first[:len(later)] == later
    # The camera looks east: the aim point's tiles lie east of the aircraft's own.
    z = 17; own_x, _ = warm.tile(15.05, 37.75, z)
    assert max(x for layer, zz, x, _ in later if zz == z and layer == "imagery") > own_x
    assert warm.plan(area, [{**drone, "state": {**drone["state"], "landed": True}}], area_done=True) == []


def test_tile_math_matches_web_mercator():
    assert warm.tile(0.0, 0.0, 1) == (1, 1) and warm.tile(-180.0, 85.0, 3) == (0, 0)
    assert warm.tile(15.05, 37.75, 12) == (2219, 1583)  # Etna: Esri's tile/12/1583/2219
