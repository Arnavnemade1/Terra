"""Long-term backscatter trends over a stack of NISAR dates.

Four pieces of the trend layer:

1. **8-bit quantization.** Gamma0 in dB is stored as 256 codes: 0 means no data, and 1..255 step
   by 0.2 dB from -40.0 dB (so -40.0 .. +10.8 dB). Rounding to the nearest code costs at most half
   a step, 0.1 dB, which is 0.2/sqrt(12) = 0.058 dB RMS when the rounding error is uniform over the
   step. `quantization_effect` measures what that rounding does to a fitted slope instead of
   assuming it is harmless.

2. **Robust per-pixel linear regression.** The slope of backscatter in dB per year is fitted by
   iteratively reweighted least squares with the Huber loss. Residuals are scaled per pixel by
   1.4826 times the median absolute residual (a MAD estimate of the noise standard deviation), so a
   few bright dates cannot drag the slope. delta = inf gives ordinary least squares.

3. **Tuning delta.** The Huber cutoff is chosen for the area at hand by leave-one-date-out cross
   validation: hold out a date, fit the remaining dates, predict the held-out one. The candidate
   with the lowest mean absolute prediction error is used. A finite delta wins when gross outliers
   are present and tends to lose to ordinary least squares when the residuals are Gaussian.

4. **Summaries.** The median slope and its interquartile range, plus how many pixels trend up or
   down with p < 0.05 and their area, when the pixel area is known.

Every function is pure numerics: it takes arrays and returns arrays or plain Python values.
Nothing here reads a file, and nothing here claims a trend is a physical change: moisture,
vegetation, calibration and geometry all move L-band backscatter over months.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy import stats

STEP_DB = 0.2
OFFSET_DB = -40.0
MIN_DATES = 3
MAD_FLOOR = 1e-3
DEFAULT_DELTA_CANDIDATES = (0.5, 1.0, 1.345, 2.0, 3.0, np.inf)


def quantization_error_db() -> dict:
    """Rounding bounds of the 8-bit code: half a step at worst, uniform rounding error on average.

    The RMS value 0.2/sqrt(12) is the standard deviation of a uniform error over one step. It
    describes the rounding alone; it is not the measurement error of the backscatter.
    """
    return {"step_db": STEP_DB, "max_abs_db": STEP_DB / 2, "rms_db": STEP_DB / math.sqrt(12)}


def quantize(db: np.ndarray) -> np.ndarray:
    """Map dB to 8-bit codes: 0 for no data, otherwise the nearest code, clipped to 1..255.

    Clipping is intentional: values below -40.0 dB and above +10.8 dB saturate at the end codes
    instead of wrapping around, so an out-of-range sample is never mistaken for a valid one.
    """
    values = np.asarray(db, dtype=np.float64)
    codes = np.zeros(values.shape, dtype=np.uint8)
    finite = np.isfinite(values)
    with np.errstate(over="ignore", invalid="ignore"):
        scaled = np.rint((values[finite] - OFFSET_DB) / STEP_DB) + 1.0
        codes[finite] = np.clip(scaled, 1.0, 255.0).astype(np.uint8)
    return codes


def dequantize(codes: np.ndarray) -> np.ndarray:
    """Map 8-bit codes back to dB: 0 becomes NaN, every other code is OFFSET_DB + (code - 1) * STEP_DB."""
    values = np.asarray(codes)
    if values.dtype != np.uint8:
        if not np.all(np.equal(values, np.floor(values))):
            raise ValueError("Quantization codes must be whole numbers.")
        if np.any(values < 0) or np.any(values > 255):
            raise ValueError("Quantization codes must lie in 0..255.")
    codes_u8 = values.astype(np.uint8)
    out = np.full(codes_u8.shape, np.nan, dtype=np.float32)
    valid = codes_u8 > 0
    out[valid] = OFFSET_DB + (codes_u8[valid].astype(np.float32) - 1.0) * np.float32(STEP_DB)
    return out


def _weighted_sums(t2: np.ndarray, yv: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, ...]:
    return (w.sum(axis=0), (w * t2).sum(axis=0), (w * yv).sum(axis=0),
            (w * t2 * t2).sum(axis=0), (w * t2 * yv).sum(axis=0))


def _solve(sw: np.ndarray, swx: np.ndarray, swy: np.ndarray, swxx: np.ndarray,
           swxy: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    det = sw * swxx - swx ** 2
    ok = det > 0
    safe_det = np.where(ok, det, 1.0)
    safe_sw = np.where(sw > 0, sw, 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(ok, (sw * swxy - swx * swy) / safe_det, np.nan)
        intercept = np.where(ok, (swy - slope * swx) / safe_sw, np.nan)
    return slope, intercept, det, ok


def _mad_scale(residual: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Per-pixel 1.4826 * median(|residual|), the MAD estimate of the noise, with a 1e-3 dB floor."""
    masked = np.ma.masked_array(np.abs(residual), mask=~valid)
    median = np.ma.median(masked, axis=0).filled(np.nan)
    scale = 1.4826 * median
    return np.where(np.isfinite(scale), np.maximum(scale, MAD_FLOOR), 1.0)


def fit_trend(t_years: np.ndarray, y: np.ndarray, delta: float,
              iterations: int = 20) -> dict:
    """Per-pixel Huber regression of backscatter (dB) on time (years), vectorised over pixels.

    `t_years` has one value per date and `y` has shape (dates, pixels); NaN in `y` means the pixel
    has no observation on that date and that date is left out of the fit for that pixel. Pixels with
    fewer than three valid dates get NaN outputs.

    The Huber weights are w = 1 where |r| <= delta * s and w = delta * s / |r| beyond that, with
    s = 1.4826 * median(|r|) per pixel (floor 1e-3 dB). The fit is repeated `iterations` times, each
    time down-weighting the residuals of the previous fit. delta = np.inf keeps every weight at 1,
    which is ordinary least squares.

    The slope standard error comes from the weighted least squares covariance with residual variance
    sum(w * r^2) / (sum(w) - 2), so it is only defined when the summed weights exceed two. The
    p-value is the two-sided t test of slope = 0 with n_used - 2 degrees of freedom, and r2 is the
    weighted coefficient of determination. These are per-pixel statistics: neighbouring radar pixels
    are correlated, so the count of significant pixels is not a count of independent tests.
    """
    t = np.asarray(t_years, dtype=np.float64).ravel()
    values = np.asarray(y, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or values.shape[0] != t.size:
        raise ValueError("y must have one row per date in t_years.")
    if t.size < MIN_DATES:
        raise ValueError(f"At least {MIN_DATES} dates are required to fit a trend.")
    if not (np.isinf(delta) or delta > 0):
        raise ValueError("delta must be positive, or infinity for ordinary least squares.")
    if np.any(~np.isfinite(t)):
        raise ValueError("t_years must be finite.")

    n, p = values.shape
    valid = np.isfinite(values)
    counts = valid.sum(axis=0)
    yv = np.where(valid, values, 0.0)
    t2 = t[:, None]
    w = valid.astype(np.float64)

    slope = np.full(p, np.nan)
    intercept = np.full(p, np.nan)
    for _ in range(max(1, int(iterations))):
        slope, intercept, _, _ = _solve(*_weighted_sums(t2, yv, w))
        if np.isinf(delta):
            break
        residual = np.where(valid, yv - (intercept + slope * t2), 0.0)
        limit = float(delta) * _mad_scale(residual, valid)
        abs_r = np.abs(residual)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(abs_r > limit, limit / np.where(abs_r > 0, abs_r, 1.0), 1.0)
        w = valid.astype(np.float64) * ratio

    sw, swx, swy, swxx, swxy = _weighted_sums(t2, yv, w)
    slope, intercept, det, solved = _solve(sw, swx, swy, swxx, swxy)
    ok = solved & (counts >= MIN_DATES) & (sw > 2.0)
    slope = np.where(ok, slope, np.nan)
    intercept = np.where(ok, intercept, np.nan)
    residual = np.where(valid, yv - (intercept + slope * t2), 0.0)
    ss_res = (w * residual ** 2).sum(axis=0)
    df = counts - 2
    safe_sw = np.where(sw > 0, sw, 1.0)
    sigma2 = np.where(ok, ss_res / np.where(ok, sw - 2.0, 1.0), np.nan)
    se = np.where(ok, np.sqrt(sigma2 * sw / np.where(det > 0, det, 1.0)), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_stat = slope / se
    dof = np.where(ok, np.maximum(df, 1), 1)
    p_value = np.where(ok, 2.0 * stats.t.sf(np.abs(np.where(ok, t_stat, 0.0)), dof), np.nan)
    ybar = np.where(sw > 0, swy / safe_sw, np.nan)
    ss_tot = (w * (yv - ybar) ** 2).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        r2 = np.where(ok & (ss_tot > 0), 1.0 - ss_res / np.where(ss_tot > 0, ss_tot, 1.0), np.nan)
    return {
        "slope_db_per_year": slope,
        "intercept_db": intercept,
        "slope_se": se,
        "r2": r2,
        "p_value": p_value,
        "n_used": counts.astype(np.int64),
    }


def tune_delta(t_years: np.ndarray, y: np.ndarray,
               candidates: Sequence[float] = DEFAULT_DELTA_CANDIDATES,
               sample: int = 4000, seed: int = 0) -> dict:
    """Choose the Huber delta for this stack by leave-one-date-out cross validation.

    Pixels with at least four valid dates are eligible. A deterministic random sample of them
    (`sample` pixels, drawn with `seed`) is cross validated: for every held-out date the trend is
    fitted on the other dates and used to predict the held-out date in dB. Each candidate delta gets
    the mean absolute prediction error over all folds and sampled observations, and the smallest
    wins.

    Ties keep the earlier candidate in `candidates`, so put np.inf last if ordinary least squares
    should only win when it is strictly better. The returned `cv_mae_db` keys are str(candidate),
    with "inf" for infinity.
    """
    t = np.asarray(t_years, dtype=np.float64).ravel()
    values = np.asarray(y, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or values.shape[0] != t.size:
        raise ValueError("y must have one row per date in t_years.")
    if int(sample) < 1:
        raise ValueError("sample must be at least one pixel.")
    if not len(candidates):
        raise ValueError("Provide at least one Huber delta candidate.")

    valid = np.isfinite(values)
    eligible = np.nonzero(valid.sum(axis=0) >= 4)[0]
    if eligible.size == 0:
        raise ValueError("No pixel has four valid dates, so the delta cannot be tuned.")
    rng = np.random.default_rng(seed)
    if eligible.size > int(sample):
        chosen = np.sort(rng.choice(eligible, size=int(sample), replace=False))
    else:
        chosen = eligible
    picked = values[:, chosen]

    cv: dict[str, float] = {}
    for candidate in candidates:
        total, count = 0.0, 0
        for k in range(t.size):
            keep = np.arange(t.size) != k
            fit = fit_trend(t[keep], picked[keep], candidate)
            predicted = fit["intercept_db"] + fit["slope_db_per_year"] * t[k]
            observed = picked[k]
            usable = np.isfinite(predicted) & np.isfinite(observed)
            total += float(np.abs(predicted[usable] - observed[usable]).sum())
            count += int(usable.sum())
        cv[str(candidate)] = total / count if count else math.inf
    best = min(candidates, key=lambda c: cv[str(c)])
    return {"delta": float(best), "cv_mae_db": cv, "pixels": int(chosen.size), "folds": int(t.size)}


def quantization_effect(t_years: np.ndarray, y_float: np.ndarray, delta: float) -> dict:
    """How much 8-bit quantization moves the fitted slope, measured on the same pixels.

    The stack is fitted twice: once on the float dB values and once on dequantize(quantize(...)).
    Reported are the median and 99th percentile absolute slope difference in dB/year and the
    fraction of pixels whose significance at p < 0.05 differs between the two. The slope difference
    matters for small trends; a large significance-change fraction means the quantization step is
    comparable to the trend over the stack.
    """
    values = np.asarray(y_float, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2:
        raise ValueError("y_float must have shape (dates, pixels).")
    exact = fit_trend(t_years, values, delta)
    stored = fit_trend(t_years, dequantize(quantize(values)).astype(np.float64), delta)
    difference = np.abs(exact["slope_db_per_year"] - stored["slope_db_per_year"])
    usable = np.isfinite(difference)
    if not usable.any():
        raise ValueError("No pixel has a defined slope on both the float and quantized data.")
    significant = (exact["p_value"] < 0.05) != (stored["p_value"] < 0.05)
    return {
        "delta": float(delta),
        "step_db": STEP_DB,
        "pixels": int(usable.sum()),
        "median_abs_slope_diff_db_per_year": float(np.median(difference[usable])),
        "p99_abs_slope_diff_db_per_year": float(np.percentile(difference[usable], 99)),
        "significance_change_fraction": float(significant[usable].mean()),
    }


def summarize(fit: dict, pixel_km2: float | None = None) -> dict:
    """Median slope with its interquartile range, and the significant upward and downward shares.

    "Share" is among the pixels whose slope is significant at p < 0.05; the fraction of all fitted
    pixels that is significant is reported separately, because with correlated pixels it overstates
    how much of the area really trends. Areas are counts times `pixel_km2` and stay None when the
    pixel area is unknown, since a geographic CRS does not give a constant pixel area.
    """
    slope = np.asarray(fit["slope_db_per_year"], dtype=np.float64)
    p_value = np.asarray(fit["p_value"], dtype=np.float64)
    if slope.shape != p_value.shape:
        raise ValueError("slope_db_per_year and p_value must have the same length.")
    defined = np.isfinite(slope)
    if not defined.any():
        raise ValueError("No pixel has a defined trend slope.")
    q25, median, q75 = np.percentile(slope[defined], [25.0, 50.0, 75.0])
    significant = defined & np.isfinite(p_value) & (p_value < 0.05)
    increasing = significant & (slope > 0)
    decreasing = significant & (slope < 0)
    n_pixels = int(defined.sum())
    n_significant = int(significant.sum())
    up, down = int(increasing.sum()), int(decreasing.sum())
    area = None if pixel_km2 is None else float(pixel_km2)
    return {
        "pixels": n_pixels,
        "median_slope_db_per_year": float(median),
        "slope_iqr_db_per_year": float(q75 - q25),
        "significant_pixels": n_significant,
        "significant_fraction": n_significant / n_pixels,
        "increasing_pixels": up,
        "decreasing_pixels": down,
        "increasing_share_of_significant": up / n_significant if n_significant else None,
        "decreasing_share_of_significant": down / n_significant if n_significant else None,
        "increasing_km2": up * area if area is not None else None,
        "decreasing_km2": down * area if area is not None else None,
        "units": "dB/year",
    }
