"""Analytic synthetic fixtures are tests only, never application observations."""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from terra.science import ScienceError, check_compatibility, inspect_gcov, run_analysis


def make_gcov(path: Path, values: np.ndarray | None = None, date: str = "2026-01-01T00:00:00Z", **overrides) -> Path:
    values = np.asarray(values if values is not None else [[1.0, 0.1], [0.01, 0.001]], dtype=np.float32)
    with h5py.File(path, "w") as f:
        f.attrs["test_fixture"] = "SYNTHETIC — NOT OBSERVED EARTH DATA"
        ident = f.create_group("/science/LSAR/identification")
        fields = {"missionId": "NISAR", "productType": "GCOV", "zeroDopplerStartTime": date,
                  "trackNumber": 1, "frameNumber": 1, "orbitPassDirection": "Ascending", "lookDirection": "Right",
                  "productVersion": "1.0", "compositeReleaseId": "TEST01", "dataMaturity": "BETA"}
        fields.update(overrides)
        for key, val in fields.items():
            ident[key] = val
        grid = f.create_group("/science/LSAR/GCOV/grids/frequencyA")
        grid["HHHH"] = values
        grid["xCoordinates"] = 500010 + np.arange(values.shape[1]) * 20
        grid["yCoordinates"] = 9999990 - np.arange(values.shape[0]) * 20
        grid["xCoordinateSpacing"], grid["yCoordinateSpacing"] = 20.0, -20.0
        grid["projection"] = 32718
        grid["mask"] = np.ones(values.shape, dtype=np.uint8)
        grid["inputDataExceptionMask"] = np.zeros(values.shape, dtype=np.uint16)
    return path


def manifest(root: Path, *inputs: Path, **extra) -> dict:
    return {"input_root": str(root), "inputs": [{"path": str(p)} for p in inputs], **extra}


def test_power_db_and_masks_are_numerically_correct(tmp_path):
    path = make_gcov(tmp_path / "synthetic.h5", [[1, 0.1, 0.01], [0, -1, np.nan], [0.001, 0.01, 1]])
    with h5py.File(path, "r+") as f:
        grid = f["/science/LSAR/GCOV/grids/frequencyA"]
        grid["mask"][0, 2] = 0
        grid["mask"][2, 0] = 255
        grid["inputDataExceptionMask"][2, 2] = 1
    result = run_analysis(manifest(tmp_path, path), tmp_path / "out")
    stats = result["measurements"]["observations"][0]
    assert stats["valid_pixels"] == 3
    assert stats["median_db"] == pytest.approx(-10)
    assert stats["mean_db"] == pytest.approx(-10)
    assert stats["valid_projected_area_km2"] == pytest.approx(0.0012)
    assert result["validation"]["independent_validation"] == "not_performed"
    with rasterio.open(tmp_path / "out/backscatter-1.tif") as ds:
        assert ds.crs.to_epsg() == 32718
        assert ds.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert ds.transform == from_origin(500000, 10000000, 20, 20)
        assert ds.read(1, masked=True).count() == 3
        assert ds.read(1)[0, 1] == pytest.approx(-10)
    assert (tmp_path / "out/backscatter-1.png").is_file()


def test_pair_change_uses_common_valid_pixels_and_later_minus_earlier(tmp_path):
    before = make_gcov(tmp_path / "before.h5", [[1, 1], [1, 1]])
    after = make_gcov(tmp_path / "after.h5", [[10, 0.1], [1, np.nan]], date="2026-01-13T00:00:00Z")
    result = run_analysis(manifest(tmp_path, before, after), tmp_path / "out")
    change = result["measurements"]["change"]
    assert change["valid_pixels"] == 3
    assert change["median_db"] == pytest.approx(0)
    assert change["mean_db"] == pytest.approx(0)
    assert change["increasing_pixels"] == 1
    assert change["decreasing_pixels"] == 1
    assert change["threshold_exceedance_pct"] == pytest.approx(200 / 3)
    assert "not a significance test" in result["report"]


@pytest.mark.parametrize("field,value", [("trackNumber", 2), ("frameNumber", 2), ("lookDirection", "Left"),
                                           ("dataMaturity", "PROVISIONAL"), ("compositeReleaseId", "TEST02")])
def test_incompatible_pair_is_rejected(tmp_path, field, value):
    a = make_gcov(tmp_path / "a.h5")
    b = make_gcov(tmp_path / "b.h5", date="2026-01-13T00:00:00Z", **{field: value})
    with pytest.raises(ScienceError, match="differs"):
        run_analysis(manifest(tmp_path, a, b), tmp_path / "out")


def test_shifted_grid_cannot_be_silently_subtracted(tmp_path):
    a = make_gcov(tmp_path / "a.h5")
    b = make_gcov(tmp_path / "b.h5", date="2026-01-13T00:00:00Z")
    with h5py.File(b, "r+") as f:
        coords = f["/science/LSAR/GCOV/grids/frequencyA/xCoordinates"]
        coords[:] = coords[:] + 10
    with pytest.raises(ScienceError, match="grids do not align"):
        check_compatibility(inspect_gcov(a), inspect_gcov(b))


def test_missing_quality_mask_refuses_analysis(tmp_path):
    path = make_gcov(tmp_path / "a.h5")
    with h5py.File(path, "r+") as f:
        del f["/science/LSAR/GCOV/grids/frequencyA/mask"]
    with pytest.raises(ScienceError, match="quality mask is missing"):
        run_analysis(manifest(tmp_path, path), tmp_path / "out")


def test_prelaunch_observation_refused(tmp_path):
    path = make_gcov(tmp_path / "a.h5", date="2023-01-01T00:00:00Z")
    with pytest.raises(ScienceError, match="Pre-launch"):
        inspect_gcov(path)


def test_no_valid_pixels_refused(tmp_path):
    path = make_gcov(tmp_path / "a.h5", [[0, -1], [np.nan, np.inf]])
    with pytest.raises(ScienceError, match="No finite"):
        run_analysis(manifest(tmp_path, path), tmp_path / "out")


def test_path_outside_input_root_refused(tmp_path):
    path = make_gcov(tmp_path / "a.h5")
    (tmp_path / "allowed").mkdir()
    with pytest.raises(ScienceError, match="configured input directory"):
        run_analysis(manifest(tmp_path / "allowed", path), tmp_path / "out")


def test_derivative_provenance_required_and_retained(tmp_path):
    path = tmp_path / "synthetic.tif"
    with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=1, dtype="float32",
                       crs="EPSG:32718", transform=from_origin(500000, 10000000, 20, 20), nodata=np.nan) as ds:
        ds.write(np.array([[1, 0.1], [np.nan, 0.01]], dtype=np.float32), 1)
    with pytest.raises(ScienceError, match="provenance"):
        inspect_gcov(path)
    sidecar = {"source_url": "https://example.invalid/test-fixture", "source_granule": "NISAR_SYNTHETIC_TEST_ONLY",
               "date": "2026-01-01T00:00:00Z", "product": "GCOV", "polarization": "HHHH", "maturity": "BETA",
               "radiometry": "gamma0_power", "mask_applied": True}
    Path(str(path) + ".provenance.json").write_text(json.dumps(sidecar))
    result = run_analysis(manifest(tmp_path, path), tmp_path / "out")
    assert result["measurements"]["observations"][0]["valid_pixels"] == 3
    assert result["provenance"]["inputs"][0]["sidecar_sha256"]
    assert any("derivative" in s for s in result["validation"]["limitations"])


def test_roi_outside_scene_refused(tmp_path):
    path = make_gcov(tmp_path / "a.h5")
    with pytest.raises(ScienceError, match="does not intersect"):
        run_analysis(manifest(tmp_path, path, bbox=[10, 40, 10.01, 40.01]), tmp_path / "out")


def test_single_input_does_not_claim_change(tmp_path):
    path = make_gcov(tmp_path / "a.h5")
    result = run_analysis(manifest(tmp_path, path), tmp_path / "out")
    assert result["measurements"]["kind"] == "backscatter_summary"
    assert "change" not in result["measurements"]
    assert "It cannot answer what changed" in result["report"]


def test_exception_mask_reads_validity_bits_and_anomaly_codes():
    import numpy as np
    from terra.science import exception_quality
    codes = np.array([[0, 256, 258, 512], [65535, 768, 2, 256]], dtype=np.uint16)
    # Validity bits are present, so HH needs bit 256 and no anomaly code in the low byte.
    assert exception_quality(codes, "HHHH").tolist() == [[False, True, False, False], [False, True, False, True]]
    # Legacy products without validity bits: zero means no anomaly.
    legacy = np.array([[0, 2, 0]], dtype=np.uint16)
    assert exception_quality(legacy, "HHHH").tolist() == [[True, False, True]]


def test_multilook_is_conservative_for_quality_layers():
    import numpy as np
    from terra.acquire import _looks
    power = np.array([[1, 3, 2, 2], [1, 3, 2, np.nan], [4, 4, 1, 1], [4, 4, 1, 1]], dtype=np.float32)
    out = _looks(power, 2, "HHHH")
    assert out[0, 0] == 2 and np.isnan(out[0, 1]) and out[1, 0] == 4 and out[1, 1] == 1
    mask = np.array([[1, 1, 1, 255], [1, 1, 1, 1]], dtype=np.uint8)
    assert _looks(mask, 2, "mask").tolist() == [[1, 255]]
    exc = np.array([[256, 256, 256, 2 | 256], [256, 0, 256, 256]], dtype=np.uint16)
    # HH validity must hold for every sample; any anomaly code propagates.
    assert _looks(exc, 2, "inputDataExceptionMask").tolist() == [[0, 258]]


def test_surface_classes_and_hotspots_locate_patches():
    from rasterio.transform import from_origin
    from terra.science import _hotspots, _surface_classes, LOW_RETURN_DB
    meta = {"crs": "EPSG:32605", "pixel_spacing": [20.0, -20.0]}
    values = np.full((60, 60), -10.0, dtype=np.float32)
    values[5:15, 5:15] = LOW_RETURN_DB - 6  # a 10 × 10 water-like patch
    classes = _surface_classes(values, meta)
    # The 3×3 average in linear power trims the patch edge (bright neighbours dominate), never enlarges it.
    assert 1.5 < classes["low_return_pct"] < 2.8 and 0.02 < classes["low_return_km2"] <= 0.04
    delta = np.zeros((60, 60), dtype=np.float32)
    delta[30:45, 30:45] = 6.0  # brighter patch
    delta[2:8, 40:50] = -5.0   # darker patch
    delta[50, 50] = 9.0        # an isolated pixel is not a hotspot
    delta[0, 0] = np.nan
    transform = from_origin(260000, 2150000, 20, 20)
    spots = _hotspots(delta, transform, meta["crs"], meta, 3.0)
    assert [s["direction"] for s in spots] == ["increase", "decrease"]
    assert spots[0]["rank"] == 1 and spots[0]["mean_change_db"] > 5 and spots[0]["area_km2"] > 0.05
    lat, lon = spots[0]["centre"]
    w, s, e, n = spots[0]["bounds"]
    assert w < lon < e and s < lat < n


def _speckled(rng, mean, looks, shape=(64, 64)):
    return rng.gamma(looks, mean / looks, shape).astype(np.float32)


def _add_pol(path: Path, name: str, values: np.ndarray) -> None:
    with h5py.File(path, "r+") as f:
        f["/science/LSAR/GCOV/grids/frequencyA"][name] = values


def test_enl_estimate_recovers_looks_and_ignores_texture():
    from terra.science import estimate_enl, speckle_sd_db
    rng = np.random.default_rng(1)
    flat = _speckled(rng, 0.05, 10)
    textured = flat * np.where(np.arange(64)[None, :] // 16 % 2, 0.2, 5.0)  # 16-pixel blocks: windows that straddle a block edge are heterogeneous and excluded
    for field in (flat, textured):
        enl = estimate_enl(10 * np.log10(field))
        assert 8 <= enl <= 16
    assert estimate_enl(np.full((5, 5), -10.0, dtype=np.float32)) is None
    assert speckle_sd_db(1) == pytest.approx(5.57, abs=0.01)  # single-look gamma power, in dB
    assert speckle_sd_db(10) < speckle_sd_db(2)


def test_every_polarization_is_measured_and_the_clearer_one_is_named(tmp_path):
    rng = np.random.default_rng(2)
    hh0, hv0 = _speckled(rng, 0.1, 10), _speckled(rng, 0.02, 10)
    hh1, hv1 = _speckled(rng, 0.1, 10), _speckled(rng, 0.02, 10)
    hh1[10:40, 10:40] = _speckled(rng, 0.4, 10, (30, 30))  # +6 dB in HH only
    first = make_gcov(tmp_path / "a.h5", hh0)
    second = make_gcov(tmp_path / "b.h5", hh1, date="2026-02-01T00:00:00Z")
    _add_pol(first, "HVHV", hv0)
    _add_pol(second, "HVHV", hv1)
    change = run_analysis(manifest(tmp_path, first, second), tmp_path / "out")["measurements"]["change"]
    rows = {r["polarization"]: r for r in change["polarizations"]}
    assert set(rows) == {"HHHH", "HVHV"}
    assert all(8 <= r["enl_before"] <= 16 for r in rows.values())
    assert rows["HHHH"]["mean_abs_standardized_change"] > 1.4 * rows["HVHV"]["mean_abs_standardized_change"]
    assert rows["HVHV"]["standardized_exceedance_pct"] < rows["HHHH"]["standardized_exceedance_pct"] / 2
    assert change["leading_polarization"] == "HHHH"
    spot = change["hotspots"][0]
    assert spot["leading_polarization"] == "HHHH"
    assert spot["by_polarization"]["HHHH"]["mean_change_db"] == pytest.approx(6, abs=1)
    assert abs(spot["by_polarization"]["HVHV"]["mean_change_db"]) < 3


def test_single_polarization_pair_names_no_leader(tmp_path):
    rng = np.random.default_rng(3)
    first = make_gcov(tmp_path / "a.h5", _speckled(rng, 0.1, 10))
    second = make_gcov(tmp_path / "b.h5", _speckled(rng, 0.1, 10), date="2026-02-01T00:00:00Z")
    change = run_analysis(manifest(tmp_path, first, second), tmp_path / "out")["measurements"]["change"]
    assert [r["polarization"] for r in change["polarizations"]] == ["HHHH"]
    assert change["leading_polarization"] is None


def test_subset_cached_before_all_terms_were_read_is_not_reused(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from terra import acquire
    monkeypatch.setattr(acquire, "get_settings", lambda: SimpleNamespace(inputs=tmp_path))
    for name, flag in (("G__aoi-old.h5", {}), ("G__aoi-new.h5", {"all_terms": True})):
        (tmp_path / name).write_bytes(b"x")
        (tmp_path / f"{name}.provenance.json").write_text(json.dumps({"covered_bbox": [0, 0, 1, 1], **flag}))
    assert acquire.find_subset("G", [0.1, 0.1, 0.9, 0.9]).name == "G__aoi-new.h5"
