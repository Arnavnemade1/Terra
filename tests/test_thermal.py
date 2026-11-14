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
