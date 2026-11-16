"""Trend stack numerics: 8-bit quantization, robust per-pixel regression, tuning and summaries."""
import numpy as np
import pytest

from terra.trends import (
    OFFSET_DB,
    STEP_DB,
    dequantize,
    fit_trend,
    quantization_effect,
    quantization_error_db,
    quantize,
    summarize,
    tune_delta,
)

rng = np.random.default_rng(11)


def _dates(n: int = 24, years: float = 2.0) -> np.ndarray:
    return np.linspace(0.0, years, n)


def test_quantization_codes_round_trip_and_mark_nodata():
    db = rng.uniform(OFFSET_DB, OFFSET_DB + 254 * STEP_DB, 5000)
    back = dequantize(quantize(db))
    assert np.all(np.isfinite(back))
    assert np.max(np.abs(back - db)) <= STEP_DB / 2 + 1e-5  # half a code, 0.1 dB

    codes = quantize(np.array([np.nan, -1e6, 1e6, OFFSET_DB, OFFSET_DB + 254 * STEP_DB, np.inf]))
    assert codes.dtype == np.uint8
    assert codes[0] == 0 and codes[5] == 0  # NaN and infinity are no data, not an end code
    assert codes[1] == 1 and codes[2] == 255  # clipping, no wraparound
    assert codes[3] == 1 and codes[4] == 255

    back = dequantize(codes)
    assert np.isnan(back[0]) and np.isnan(back[5])
    assert back[1] == pytest.approx(-40.0, abs=1e-5)
    assert back[4] == pytest.approx(10.8, abs=1e-5)

    error = quantization_error_db()
    assert error["step_db"] == 0.2 and error["max_abs_db"] == 0.1
    assert error["rms_db"] == pytest.approx(0.2 / np.sqrt(12))


def test_quantization_rejects_codes_outside_the_byte():
    with pytest.raises(ValueError):
        dequantize(np.array([256]))
    with pytest.raises(ValueError):
        dequantize(np.array([1.5]))


def test_fit_trend_recovers_a_known_slope():
    t = _dates()
    truth = -1.5
    y = truth * t[:, None] + 3.0 + rng.normal(0.0, 1.0, (t.size, 300))
    fit = fit_trend(t, y, np.inf)
    assert np.all(np.isfinite(fit["slope_db_per_year"])) and fit["n_used"].min() == t.size
    se = float(np.median(fit["slope_se"]))
    assert abs(float(np.median(fit["slope_db_per_year"])) - truth) < 3 * se
    assert abs(float(np.median(fit["intercept_db"])) - 3.0) < 0.5
    within = np.abs(fit["slope_db_per_year"] - truth) < 3 * fit["slope_se"]
    assert within.mean() > 0.95  # a 3 SE interval should cover almost every pixel
    assert abs(se - 1.0 / np.sqrt(np.sum((t - t.mean()) ** 2))) < 0.05  # the standard error is honest


def test_huber_beats_ordinary_least_squares_with_gross_outliers():
    t = _dates()
    truth = -1.5
    clean = truth * t[:, None] + 2.0 + rng.normal(0.0, 1.0, (t.size, 400))
    gross = rng.random((t.size, 400)) < 0.2
    y = clean + np.where(gross, 8.0, 0.0)
    ols = fit_trend(t, y, np.inf)
    huber = fit_trend(t, y, 1.345)
    ols_error = float(np.median(np.abs(ols["slope_db_per_year"] - truth)))
    huber_error = float(np.median(np.abs(huber["slope_db_per_year"] - truth)))
    assert ols_error > 0.3  # the outliers really do move an unweighted slope
    assert huber_error < 0.7 * ols_error


def test_p_values_flag_a_strong_trend_and_stay_calibrated_on_noise():
    t = _dates()
    strong = fit_trend(t, -1.5 * t[:, None] + rng.normal(0.0, 1.0, (t.size, 20)), np.inf)
    assert float(np.median(strong["p_value"])) < 1e-3

    noise = fit_trend(t, rng.normal(0.0, 1.0, (t.size, 2000)), np.inf)
    false_positive = float((noise["p_value"] < 0.05).mean())
    assert 0.02 < false_positive < 0.09  # nominal 5 %
    assert float((noise["p_value"] > 0.05).mean()) > 0.9


def test_short_series_and_missing_dates_are_left_out():
    t = _dates(12)
    y = np.full((12, 3), np.nan)
    y[:, 0] = -0.8 * t + 1.0
    y[:2, 1] = [0.0, 1.0]  # two valid dates: too few to fit
    y[:5, 2] = -0.8 * t[:5] + 1.0
    y[3, 2] = np.nan  # a missing date in an otherwise long series
    fit = fit_trend(t, y, 1.345)

    assert fit["n_used"].tolist() == [12, 2, 4]
    for key in ("slope_db_per_year", "intercept_db", "slope_se", "r2", "p_value"):
        assert np.isnan(fit[key][1])  # no fabricated slope for a two-date pixel
    assert fit["slope_db_per_year"][2] == pytest.approx(-0.8, abs=0.05)
    assert fit["slope_db_per_year"][0] == pytest.approx(-0.8, abs=1e-9)
    assert fit["r2"][2] == pytest.approx(1.0, abs=1e-6)  # the four points are exactly on the line


def test_tune_delta_prefers_a_finite_delta_when_outliers_are_present():
    t = _dates()
    y = -1.0 * t[:, None] + rng.normal(0.0, 1.0, (t.size, 150))
    y = y + np.where(rng.random((t.size, 150)) < 0.2, 8.0, 0.0)
    tuned = tune_delta(t, y, sample=150)
    assert tuned["pixels"] == 150 and tuned["folds"] == t.size
    assert np.isfinite(tuned["delta"])
    cv = tuned["cv_mae_db"]
    assert set(cv) == {"0.5", "1.0", "1.345", "2.0", "3.0", "inf"}
    assert cv[str(tuned["delta"])] == pytest.approx(min(cv.values()))
    assert cv["inf"] > cv[str(tuned["delta"])]


def test_tune_delta_is_deterministic_for_a_given_seed():
    t = _dates()
    y = rng.normal(0.0, 1.0, (t.size, 60))
    first = tune_delta(t, y, sample=20, seed=4)
    second = tune_delta(t, y, sample=20, seed=4)
    assert first == second


def test_quantization_barely_moves_a_realistic_trend():
    t = _dates()
    y = -0.4 * t[:, None] - 10.0 + rng.normal(0.0, 1.0, (t.size, 800))
    effect = quantization_effect(t, y, 1.345)
    assert effect["pixels"] == 800
    assert effect["median_abs_slope_diff_db_per_year"] < 0.05  # the 0.1 dB step is small next to 1 dB noise
    assert effect["p99_abs_slope_diff_db_per_year"] < 0.15
    assert 0.0 <= effect["significance_change_fraction"] <= 1.0


def test_summary_reports_median_slope_direction_and_area():
    slopes = np.concatenate([np.full(40, -1.0), np.full(30, 0.5), np.full(10, 0.2), np.full(20, np.nan)])
    p_values = np.concatenate([np.full(40, 1e-4), np.full(30, 1e-4), np.full(10, 0.4), np.full(20, np.nan)])
    stats = summarize({"slope_db_per_year": slopes, "p_value": p_values}, pixel_km2=0.01)
    assert stats["pixels"] == 80 and stats["significant_pixels"] == 70
    assert stats["median_slope_db_per_year"] == pytest.approx(-0.4, abs=1e-9)
    assert stats["slope_iqr_db_per_year"] > 0
    assert stats["increasing_pixels"] == 30 and stats["decreasing_pixels"] == 40
    assert stats["increasing_share_of_significant"] == pytest.approx(30 / 70)
    assert stats["decreasing_share_of_significant"] == pytest.approx(40 / 70)
    assert stats["increasing_km2"] == pytest.approx(0.3) and stats["decreasing_km2"] == pytest.approx(0.4)
    blind = summarize({"slope_db_per_year": np.array([0.1, -0.2]), "p_value": np.array([0.5, 0.5])})
    assert blind["significant_pixels"] == 0 and blind["increasing_km2"] is None
    assert blind["increasing_share_of_significant"] is None
