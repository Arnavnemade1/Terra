"""Automatic thresholds, polarimetric signatures and field-verification judgements."""
import numpy as np
import pytest

from terra.signatures import change_thresholds, judge, kittler_illingworth, signature

rng = np.random.default_rng(7)


def test_threshold_is_automatic_only_where_a_change_class_exists():
    speckle = rng.normal(0, 1.2, 200_000)
    both = np.concatenate([rng.normal(0.2, 1.0, 150_000), rng.normal(-5, 1.2, 5_000), rng.normal(4.5, 1.0, 3_000)])
    plain = change_thresholds(speckle, 0.9)
    assert plain["increase_source"] == plain["decrease_source"] == "cfar"  # no change class: the 0.1 % false-alarm level of the noise
    assert plain["increase_db"] == pytest.approx(1.2 * 3.09, abs=0.25)
    two = change_thresholds(both, 0.7)
    assert two["increase_source"] == two["decrease_source"] == "automatic"
    assert 2.5 < two["increase_db"] < 4.5 and 2.5 < two["decrease_db"] < 5  # between the unchanged and changed modes
    one = change_thresholds(np.concatenate([speckle, rng.normal(6, 1.0, 8_000)]), 0.9)
    assert one["increase_source"] == "automatic" and one["decrease_source"] == "cfar"


def test_threshold_never_falls_below_twice_the_speckle():
    d = np.concatenate([rng.normal(0, 0.4, 100_000), rng.normal(1.8, 0.3, 20_000)])
    t = change_thresholds(d, 1.5)
    assert t["increase_db"] >= 3.0  # 2 x 1.5 dB speckle, or the fallback


def test_kittler_illingworth_finds_no_split_in_tiny_samples():
    assert kittler_illingworth(rng.normal(0, 1, 100)) is None


@pytest.mark.parametrize("per,direction,expected", [
    ({"HH": (-6.0, -7.0), "HV": (-5.5, -6.0)}, "decrease", "water"),
    ({"HH": (-0.6, -0.8), "HV": (-4.2, -5.1)}, "decrease", "vegetation_loss"),
    ({"HH": (0.4, 0.5), "HV": (3.1, 4.0)}, "increase", "vegetation_gain"),
    ({"VV": (4.0, 5.0), "VH": (0.8, 1.0)}, "increase", "structure"),
    ({"HH": (2.0, 2.6), "HV": (2.2, 2.4)}, "increase", "roughening"),
    ({"HH": (-1.8, -2.2), "HV": (-1.5, -2.1)}, "decrease", "smoothing"),
    ({"HH": (0.3, 0.4), "HV": (-0.2, -0.3)}, "decrease", "unclear"),
])
def test_signatures_follow_scattering_physics(per, direction, expected):
    s = signature({k: {"mean_change_db": a, "standardized_change": z} for k, (a, z) in per.items()}, direction)
    assert s["hypothesis"] == expected and s["question"].endswith("?") and s["needs"]
    assert all(isinstance(c, str) for c in s["consistent_with"])


def test_single_polarization_does_not_pretend_to_know_the_mechanism():
    s = signature({"HH": {"mean_change_db": -4.0, "standardized_change": -5.0}}, "decrease")
    assert s["hypothesis"] == "darker" and "cannot be separated" in s["mechanism"]
    assert signature({}, "increase")["hypothesis"] == "brighter"


def test_confidence_reflects_the_change_in_speckle_units():
    assert signature({"HH": {"mean_change_db": -6, "standardized_change": -7}, "HV": {"mean_change_db": -5, "standardized_change": -6}}, "decrease")["confidence"] == "strong"
    assert signature({"HH": {"mean_change_db": 2, "standardized_change": 2.5}, "HV": {"mean_change_db": 2.1, "standardized_change": 2.2}}, "increase")["confidence"] == "moderate"


def test_judgements_use_only_measured_values():
    ctx = {"surface_median_c": 25.0}
    assert judge("water", {"cover": "Permanent water bodies"}, ctx)["verdict"] == "supports"
    assert judge("water", {"cover": "Cropland", "surface_c": 33.0}, ctx)["verdict"] == "contradicts"
    assert judge("vegetation_loss", {"cover": "Tree cover", "surface_c": 31.0}, ctx)["verdict"] == "supports"
    assert judge("vegetation_loss", {"cover": "Built-up", "surface_c": 31.0}, ctx)["verdict"] == "contradicts"
    assert judge("vegetation_loss", {"cover": "Tree cover", "surface_c": 25.5}, ctx)["verdict"] == "inconclusive"
    assert judge("structure", {"cover": "Built-up"}, ctx)["verdict"] == "supports"
    nothing = judge("roughening", {}, ctx)
    assert nothing["verdict"] == "inconclusive" and nothing["reasons"]


def test_unchanged_ground_is_modelled_as_speckle_when_the_looks_are_known():
    from terra.signatures import speckle_tail
    L = 10.0; db = lambda n: 10 * np.log10(rng.gamma(L, 1 / L, n))  # noqa: E731
    speckle = db(300_000) - db(300_000)
    t = change_thresholds(speckle, None, (L, L))
    assert t["increase_source"] == "cfar" and t["unchanged_model"].startswith("speckle")
    assert abs(float((speckle >= t["increase_db"]).mean()) - 1e-3) < 6e-4  # the false-alarm rate it promises
    up, down = speckle_tail(3.0, (L, L))
    assert 0 < up < 0.2 and abs(up - down) < 0.02  # symmetric for equal looks
    change = np.concatenate([speckle, db(9_000) + 6 - db(9_000)])
    assert change_thresholds(change, None, (L, L))["increase_source"] == "automatic"


def test_single_polarization_patches_can_still_be_checked():
    ctx = {"surface_median_c": 25.0}
    assert judge("brighter", {"cover": "Built-up"}, ctx)["verdict"] == "supports"
    assert judge("brighter", {"cover": "Bare / sparse vegetation", "surface_c": 44.0, "surface_p95_c": 40.0}, ctx)["verdict"] == "supports"
    assert judge("darker", {"cover": "Grassland", "surface_c": 20.0}, ctx)["verdict"] == "supports"
    assert judge("darker", {"cover": "Grassland", "surface_c": 26.0}, ctx)["verdict"] == "inconclusive"


def test_optical_change_is_independent_evidence():
    opt = lambda db, dg, after=0.4: {"optical": {"before_date": "2026-06-10", "after_date": "2026-10-15", "d_brightness": db, "d_greenness": dg,  # noqa: E731
                                                 "brightness_before": after - db, "brightness_after": after, "greenness_before": 0.1, "greenness_after": 0.1 + dg}}
    j = judge("vegetation_loss", opt(0.05, -0.12)); assert j["verdict"] == "supports" and j["evidence"] == "optical" and "greenness fell" in j["reasons"][0]
    assert judge("vegetation_loss", opt(0.0, 0.08))["verdict"] == "contradicts"
    assert judge("water", opt(-0.15, -0.02, after=0.1))["verdict"] == "supports"
    assert judge("brighter", opt(-0.1, 0.0))["verdict"] == "supports"
    flat = judge("brighter", opt(0.005, 0.004))
    assert flat["verdict"] == "inconclusive" and "radar-only change" in flat["reasons"][0]


def test_snow_or_cloud_in_an_image_is_named_not_counted_as_change():
    snowy = {"optical": {"before_date": "2026-06-10", "after_date": "2026-10-15", "brightness_before": 0.82, "brightness_after": 0.12,
                         "colour_before": 0.08, "colour_after": 0.07, "d_brightness": -0.70, "d_greenness": -0.07, "greenness_before": 0.02, "greenness_after": -0.05}}
    j = judge("brighter", snowy)
    assert j["verdict"] == "inconclusive" and "snowmelt" in j["alternative"] and "Sentinel-2" in j["reasons"][0]
    plume = {"optical": {**snowy["optical"], "brightness_before": 0.3, "colour_before": 0.1, "brightness_after": 0.42, "colour_after": 0.03}}
    assert judge("darker", {**plume, "optical": {**plume["optical"], "brightness_after": 0.6}})["verdict"] == "inconclusive"
