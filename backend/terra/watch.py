"""Area watch: a durable agent per area that waits for NISAR to pass, finds the new acquisition, measures the change
against the previous one and, when the change is significant, proposes a drone dispatch for an operator to approve.

This module holds the pure parts (NISAR swath prediction, choosing a comparable acquisition, judging significance),
the watch read model (a table in the same SQLite file) and the Temporal activities. The loop itself is
`watch_workflows.AreaWatchWorkflow`; the HTTP surface is `watch_api`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from uuid import uuid4

from temporalio import activity
from temporalio.exceptions import ApplicationError

R_EARTH_KM = 6371.0
# NISAR's L-band SAR images the ground at 33-47 degrees incidence, looking left: from ~747 km that is roughly
# 420-680 km from the ground track (the same geometry the workspace uses for "next NISAR pass").
SWATH_NEAR_KM, SWATH_FAR_KM = 420.0, 680.0
NISAR_NORAD = 65053
# Products are published hours to days after a pass; the watch starts looking this long after the predicted pass.
DATA_LATENCY = timedelta(hours=12)


# ------------------------------------------------------------------ orbit geometry (sgp4, TEME -> Earth-fixed)
def _gmst(jd_ut1: float) -> float:
    """Greenwich mean sidereal time in radians (IAU 1982, as used by sgp4/satellite.js)."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    seconds = -6.2e-6 * t ** 3 + 0.093104 * t ** 2 + (876600.0 * 3600 + 8640184.812866) * t + 67310.54841
    return (seconds * math.pi / 180.0 / 240.0) % (2 * math.pi)


def sub_point(sat, when: datetime) -> tuple[float, float] | None:
    """Geodetic latitude and longitude (degrees) of the point beneath the satellite."""
    from sgp4.api import jday
    jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute, when.second + when.microsecond / 1e6)
    error, r, _ = sat.sgp4(jd, fr)
    if error != 0:
        return None
    g = _gmst(jd + fr)
    x = r[0] * math.cos(g) + r[1] * math.sin(g); y = -r[0] * math.sin(g) + r[1] * math.cos(g); z = r[2]
    a, e2 = 6378.137, 0.00669437999014
    lon = math.atan2(y, x); p = math.hypot(x, y); lat = math.atan2(z, p * (1 - e2))
    for _ in range(4):  # Bowring-style iteration to geodetic latitude
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * n * math.sin(lat), p)
    return math.degrees(lat), (math.degrees(lon) + 540) % 360 - 180


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2); dp = p2 - p1; dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2); dl = math.radians(lon2 - lon1)
    return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360


def next_nisar_pass(line1: str, line2: str, lat: float, lon: float, start: datetime, days: float = 13) -> datetime | None:
    """The next time the point falls inside NISAR's left-looking swath (closest approach of that pass), or None.
    NISAR repeats its ground track every 12 days, so 13 days always contains at least one imaging pass."""
    from sgp4.api import Satrec
    sat = Satrec.twoline2rv(line1, line2)
    t = start; end = start + timedelta(days=days)
    prev = prev_prev = math.inf; prev_t = start
    while t <= end:
        p = sub_point(sat, t)
        if p is None:
            t += timedelta(seconds=60); continue
        d = distance_km(lat, lon, p[0], p[1])
        if prev < prev_prev and prev <= d and SWATH_NEAR_KM <= prev <= SWATH_FAR_KM:
            a, b = sub_point(sat, prev_t), sub_point(sat, prev_t + timedelta(seconds=10))
            if a and b:
                side = (_bearing(a[0], a[1], lat, lon) - _bearing(a[0], a[1], b[0], b[1]) + 540) % 360 - 180
                if side < 0:  # the area is left of the track: inside the imaged swath
                    return prev_t
        prev_prev, prev, prev_t = prev, d, t
        # Far from the area, skip ahead in proportion to the distance (ground speed ~7 km/s).
        if d > SWATH_FAR_KM + 1500:
            t += timedelta(seconds=min(600.0, (d - SWATH_FAR_KM - 1000) / 7)); prev = prev_prev = math.inf
        else:
            t += timedelta(seconds=15)
    return None


# ------------------------------------------------------------------ choosing what to compare and judging it
def _date(item: dict) -> str:
    return str(item.get("date") or "")


def pick_new_acquisition(observations: list[dict], reference: dict | None) -> dict | None:
    """The newest GCOV acquisition after the reference with the same viewing geometry (track and direction, and the
    frame when both have one): radar change is only meaningful between like-for-like looks."""
    if not reference:
        return None
    same = [o for o in observations if o.get("product") == "GCOV" and _date(o) > _date(reference)
            and o.get("track") == reference.get("track") and o.get("direction") == reference.get("direction")
            and (reference.get("frame") is None or o.get("frame") is None or o.get("frame") == reference.get("frame"))]
    return max(same, key=_date) if same else None


def latest_acquisition(observations: list[dict]) -> dict | None:
    gcov = [o for o in observations if o.get("product") == "GCOV"]
    return max(gcov, key=_date) if gcov else None


def assess_change(investigation: dict, min_changed_pct: float = 0.5, min_patch_km2: float = 0.1) -> dict:
    """Is the measured change worth sending drones for? Significant when the share of the area beyond the automatic
    thresholds reaches `min_changed_pct`, or any changed patch reaches `min_patch_km2`."""
    result = investigation.get("result") or {}
    change = (result.get("measurements") or {}).get("change") or {}
    pct = change.get("threshold_exceedance_pct")
    spots = change.get("hotspots") or []
    biggest = max(spots, key=lambda s: s.get("area_km2") or 0, default=None)
    status = investigation.get("status")
    if status != "completed" or pct is None:
        return {"significant": False, "status": status, "changed_pct": pct, "patches": len(spots),
                "summary": investigation.get("blocker") or "The comparison did not produce a change measurement."}
    big_km2 = (biggest or {}).get("area_km2") or 0
    significant = pct >= min_changed_pct or big_km2 >= min_patch_km2
    dates = sorted(str(o.get("date", ""))[:10] for o in ((result.get("measurements") or {}).get("observations") or []))[:2]
    when = f" between {dates[0]} and {dates[1]}" if len(dates) == 2 else ""
    largest = ""
    if biggest:
        db = biggest.get("mean_change_db")
        largest = f"; largest patch {big_km2:.2f} km²" + (f", {'brighter' if biggest.get('direction') == 'increase' else 'darker'} by {abs(db):.1f} dB" if db is not None else "")
    return {"significant": significant, "status": status, "changed_pct": round(pct, 2), "patches": len(spots), "largest_km2": round(big_km2, 3),
            "summary": f"{pct:.1f}% of the area changed beyond the automatic thresholds{when} ({len(spots)} patches{largest})."}


# ------------------------------------------------------------------ read model (same SQLite file as the rest of Terra)
def _db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10); db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS area_watches (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
    return db


def save_watch(path: Path, state: dict) -> None:
    with _db(path) as db:
        db.execute("INSERT INTO area_watches VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                   (state["id"], json.dumps(state, allow_nan=False), state.get("updated_at") or datetime.now(timezone.utc).isoformat()))


def list_watches(path: Path) -> list[dict]:
    with _db(path) as db:
        rows = db.execute("SELECT payload FROM area_watches ORDER BY updated_at DESC LIMIT 50").fetchall()
    return [json.loads(r["payload"]) for r in rows]


def get_watch(path: Path, identifier: str) -> dict | None:
    with _db(path) as db:
        row = db.execute("SELECT payload FROM area_watches WHERE id=?", (identifier,)).fetchone()
    return json.loads(row["payload"]) if row else None


# ------------------------------------------------------------------ activities
def _settings():
    from .config import get_settings
    return get_settings()


@activity.defn(name="watch_save")
async def watch_save(state: dict) -> None:
    save_watch(_settings().database, state)


@activity.defn(name="watch_next_pass")
async def watch_next_pass(payload: dict) -> dict:
    from . import feeds
    w, s, e, n = payload["bbox"]
    sats = await feeds.satellites()
    nisar = sats["nisar"]
    start = datetime.now(timezone.utc)
    at = next_nisar_pass(nisar["line1"], nisar["line2"], (s + n) / 2, (w + e) / 2, start)
    return {"at": at.isoformat() if at else None, "elements_epoch": nisar.get("element_epoch"), "source": sats.get("source")}


async def _catalog(bbox: list[float]) -> list[dict]:
    from .catalog import search_observations
    from .store import Store
    items = await search_observations(bbox, limit=40)
    Store(_settings().database).save_observations(items)  # investigations resolve observations from this table
    return items


@activity.defn(name="watch_reference")
async def watch_reference(payload: dict) -> dict | None:
    """The acquisition the watch compares against first: the later date of the investigation it was started from,
    otherwise the newest acquisition over the area."""
    from .store import Store
    store = Store(_settings().database)
    if payload.get("investigation_id"):
        item = store.investigation(payload["investigation_id"])
        ids = (item or {}).get("observation_ids") or []
        observations = [o for o in (store.observation(i) for i in ids) if o]
        if observations:
            return max(observations, key=_date)
    return latest_acquisition(await _catalog(payload["bbox"]))


@activity.defn(name="watch_find_new")
async def watch_find_new(payload: dict) -> dict | None:
    return pick_new_acquisition(await _catalog(payload["bbox"]), payload.get("reference"))


@activity.defn(name="watch_register_investigation")
async def watch_register_investigation(payload: dict) -> dict:
    """Create the change investigation between the reference and the new acquisition (same read model as the API)."""
    from .store import Store, utc_now
    reference, new = payload["reference"], payload["new"]
    now = utc_now(); identifier = str(uuid4())
    request = {"id": identifier, "title": f"{payload['name']} · watch", "bbox": payload["bbox"], "mode": "change", "auto": False,
               "question": f"What changed on {payload['name']} between the NISAR passes of {_date(reference)[:10]} and {_date(new)[:10]}?",
               "observation_ids": [reference["id"], new["id"]], "created_at": now, "updated_at": now, "watch_id": payload["watch_id"]}
    Store(_settings().database).save_investigation({**request, "status": "queued", "stage": "discover", "progress": 0, "events": [], "result": None, "blocker": None})
    return request


@activity.defn(name="watch_assess")
async def watch_assess(payload: dict) -> dict:
    from .store import Store
    item = Store(_settings().database).investigation(payload["investigation_id"])
    if not item:
        raise ApplicationError("The watch's investigation is missing from the store.", non_retryable=True)
    return assess_change(item, payload.get("min_changed_pct", 0.5))


@activity.defn(name="watch_dispatch")
async def watch_dispatch(payload: dict) -> dict:
    from .fleet.api import DispatchError, dispatch_investigation
    try:
        out = await dispatch_investigation(payload["investigation_id"])
    except DispatchError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    return {"targets": len(out["targets"]), "area": out["area"].get("name") if isinstance(out.get("area"), dict) else None, "coordinator": out["coordinator"]}


ACTIVITIES = [watch_save, watch_next_pass, watch_reference, watch_find_new, watch_register_investigation, watch_assess, watch_dispatch]
