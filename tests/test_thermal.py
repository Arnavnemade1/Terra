"""Thermal scenes: cloud masking, temperature scaling and the response contract. No network access."""
import asyncio
import base64
from io import BytesIO

import numpy as np
from PIL import Image
import pytest

from terra import thermal


def _scene():
    return {"id": "LC09_TEST", "properties": {"datetime": "2026-10-29T09:42:00Z", "platform": "landsat-9", "eo:cloud_cover": 3.0},
            "assets": {"lwir11": {"href": "https://landsateuwest.blob.core.windows.net/x/ST_B10.TIF"}, "qa_pixel": {"href": "https://landsateuwest.blob.core.windows.net/x/QA.TIF"}}}


def test_render_masks_clouds_and_converts_to_celsius(monkeypatch):
    def fake_read(url, bands, bbox, transform, dtype, resampling, provider="sentinel", shape=None):
        h, w = shape
        assert provider == "landsat" and "?sig" in url
        if "ST_B10" in url:  # 20 °C everywhere, 45 °C in one corner
            kelvin = np.full((h, w), 293.15); kelvin[:5, :5] = 318.15  # noqa: E702
            return ((kelvin - 149.0) / 0.00341802).round().astype(np.uint16)[None]
        qa = np.full((h, w), 21824, dtype=np.uint16); qa[h * 3 // 4:, :] = 21824 | (1 << 3)  # bottom quarter cloudy
        return qa[None]

    monkeypatch.setattr("terra.optical._read_cog", fake_read)
    out = thermal._render(_scene(), "sig=abc", [15.0, 37.7, 15.1, 37.8])
    assert out and out["date"] == "2026-10-29" and out["platform"] == "Landsat 9"
    assert out["valid_pct"] == pytest.approx(75.0, abs=0.5)
    assert out["stats_c"]["median"] == pytest.approx(20.0, abs=0.1) and out["stats_c"]["max"] == pytest.approx(45.0, abs=0.1)
    image = np.array(Image.open(BytesIO(base64.b64decode(out["png"]))))
    assert image.shape[2] == 2 and (image[..., 1] == 0).mean() == pytest.approx(0.25, abs=0.01)  # clouds transparent
    assert "sig=abc" not in str({k: v for k, v in out.items() if k != "png"})


def test_mostly_cloudy_scene_is_skipped(monkeypatch):
    def fake_read(url, bands, bbox, transform, dtype, resampling, provider="sentinel", shape=None):
        return (np.full((1, *shape), 40000, dtype=np.uint16) if "ST_B10" in url else np.full((1, *shape), 1 << 3, dtype=np.uint16))
    monkeypatch.setattr("terra.optical._read_cog", fake_read)
    assert thermal._render(_scene(), "sig=abc", [15.0, 37.7, 15.1, 37.8]) is None


def test_thermal_reports_unavailable_without_scenes(monkeypatch, tmp_path):
    monkeypatch.setattr(thermal, "_disk", lambda key: tmp_path / f"{key}.json")
    thermal._memory.clear()

    async def no_scenes(bbox, days):
        return []

    async def token():
        return "sig=abc"

    monkeypatch.setattr(thermal, "_scenes", no_scenes); monkeypatch.setattr(thermal, "_access_token", token)
    value = asyncio.run(thermal.thermal([15.0, 37.7, 15.1, 37.8]))
    assert value["available"] is False and "cloud-free" in value["reason"]


def test_scene_edges_are_not_read_as_cold_and_cirrus_is_masked(monkeypatch):
    resampling_used = []

    def fake_read(url, bands, bbox, transform, dtype, resampling, provider="sentinel", shape=None):
        h, w = shape; resampling_used.append(resampling.name)
        if "ST_B10" in url:  # 20 °C, with the scene's fill (0) along the left edge
            band = np.full((h, w), round((293.15 - 149.0) / 0.00341802), dtype=np.uint16); band[:, :10] = 0
            return band[None]
        qa = np.full((h, w), 21824, dtype=np.uint16); qa[:, :10] = 1; qa[:20, 50:60] = 21824 | (1 << 2)  # fill; a cirrus patch
        return qa[None]

    monkeypatch.setattr("terra.optical._read_cog", fake_read)
    out = thermal._render(_scene(), "sig=abc", [15.0, 37.7, 15.1, 37.8])
    assert set(resampling_used) == {"nearest"}  # bilinear would blend fill (0) into the edge as very cold pixels
    image = np.array(Image.open(BytesIO(base64.b64decode(out["png"]))))
    assert (image[:, :11, 1] == 0).all()  # the fill and the cell next to it are transparent
    assert (image[:20, 50:60, 1] == 0).all()  # cirrus is masked
    assert out["minC"] >= 19 and out["stats_c"]["p5"] == pytest.approx(20.0, abs=0.1)  # no cold edge drags the scale


def test_areas_snap_to_a_grid_and_caches_stay_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(thermal, "_disk", lambda key: tmp_path / f"thermal-{key}.json")
    monkeypatch.setattr(thermal, "MEMORY_KEEP", 3); monkeypatch.setattr(thermal, "DISK_KEEP", 4)
    thermal._memory.clear()
    reads = []

    async def no_scenes(bbox, days):
        reads.append(bbox); return []

    async def token():
        return "sig=abc"

    monkeypatch.setattr(thermal, "_scenes", no_scenes); monkeypatch.setattr(thermal, "_access_token", token)
    assert thermal.snap([15.0012, 37.7049, 15.0951, 37.7999]) == [15.0, 37.7, 15.1, 37.8]
    asyncio.run(thermal.thermal([15.0012, 37.7049, 15.0951, 37.7999]))
    asyncio.run(thermal.thermal([15.0034, 37.7001, 15.0987, 37.7962]))  # nearby: the same snapped area, no second read
    assert len(reads) == 1
    for i in range(8):
        asyncio.run(thermal.thermal([15.0 + i * 0.05, 37.7, 15.04 + i * 0.05, 37.74]))
    assert len(thermal._memory) <= 3 and len(list(tmp_path.glob("thermal-*.json"))) <= 4


def test_unreadable_scene_is_reported_unavailable_not_a_server_error(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from rasterio.errors import RasterioIOError
    from terra import app as app_module
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))

    async def broken(bbox):
        raise RasterioIOError("truncated tile")
    monkeypatch.setattr(thermal, "thermal", broken)
    with TestClient(app_module.app, base_url="http://localhost") as client:
        reply = client.get("/api/thermal?bbox=15.0,37.7,15.1,37.8")
    assert reply.status_code == 200 and reply.json()["available"] is False
