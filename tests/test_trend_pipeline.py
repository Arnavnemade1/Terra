"""Trend pipeline: grid alignment, tiling, tile fitting and publishing on a synthetic stack, and the API guards.

The pipeline reads real GCOV products, so the acquisition side is not exercised here. The grid alignment and the
tiling planner are tested as the pure functions they are, and the tile and publish activities are run end to end
on a synthetic quantized stack written in the same layout the build activity produces, with a known slope.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from numpy.lib.format import open_memmap
import pytest

from terra.trend_activities import LIMITATIONS, align, fit_tile, plan_tiles, publish_trends
from terra.trends import quantize, quantization_error_db

TRANSFORM = [100.0, 0.0, 300000.0, 0.0, -100.0, 4000000.0]


def test_align_shifts_crops_and_pads_on_the_reference_grid():
    source = np.arange(6, dtype=np.float32).reshape(2, 3) + 1.0
    moved = [100.0, 0.0, 300100.0, 0.0, -100.0, 3999900.0]  # one column east, one row south
    placed = align(source, moved, TRANSFORM, (3, 5))
    assert placed.shape == (3, 5)
    assert np.array_equal(placed[1:3, 1:4], source)
    assert np.isnan(placed[0]).all() and np.isnan(placed[:, 0]).all()

    cropped = align(source, TRANSFORM, TRANSFORM, (2, 2))
    assert np.array_equal(cropped, source[:, :2])

    codes = align(np.ones((2, 2), dtype=np.uint8), moved, TRANSFORM, (3, 5))
    assert codes.dtype == np.uint8 and codes[0, 0] == 0 and codes[1, 1] == 1  # no data, never NaN, in codes

    far = [100.0, 0.0, 400000.0, 0.0, -100.0, 4000000.0]
    assert np.isnan(align(source, far, TRANSFORM, (3, 5))).all()


def test_plan_tiles_covers_the_grid_within_the_agent_budget():
    tiles = plan_tiles((2000, 2000))
    assert 1 <= len(tiles) <= 16
    assert [tile["index"] for tile in tiles] == list(range(len(tiles)))
    assert sum((t["rows"][1] - t["rows"][0]) * (t["cols"][1] - t["cols"][0]) for t in tiles) == 2000 * 2000
    assert all((t["rows"][1] - t["rows"][0]) * (t["cols"][1] - t["cols"][0]) <= 250_000 for t in tiles)

    assert plan_tiles((380, 380)) == [{"index": 0, "rows": [0, 380], "cols": [0, 380]}]
    tall = plan_tiles((3000, 10))
    assert 1 <= len(tall) <= 16
    assert sum((t["rows"][1] - t["rows"][0]) * (t["cols"][1] - t["cols"][0]) for t in tall) == 30_000
    with pytest.raises(ValueError):
        plan_tiles((0, 10))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))
    (tmp_path / "trends").mkdir()
    return tmp_path


def _write_stack(directory: Path, truth: float = -1.2, shape=(40, 50), dates: int = 6, seed: int = 5) -> dict:
    """A synthetic stack in the build activity's layout, with a known straight-line slope in dB/year."""
    rng = np.random.default_rng(seed)
    height, width = shape
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    stamps = [start + timedelta(days=30 * index) for index in range(dates)]
    span = np.array([(stamp - stamps[0]).days / 365.25 for stamp in stamps], dtype=np.float64)
    values = truth * span[:, None, None] - 8.0 + rng.normal(0.0, 0.4, (dates, height, width))
    stack = open_memmap(directory / "stack.u8", mode="w+", dtype=np.uint8, shape=(dates, height, width))
    stack[:] = quantize(values)
    stack.flush()
    del stack
    np.savez(directory / "sample.npz", values=values.reshape(dates, -1).astype(np.float32),
             rows=np.repeat(np.arange(height), width), columns=np.tile(np.arange(width), height),
             dates=np.array([stamp.isoformat() for stamp in stamps]))
    meta = {
        "id": directory.name, "bbox": [0.0, 0.0, 0.01, 0.01],
        "dates": [stamp.isoformat() for stamp in stamps], "t_years": [float(round(x, 6)) for x in span],
        "transform": TRANSFORM, "crs": "EPSG:32633", "shape": [height, width],
        "pixel_spacing": [100.0, 100.0], "pixel_km2": 0.01, "dropped": [],
        "bytes": {"uint8": dates * height * width, "float32": 4 * dates * height * width},
        "quantization": quantization_error_db(),
    }
    (directory / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def test_tile_agents_and_publish_recover_a_known_slope(data_dir):
    identifier = "trend-0123456789ab"
    directory = data_dir / "trends" / identifier
    directory.mkdir()
    meta = _write_stack(directory)
    tiles = plan_tiles(tuple(meta["shape"]), max_pixels=800)
    assert len(tiles) == 3  # a small budget really does split the area across agents

    results = [fit_tile({"id": identifier, "tile": tile}) for tile in tiles]
    assert [result["index"] for result in results] == [0, 1, 2]
    assert sum(result["pixels_fit"] for result in results) == 40 * 50
    assert all(result["cv_mae_db"] for result in results)

    plan = {
        "ok": True,
        "group": {"track": 1, "direction": "ASCENDING", "frame": 10, "maturity": "VALIDATED",
                  "product_version": "1.0", "candidates": 8},
        "qubo": {"energy": -12.5, "brute_force_optimum_energy": -12.5,
                 "solver": "classical simulated annealing", "chosen": list(range(6))},
    }
    stack = {"ok": True, "shape": meta["shape"], "dates": meta["dates"], "tiles": tiles, "dropped": [],
             "bytes": meta["bytes"]}
    published = publish_trends({"id": identifier, "tiles": results, "plan": plan, "stack": stack})
    assert published["ok"] is True
    for name in ("trend", "trend-significant"):
        assert (directory / f"{name}.tif").is_file() and (directory / f"{name}.png").is_file()

    text = (directory / "result.json").read_text()
    document = json.loads(text, parse_constant=lambda token: pytest.fail(f"non-finite {token} in result.json"))
    assert document["summary"]["median_slope_db_per_year"] == pytest.approx(-1.2, abs=0.2)
    assert document["summary"]["pixels"] == 40 * 50
    assert document["qubo"] == {"energy": -12.5, "optimum_energy": -12.5,
                                "solver": "classical simulated annealing", "chosen": 6, "candidates": 8}
    assert len(document["agents"]) == 3
    assert sum(agent["pixels_fit"] for agent in document["agents"]) == 40 * 50
    assert document["quantized_dataset"]["bytes_uint8"] == 6 * 40 * 50
    assert document["quantized_dataset"]["bytes_float32"] == 4 * 6 * 40 * 50
    effect = document["quantized_dataset"]["effect"]
    assert effect["pixels"] == 40 * 50
    assert 0.0 <= effect["median_abs_slope_diff_db_per_year"] < 0.25
    assert [layer["id"] for layer in document["layers"]] == ["trend", "trend-significant"]
    assert document["layers"][0]["units"] == "dB/yr"
    assert document["layers"][1]["min"] == -3.0 and document["layers"][1]["max"] == 3.0
    assert document["limitations"] == LIMITATIONS
    assert any("straight-line" in item for item in document["limitations"])
    assert any("classical simulated annealing" in item for item in document["limitations"])


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from terra.trends_api import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_rejects_bad_identifiers_and_layer_names(api):
    assert api.get("/api/trends/trend-XYZ").status_code == 422
    assert api.get("/api/trends/not-a-trend").status_code == 422
    assert api.get("/api/trends/trend-0123456789ab/files/notes.txt").status_code == 404
    assert api.get("/api/trends/trend-0123456789ab/files/trend.gif").status_code == 404
    assert api.get("/api/trends/trend-0123456789ab/files/trend.png").status_code == 404  # nothing written yet


def test_api_rejects_oversized_and_invalid_requests(api):
    assert api.post("/api/trends", json={"name": "wide", "bbox": [0, 0, 1, 1]}).status_code == 422
    assert api.post("/api/trends", json={"name": "tall", "bbox": [0, 0, 0.1, 0.9]}).status_code == 422
    assert api.post("/api/trends", json={"name": "many", "bbox": [0, 0, 0.2, 0.2], "max_dates": 20}).status_code == 422
    assert api.post("/api/trends", json={"name": "few", "bbox": [0, 0, 0.2, 0.2], "max_dates": 2}).status_code == 422
    assert api.post("/api/trends", json={"name": "", "bbox": [0, 0, 0.2, 0.2]}).status_code == 422


def test_api_serves_only_the_two_layer_files(api, tmp_path):
    directory = tmp_path / "trends" / "trend-0123456789ab"
    directory.mkdir(parents=True)
    (directory / "trend.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (directory / "trend-significant.tif").write_bytes(b"II*\x00")
    (directory / "result.json").write_text("{}")
    response = api.get("/api/trends/trend-0123456789ab/files/trend.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    significant = api.get("/api/trends/trend-0123456789ab/files/trend-significant.tif")
    assert significant.status_code == 200 and significant.headers["content-type"] == "image/tiff"
    assert api.get("/api/trends/trend-0123456789ab/files/result.json").status_code == 404
    assert api.get("/api/trends/trend-0123456789ab/files/TREND.png").status_code == 404


def test_unknown_analysis_is_not_found(api):
    assert api.get("/api/trends/trend-000000000000").status_code == 404


def test_every_trend_activity_is_async_so_the_worker_can_start():
    """The worker has no thread-pool activity executor; a synchronous activity stops it at startup."""
    import inspect
    from terra.trend_activities import ACTIVITIES
    assert ACTIVITIES and all(inspect.iscoroutinefunction(a) for a in ACTIVITIES)


def test_multilook_is_seamless_across_tile_edges():
    """Averaging a tile with its halo gives the same values as averaging the whole grid and cutting the tile out."""
    import numpy as np
    from terra.trend_activities import multilook_block
    from terra.trends import quantize
    rng = np.random.default_rng(3)
    db = rng.normal(-12, 2, (5, 40, 50)).astype(np.float32)
    db[:, 10:12, 20:23] = np.nan
    stack = quantize(db)
    whole = multilook_block(stack, [0, 40], [0, 50])
    tile = multilook_block(stack, [13, 27], [9, 31])
    assert np.allclose(tile, whole[:, 13:27, 9:31], equal_nan=True)
    # Speckle-like noise drops by about the square root of nine.
    assert np.nanstd(whole[:, 2:-2, 2:-2]) < 0.5 * np.nanstd(db)
