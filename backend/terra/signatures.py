"""What the radar change is consistent with, and how a drone could check it.

Two pieces of the NISAR "which polarization shows the change" question:

1. **Automatic change threshold** (Kittler & Illingworth 1986, "Minimum error thresholding"). For each direction
   of change, the difference image's histogram is split in two at the threshold that minimises the classification
   error of a two-Gaussian model. A threshold is used only when the split is real: an interior minimum, at least
   0.05 % of pixels on the changed side, a threshold above twice the expected speckle (when known) and between
   1.5 and 12 dB. Otherwise the conventional 3 dB is kept and the report says so.

2. **Scattering signature of each changed patch.** Co-polarized (HH/VV) and cross-polarized (HV/VH) backscatter
   respond differently to physical change: cross-pol is dominated by volume scattering (vegetation canopies),
   co-pol by surface and double-bounce scattering. From each polarization's change and its speckle-standardized
   size (z), a patch is described as *consistent with* a small set of mechanisms. These are hypotheses for a person
   or a drone to check, never classifications: soil moisture, incidence geometry, wind on water and calibration
   can produce the same signs.

Each signature carries the question a drone should answer and the sensors that help answer it.
"""
from __future__ import annotations

import math

import numpy as np

FALLBACK_DB = 3.0
Z_SIGNIFICANT = 2.0
Z_STRONG = 4.0


def kittler_illingworth(values: np.ndarray, *, lo: float = 0.5, hi: float = 12.0, step: float = 0.05,
                        min_changed: float = 0.0005, floor_db: float = 1.5) -> float | None:
    """Minimum-error threshold separating the upper tail of `values` (dB of change in one direction) from the rest.

    Returns None when the histogram gives no interior minimum or too few changed pixels (unimodal: no change class)."""
    v = values[np.isfinite(values)].astype(np.float64)
    if v.size < 500:
        return None
    edges = np.arange(min(lo, float(v.min())), max(hi, float(np.percentile(v, 99.95))) + step, step)
    hist, edges = np.histogram(v, bins=edges)
    centres = (edges[:-1] + edges[1:]) / 2
    p = hist / hist.sum()
    c0 = np.cumsum(p); m0 = np.cumsum(p * centres); s0 = np.cumsum(p * centres ** 2)
    total_m, total_s = m0[-1], s0[-1]
    best, best_t = math.inf, None
    candidates = np.nonzero((centres >= lo) & (centres <= hi))[0]
    for i in candidates:
        p1, p2 = c0[i], 1 - c0[i]
        if p1 <= 1e-9 or p2 < min_changed:
            continue
        mu1, mu2 = m0[i] / p1, (total_m - m0[i]) / p2
        var1, var2 = s0[i] / p1 - mu1 ** 2, (total_s - s0[i]) / p2 - mu2 ** 2
        if var1 <= 1e-6 or var2 <= 1e-6:
            continue
        j = 1 + 2 * (p1 * math.log(math.sqrt(var1)) + p2 * math.log(math.sqrt(var2))) - 2 * (p1 * math.log(p1) + p2 * math.log(p2))
        if j < best:
            best, best_t = j, float(edges[i + 1])
    if best_t is None or best_t <= lo + step or best_t >= hi - step:  # minimum at an edge: no real split
        return None
    return max(floor_db, best_t)


def change_thresholds(delta: np.ndarray, speckle_sd_db: float | None = None) -> dict:
    """Thresholds for increases and decreases, each automatic when the data support it."""
    smooth = delta[np.isfinite(delta)].astype(np.float64)
    out = {"method": "Kittler-Illingworth minimum-error thresholding per direction, accepted only where the changed side holds "
                     "at least 5x the pixels the unchanged distribution (median, 1.4826 x MAD) predicts; otherwise 3 dB", "fallback_db": FALLBACK_DB}
    floor = max(1.5, 2 * speckle_sd_db) if speckle_sd_db else 1.5
    centre = float(np.median(smooth)) if smooth.size else 0.0
    sigma = float(1.4826 * np.median(np.abs(smooth - centre))) if smooth.size else 0.0
    for name, values in (("increase", smooth), ("decrease", -smooth)):
        t = kittler_illingworth(values, floor_db=floor)
        accepted = False
        if t is not None and sigma > 0:
            offset = t - (centre if name == "increase" else -centre)
            expected = 0.5 * math.erfc(offset / (sigma * math.sqrt(2)))
            observed = float((values >= t).mean())
            accepted = observed >= 0.0005 and observed >= 5 * expected
            out[f"{name}_tail_excess"] = round(observed / expected, 1) if expected > 0 else None
        out[f"{name}_db"] = round(t, 2) if accepted else FALLBACK_DB
        out[f"{name}_source"] = "automatic" if accepted else "fallback"
    return out


def _pick(per: dict, names: tuple[str, ...]) -> tuple[str, dict] | None:
    for n in names:
        if n in per:
            return n, per[n]
    return None


def signature(per: dict, direction: str) -> dict:
    """Mechanisms a patch's polarimetric change is consistent with, the question to check, and helpful sensors.

    `per` maps polarization -> {"mean_change_db", "standardized_change"} for the patch."""
    co, cx = _pick(per, ("HH", "VV")), _pick(per, ("HV", "VH"))
    zs = [abs(v.get("standardized_change") or 0) for v in per.values()]
    confidence = "strong" if zs and max(zs) >= Z_STRONG else "moderate" if zs and max(zs) >= Z_SIGNIFICANT else "weak"
    if not (co and cx):
        bright = direction == "increase"
        return {"hypothesis": "brighter" if bright else "darker", "mechanism": "Single polarization: the scattering mechanism cannot be separated",
                "consistent_with": ["rougher or wetter ground", "new structures or debris"] if bright else ["smoother or drier ground", "standing water", "vegetation loss"],
                "question": "What changed on the ground here?", "needs": ["eo"], "confidence": confidence, "basis": _basis(per)}
    (cn, c), (xn, x) = co, cx
    dc, dx = c["mean_change_db"], x["mean_change_db"]
    zc, zx = c.get("standardized_change") or 0, x.get("standardized_change") or 0
    sig_c, sig_x = abs(zc) >= Z_SIGNIFICANT, abs(zx) >= Z_SIGNIFICANT
    base = {"confidence": confidence, "basis": _basis(per), "polarizations": [cn, xn]}
    if dc <= -3 and dx <= -3 and sig_c and sig_x:
        return {**base, "hypothesis": "water", "mechanism": f"Both {cn} and {xn} fell sharply: the surface became radar-smooth (specular)",
                "consistent_with": ["new standing or open water", "flooding", "very smooth bare ground"],
                "question": "Is there new standing water or flooding here?", "needs": ["eo", "ir"]}
    if dx < 0 and sig_x and dx - dc <= -1.5:
        return {**base, "hypothesis": "vegetation_loss", "mechanism": f"{xn} (volume scattering) fell more than {cn}: less vegetation canopy",
                "consistent_with": ["vegetation clearing or harvest", "burn scar", "canopy damage or defoliation"],
                "question": "Has vegetation been cleared, burned or harvested here?", "needs": ["multispectral", "ir", "eo"]}
    if dx > 0 and sig_x and dx - dc >= 1.5:
        return {**base, "hypothesis": "vegetation_gain", "mechanism": f"{xn} (volume scattering) rose more than {cn}: more vegetation volume",
                "consistent_with": ["crop or vegetation growth", "regrowth", "wetter canopy"],
                "question": "Has vegetation grown or a crop developed here?", "needs": ["multispectral", "eo"]}
    if dc > 0 and sig_c and dc - dx >= 1.5:
        return {**base, "hypothesis": "structure", "mechanism": f"{cn} rose more than {xn}: stronger surface or double-bounce scattering",
                "consistent_with": ["new structures or debris", "water under standing vegetation (double bounce)", "exposed rock or a fresh deposit"],
                "question": "Are there new structures, debris or flooded vegetation here?", "needs": ["eo", "lidar"]}
    if dc > 0 and dx > 0 and (sig_c or sig_x):
        return {**base, "hypothesis": "roughening", "mechanism": f"{cn} and {xn} both rose: rougher or wetter surface",
                "consistent_with": ["wetter soil", "rougher ground (tillage, landslide, fresh lava)", "debris"],
                "question": "Is the ground here wetter, disturbed or newly deposited?", "needs": ["eo", "ir", "lidar"]}
    if dc < 0 and dx < 0 and (sig_c or sig_x):
        return {**base, "hypothesis": "smoothing", "mechanism": f"{cn} and {xn} both fell: smoother or drier surface",
                "consistent_with": ["drier soil", "smoother ground", "shallow water or wet snow"],
                "question": "Is the ground here drier, smoother or covered by water or snow?", "needs": ["eo", "ir"]}
    return {**base, "hypothesis": "unclear", "mechanism": "The polarizations disagree or the change is near the speckle level",
            "consistent_with": ["speckle or calibration differences", "a small or mixed change"], "question": "Is there any visible change here?", "needs": ["eo"]}


def _basis(per: dict) -> str:
    parts = []
    for pol, v in per.items():
        z = v.get("standardized_change")
        parts.append(f"{pol} {v['mean_change_db']:+.1f} dB" + (f" (z {z:+.1f})" if z is not None else ""))
    return ", ".join(parts)


# ---------------------------------------------------------------- field verification
VEGETATED = {"Tree cover", "Shrubland", "Grassland", "Cropland", "Mangroves", "Herbaceous wetland", "Moss and lichen"}


def judge(hypothesis: str, observed: dict, context: dict | None = None) -> dict:
    """Whether a drone's measured observation supports a patch's hypothesis: supports, contradicts or inconclusive.

    `observed` is a field-note observation (land cover from ESA WorldCover 2021, i.e. before the change,
    Landsat surface temperature, the area's 95th percentile, radar change). Judgements use only those values."""
    cover = observed.get("cover"); temp = observed.get("surface_c"); p95 = observed.get("surface_p95_c")
    median = (context or {}).get("surface_median_c")
    warm = temp is not None and median is not None and temp >= median + 2
    cool = temp is not None and median is not None and temp <= median - 2
    reasons: list[str] = []
    verdict = "inconclusive"
    if hypothesis == "water":
        if cover in ("Permanent water bodies", "Herbaceous wetland"):
            verdict, reasons = "supports", [f"WorldCover maps {cover.lower()} here"]
        elif cool:
            verdict, reasons = "supports", [f"surface {temp:.0f} °C is cooler than the area's median {median:.0f} °C, as water is by day"]
        elif warm:
            verdict, reasons = "contradicts", [f"surface {temp:.0f} °C is warmer than the area's median {median:.0f} °C; open water is cooler by day"]
    elif hypothesis == "vegetation_loss":
        if cover in VEGETATED and warm:
            verdict, reasons = "supports", [f"WorldCover mapped {cover.lower()} before the change, and the surface is now warmer than the area ({temp:.0f} vs {median:.0f} °C), as cleared or burned ground is"]
        elif cover in VEGETATED:
            reasons = [f"WorldCover mapped {cover.lower()} before the change; the surface temperature does not stand out"]
        elif cover:
            verdict, reasons = "contradicts", [f"WorldCover mapped {cover.lower()} here before the change, not vegetation"]
    elif hypothesis == "vegetation_gain":
        if cover in VEGETATED and not warm:
            verdict, reasons = "supports", [f"WorldCover maps {cover.lower()} here and the surface is not warm"]
        elif cover in ("Built-up", "Permanent water bodies", "Snow and ice"):
            verdict, reasons = "contradicts", [f"WorldCover maps {cover.lower()} here"]
    elif hypothesis == "structure":
        if cover == "Built-up":
            verdict, reasons = "supports", ["WorldCover maps built-up land here"]
        elif cover in ("Herbaceous wetland", "Mangroves"):
            verdict, reasons = "supports", [f"WorldCover maps {cover.lower()}: water under vegetation can raise co-polarized return (double bounce)"]
    elif hypothesis in ("roughening", "smoothing"):
        if p95 is not None and temp is not None and temp >= p95:
            verdict, reasons = "supports", [f"surface {temp:.0f} °C is among the warmest 5% of the area: consistent with disturbed or newly deposited ground"]
    if not reasons:
        reasons = ["the measured values here neither support nor contradict it"]
    return {"verdict": verdict, "reasons": reasons}
