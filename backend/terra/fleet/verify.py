"""From radar change to field verification and back.

dispatch: each changed patch of a finished investigation becomes a fleet target that carries a brief: its
polarimetric hypothesis, the question a drone should answer, the sensors that help, and the patch's extent.
evidence: the drones' field notes inside each patch are judged against that hypothesis (signatures.judge) and
summarised per patch: how many observations, by which aircraft, supporting / contradicting / inconclusive, and why.
Judgements use only measured values (ESA WorldCover before the change, Landsat surface temperature); a verdict is
evidence for a person to weigh, never a classification.
"""
from __future__ import annotations

import math

from ..signatures import judge

WORDS = {"water": "new water?", "vegetation_loss": "vegetation loss?", "vegetation_gain": "vegetation growth?", "structure": "structures or debris?",
         "roughening": "disturbed ground?", "smoothing": "smoother or drier ground?", "brighter": "brighter return", "darker": "darker return", "unclear": "unclear change"}


def _metres(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    return math.hypot((lon1 - lon2) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2)), (lat1 - lat2) * 110_574)


def change_of(result: dict) -> dict:
    m = (result or {}).get("measurements") or {}
    return m.get("change") or (result or {}).get("change") or {}


def targets_from(result: dict, sensors_available: set[str]) -> list[dict]:
    """Fleet targets for every changed patch, most decisive first, each with its verification brief."""
    out = []
    from ..signatures import signature
    for spot in change_of(result).get("hotspots") or []:
        sig = spot.get("signature") or signature(spot.get("by_polarization") or {}, spot["direction"])
        lat, lon = spot["centre"]
        w, s, e, n = spot["bounds"]
        radius = min(3000.0, max(150.0, _metres(w, s, e, n) / 2))
        weight = {"strong": 1.0, "moderate": 0.85, "weak": 0.65}.get(sig.get("confidence"), 0.6)
        # A target requires only a sensor the fleet has and the check really needs; every aircraft carries EO.
        needs = [x for x in sig.get("needs", []) if x != "eo" and x in sensors_available][:1]
        out.append({"id": f"patch-{spot['rank']}", "kind": "change", "lon": lon, "lat": lat, "radius_m": round(radius),
                    "label": f"Patch {spot['rank']} · {WORDS.get(sig.get('hypothesis'), 'change')}",
                    "priority": round(max(0.3, weight - 0.05 * (spot["rank"] - 1)), 3), "needs": needs,
                    "brief": {"rank": spot["rank"], "direction": spot["direction"], "hypothesis": sig.get("hypothesis"), "question": sig.get("question"),
                              "consistent_with": sig.get("consistent_with", []), "mechanism": sig.get("mechanism"), "basis": sig.get("basis"),
                              "confidence": sig.get("confidence"), "sensors": sig.get("needs", []), "bounds": spot["bounds"],
                              "mean_change_db": spot.get("mean_change_db"), "area_km2": spot.get("area_km2")}})
    return out


def evidence(result: dict, notes: list[dict], context: dict | None = None) -> list[dict]:
    """Per patch: the field notes that looked inside it, each judged against the patch's hypothesis."""
    rows = []
    for t in targets_from(result, {"ir", "lidar", "multispectral", "gas"}):
        b = t["brief"]; w, s, e, n = b["bounds"]
        pad = 0.15 * max(e - w, n - s) + 0.002
        inside = [x for x in notes if w - pad <= x["lon"] <= e + pad and s - pad <= x["lat"] <= n + pad]
        judged = []
        for x in sorted(inside, key=lambda q: -q["t"]):
            data = x.get("data") or {}
            j = judge(b["hypothesis"] or "unclear", data, {**(context or {}), **({"surface_median_c": data["surface_median_c"]} if data.get("surface_median_c") is not None else {})})
            judged.append({"note_id": x["id"], "t": x["t"], "drone_id": x["drone_id"], "lon": x["lon"], "lat": x["lat"], "verdict": j["verdict"], "alternative": j.get("alternative"),
                           "reasons": j["reasons"], "surface_c": (x.get("data") or {}).get("surface_c"), "cover": (x.get("data") or {}).get("cover")})
        counts = {v: sum(1 for j in judged if j["verdict"] == v) for v in ("supports", "contradicts", "inconclusive")}
        decided = counts["supports"] + counts["contradicts"]
        sup, con, total = counts["supports"], counts["contradicts"], len(judged)
        alt = sum(1 for j in judged if j.get("alternative"))
        # A verdict needs a real share of the observations, not one lucky look; an alternative explanation that keeps
        # recurring is reported as such.
        overall = ("not yet observed" if not judged else
                   "alternative explanation" if alt >= max(2, 0.3 * total) and sup < 0.5 * total else
                   "supported" if sup >= max(1, 0.3 * total) and sup >= 2 * con else
                   "contradicted" if con >= max(1, 0.3 * total) and con >= 2 * sup else
                   "mixed" if sup and con else "inconclusive")
        rows.append({"target_id": t["id"], "label": t["label"], "lon": t["lon"], "lat": t["lat"], "brief": b, "observations": len(judged),
                     "aircraft": sorted({j["drone_id"] for j in judged}), "counts": counts, "overall": overall, "latest": judged[:5],
                     "alternatives": sorted({j["alternative"] for j in judged if j.get("alternative")})})
    return rows
