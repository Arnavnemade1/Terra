"""Missing model values must never become zero rainfall or invented profiles."""
from datetime import datetime, timedelta, timezone
import importlib

import httpx
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from terra import conditions

WHEN = datetime(2026, 6, 12, 15, 24, tzinfo=timezone.utc)
AOI = [-74.8, -5.55, -74.5, -5.25]


def hourly_payload():
    end = WHEN.replace(minute=0)
    times = [end - timedelta(hours=i) for i in reversed(range(96))]
    return {"latitude": -5.5, "longitude": -74.5, "hourly": {
        "time": [stamp.isoformat() for stamp in times], "precipitation": [0.5] * 96,
        "soil_moisture_0_to_7cm": [0.25] * 96, "temperature_2m": [25] * 96,
        "wind_speed_10m": [10] * 96, "relative_humidity_2m": [80] * 96}}


def test_precipitation_requires_exactly_72_complete_hours():
    payload = hourly_payload()
    out = conditions._summarize(payload, WHEN, recent=False)
    assert out["precipitation_72h_mm"] == 36
    assert out["rain_observed_hours"] == 72
    assert out["precipitation_window_end"].endswith("15:00:00+00:00")
    assert out["model"] == "ERA5 reanalysis"
    payload["hourly"]["precipitation"][-10] = None
    out = conditions._summarize(payload, WHEN, recent=False)
    assert out["precipitation_72h_mm"] is None
    assert out["rain_72h_mm"] is None
    assert out["rain_observed_hours"] == 71
    assert out["complete"] is False


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True, "2"])
def test_invalid_precipitation_is_missing_not_counted(invalid):
    payload = hourly_payload()
    payload["hourly"]["precipitation"][-1] = invalid
    result = conditions._summarize(payload, WHEN, recent=False)
    assert result["rain_observed_hours"] == 71
    assert result["precipitation_72h_mm"] is None


def test_duplicate_time_does_not_count_as_an_extra_hour():
    payload = hourly_payload()
    payload["hourly"]["time"][-1] = payload["hourly"]["time"][-2]
    result = conditions._summarize(payload, WHEN, recent=False)
    assert result["rain_observed_hours"] == 70
    assert result["precipitation_72h_mm"] is None


async def test_conditions_failure_retains_unavailable_acquisition(monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("upstream private details")
    monkeypatch.setattr(conditions, "request", fail)
    result = await conditions.surface_conditions(AOI, [WHEN.isoformat()])
    assert result["status"] == "unavailable"
    assert result["acquisitions"] == []
    assert result["unavailable"] == [WHEN.isoformat()]
    assert "private details" not in str(result)


async def test_era5_is_explicitly_requested_and_units_are_fixed(monkeypatch):
    calls = []
    async def request(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(200, json=hourly_payload())
    monkeypatch.setattr(conditions, "request", request)
    result = await conditions.surface_conditions(AOI, [WHEN.isoformat()])
    assert result["status"] == "available"
    assert calls[0][0][:3] == ("openmeteo", "GET", conditions.ARCHIVE)
    assert calls[0][1]["params"]["models"] == "era5"
    assert calls[0][1]["params"]["precipitation_unit"] == "mm"


@pytest.mark.parametrize("bbox,dates", [(AOI, []), (AOI, ["2099-01-01"]), (AOI, ["invalid"]), ([0, 0, 200, 1], ["2026-01-01"])])
async def test_conditions_rejects_invalid_requests(bbox, dates):
    with pytest.raises(ValueError):
        await conditions.surface_conditions(bbox, dates)


def test_profile_uses_even_geodesic_distance_and_preserves_ends():
    points, distances = conditions.path_points([(0, 0), (0.5, 0), (1, 0)], 5)
    assert points[0] == pytest.approx((0, 0))
    assert points[-1] == pytest.approx((1, 0))
    assert distances[-1] == pytest.approx(111.3195, abs=0.001)
    assert np.diff(distances).max() - np.diff(distances).min() < 0.001


@pytest.mark.parametrize("vertices,n", [([(0, 0), (0, 0)], 80), ([(0, 0), (10, 0)], 80), ([(179, 0), (-179, 0)], 80),
                                         ([(0, 0), (0.1, 0)], 1), ([(float("nan"), 0), (0.1, 0)], 80)])
def test_profile_path_limits(vertices, n):
    with pytest.raises(ValueError):
        conditions.path_points(vertices, n)


async def test_elevation_short_response_does_not_shift_samples(monkeypatch):
    async def request(*args, **kwargs):
        return httpx.Response(200, json={"elevation": [10]})
    monkeypatch.setattr(conditions, "request", request)
    assert await conditions.elevations([(0, 0), (0.1, 0)]) == [None, None]


def test_profile_samples_native_values_and_preserves_nodata(tmp_path):
    path = tmp_path / "change.tif"
    with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=1, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(0, 2, 1, 1), nodata=-9999) as dst:
        dst.write(np.array([[1, -9999], [3, 4]], dtype=np.float32), 1)
    layers = conditions.sample_profile_rasters(tmp_path, [(0.5, 1.5), (1.5, 1.5), (10, 10)],
                                               [{"id": "change", "path": "change.png", "units": "dB"}])
    assert layers[0]["values"] == [1.0, None, None]
    assert conditions.sample_profile_rasters(tmp_path, [(0.5, 1.5)], []) == []
