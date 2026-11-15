"""JRC water-history split: tile naming, class boundaries, the analysis mask, and the report section."""
from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from terra import ancillary
from terra.science import NEWLY_WATER_MASK, run_analysis
from tests.test_science import make_gcov, manifest


def test_tiles_are_named_by_west_and_north_edge():
    names = [u.rsplit("occurrence_", 1)[1] for u in ancillary._tile_urls([-74.5, -5.5, -74.0, -5.0])]
    assert names == ["80W_0Nv1_4_2021.tif"]
    names = {u.rsplit("occurrence_", 1)[1][:-len("v1_4_2021.tif")] for u in ancillary._tile_urls([-0.5, 49.5, 0.5, 50.5])}
    assert names == {"10W_60N", "0E_60N", "10W_50N", "0E_50N"}


def test_split_uses_the_documented_class_boundaries():
    occurrence = np.array([[0, 1, 89, 90], [100, 255, 50, 0]], dtype=np.uint8)
    mask = np.ones(occurrence.shape, dtype=bool)
    mask[0, 0] = False  # not newly water-like, so not counted
    split = ancillary.split_by_occurrence(mask, occurrence, 0.01)
    assert split["newly_water_like_pixels"] == 7
    assert (split["permanent"]["pixels"], split["seasonal"]["pixels"], split["never"]["pixels"], split["unmapped"]["pixels"]) == (2, 3, 1, 1)
    assert split["mapped_pixels"] == 6 and split["never"]["pct_of_mapped"] == round(1 / 6 * 100, 1)
    assert split["permanent"]["km2"] == 0.02


def test_analysis_writes_the_newly_water_like_mask_and_context_reads_it(tmp_path, monkeypatch):
    dry = np.full((8, 8), 0.1, dtype=np.float32)
    wet = dry.copy()
    wet[:, :4] = 0.001  # -30 dB: low return on the later date only
    first, second = make_gcov(tmp_path / "a.h5", dry), make_gcov(tmp_path / "b.h5", wet, date="2026-02-01T00:00:00Z")
    out = tmp_path / "out"
    run_analysis(manifest(tmp_path, first, second), out)
    with rasterio.open(out / NEWLY_WATER_MASK) as ds:
        assert ds.read(1).sum() > 0 and ds.transform == from_origin(500000, 10000000, 20, 20)
    seen = {}

    def fake(shape, transform, crs):
        seen["shape"] = shape
        return np.where(np.arange(shape[1]) < 2, 95, 0).astype(np.uint8)[None, :].repeat(shape[0], 0)

    monkeypatch.setattr(ancillary, "occurrence_on_grid", fake)
    context = ancillary.water_context(out / NEWLY_WATER_MASK)
    assert seen["shape"] == (8, 8) and context["status"] == "available"
    assert context["permanent"]["pixels"] + context["never"]["pixels"] == context["newly_water_like_pixels"] > 0
    text = ancillary.water_report(context)
    assert "JRC Global Surface Water" in text and "never mapped as water" in text


def test_no_new_water_writes_no_mask_and_no_report_section(tmp_path):
    same = np.full((8, 8), 0.1, dtype=np.float32)
    first, second = make_gcov(tmp_path / "a.h5", same), make_gcov(tmp_path / "b.h5", same, date="2026-02-01T00:00:00Z")
    run_analysis(manifest(tmp_path, first, second), tmp_path / "out")
    assert not (tmp_path / "out" / NEWLY_WATER_MASK).exists()
    assert ancillary.water_report(None) == "" and ancillary.water_report({"status": "none"}) == ""
