"""From radar change to field verification and back.

dispatch: each changed patch of a finished investigation becomes a fleet target that carries a brief: its
polarimetric hypothesis, the question a drone should answer, the sensors that help, and the patch's extent.
evidence: the drones' field notes inside each patch are judged against that hypothesis (signatures.judge) and
summarised per patch: how many observations, by which aircraft, supporting / contradicting / inconclusive, and why.
Judgements use only measured values (ESA WorldCover before the change, Landsat surface temperature); a verdict is
evidence for a person to weigh, never a classification.
record / geojson: the evidence as kept with the investigation (its report shows it) and as a GeoJSON file of patches
and observation points.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone

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
                     "alternatives": sorted({j["alternative"] for j in judged if j.get("alternative")}),
                     # Every observation as a map point (lon, lat, verdict, note id), so the report can show where each look landed.
                     "points": [[round(j["lon"], 6), round(j["lat"], 6), j["verdict"], j["note_id"]] for j in judged],
                     "judged": judged})
    return rows


METHOD = ("Each field note inside a patch is judged against the patch's polarimetric hypothesis using only measured values "
          "(Sentinel-2 before and after the radar dates, ESA WorldCover 2021, Landsat surface temperature) at the ground point "
          "under the aircraft's camera. Evidence to weigh, not a classification, and not on-site imagery.")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fingerprint(rows: list[dict]) -> str:
    """What the evidence is made of (which notes, judged how): unchanged evidence is not stored again."""
    seen = sorted((r["target_id"], j["note_id"], j["verdict"]) for r in rows for j in r.get("judged", []))
    return hashlib.sha256(json.dumps(seen).encode()).hexdigest()[:16]


def public(rows: list[dict]) -> list[dict]:
    """The rows without the full observation lists (those go to the GeoJSON file)."""
    return [{k: v for k, v in r.items() if k != "judged"} for r in rows]


def record(rows: list[dict], aircraft: list[dict], artifact: str | None) -> dict:
    """The field evidence as kept with the investigation, so the report and its data keep it after the fleet moves on."""
    return {"recorded_at": _iso(time.time()), "fingerprint": fingerprint(rows), "observations": sum(r["observations"] for r in rows),
            "totals": {k: sum(r["counts"][k] for r in rows) for k in ("supports", "contradicts", "inconclusive")},
            "aircraft": aircraft, "patches": public(rows), "method": METHOD, "artifact": artifact}


def geojson(investigation_id: str, title: str, rows: list[dict], aircraft: list[dict]) -> dict:
    """Patches (polygons, with the field verdict) and every judged observation (points), for GIS tools."""
    origin = {a["id"]: a.get("origin") for a in aircraft}
    features = []
    for r in rows:
        b = r["brief"]; w, s, e, n = b["bounds"]
        features.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
                         "properties": {"kind": "patch", "patch": b["rank"], "direction": b["direction"], "hypothesis": b.get("hypothesis"),
                                        "consistent_with": ", ".join(b.get("consistent_with") or []), "question": b.get("question"),
                                        "mean_change_db": b.get("mean_change_db"), "area_km2": b.get("area_km2"), "field_verdict": r["overall"],
                                        "observations": r["observations"], **r["counts"], "aircraft": ", ".join(r["aircraft"])}})
        for j in r.get("judged", []):
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [j["lon"], j["lat"]]},
                             "properties": {"kind": "observation", "patch": b["rank"], "note_id": j["note_id"], "time": _iso(j["t"]),
                                            "aircraft": j["drone_id"], "aircraft_origin": origin.get(j["drone_id"]), "verdict": j["verdict"],
                                            "reasons": "; ".join(j["reasons"]), "alternative": j.get("alternative"),
                                            "surface_c": j.get("surface_c"), "land_cover": j.get("cover")}})
    return {"type": "FeatureCollection", "name": f"Terra field verification · {title}",
            "properties": {"investigation_id": investigation_id, "generated": _iso(time.time()), "method": METHOD}, "features": features}
