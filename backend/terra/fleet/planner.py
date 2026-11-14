"""Coordination rules for the fleet: jobs, bids, awards, airspace separation and health checks.

Pure functions over plain dictionaries, so they are deterministic and testable; the durable agents call them
through activities that read and write the central database.

The model is a contract-net auction. The coordinator turns monitoring targets into jobs. Each drone's agent
bids on every job with an estimated cost (time to arrive plus penalties for low battery or a missing sensor)
and says whether it can do the job and still get home with a reserve. The coordinator awards jobs by priority
to the cheapest feasible bidder, keeps current assignments unless a bid is clearly better, then gives every
aircraft whose airspace overlaps another its own altitude layer. Strategic separation follows the
operation-volume idea of NASA's UAS Traffic Management research: each task reserves a volume (an area, an
altitude band and a time window) and overlapping volumes may not share altitudes.
"""
from __future__ import annotations

import math
import zlib

EARTH = 6_371_000.0
RESERVE_PCT = 15.0          # battery kept in hand after returning
LOW_MARGIN_PCT = 8.0        # start home this far above the bare minimum
STATION_S = 300.0           # planning horizon on a job (re-planned continuously), seconds
STICKY_S = 150.0            # a better bid must beat the current holder by this much to take a job over
QUAD_LAYERS = [110, 150, 190, 230, 270, 310]       # metres above ground
WING_LAYERS = [430, 500, 570]
SEPARATION_M = 40.0         # vertical separation between overlapping volumes
PROXIMITY_H_M, PROXIMITY_V_M = 60.0, 25.0
LINK_FLOOR_DBM = -92.0
RELAY_RANGE_M = 6000.0
OUT_OF_AREA_M = 40_000.0    # aircraft farther than this from the launch site are not part of this operation


# ---------------------------------------------------------------- geometry
def distance(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH * math.asin(min(1.0, math.sqrt(a)))


def offset(lon: float, lat: float, east_m: float, north_m: float) -> tuple[float, float]:
    return lon + math.degrees(east_m / (EARTH * math.cos(math.radians(lat)))), lat + math.degrees(north_m / EARTH)


def _bbox_m(bbox: list[float]) -> tuple[float, float]:
    w, s, e, n = bbox
    return distance(w, (s + n) / 2, e, (s + n) / 2), distance(w, s, w, n)


def footprint(geometry: dict) -> tuple[float, float, float]:
    """A circle (lon, lat, radius in metres) enclosing an operation volume's area, for overlap tests."""
    if geometry["type"] == "circle":
        return geometry["lon"], geometry["lat"], geometry["radius_m"]
    w, s, e, n = geometry["bbox"]
    cx, cy = (w + e) / 2, (s + n) / 2
    return cx, cy, distance(cx, cy, e, n)


def overlaps(a: dict, b: dict) -> bool:
    ax, ay, ar = footprint(a); bx, by, br = footprint(b)
    if a["type"] == "box" and b["type"] == "box":
        aw, as_, ae, an = a["bbox"]; bw, bs, be, bn = b["bbox"]
        return aw < be and bw < ae and as_ < bn and bs < an
    return distance(ax, ay, bx, by) < ar + br


# ---------------------------------------------------------------- energy
def drain_per_s(drone: dict) -> float:
    minutes = float(drone.get("capabilities", {}).get("endurance_min") or 30)
    return 100.0 / (max(5.0, minutes) * 60.0)


def cruise(drone: dict) -> float:
    return float(drone.get("capabilities", {}).get("cruise_mps") or (20 if drone.get("aircraft") == "fixed-wing" else 10))


def transit(drone: dict) -> float:
    """Speed for getting somewhere (faster than working speed on station)."""
    caps = drone.get("capabilities", {})
    return max(cruise(drone), 0.85 * float(caps.get("max_mps") or cruise(drone) * 1.5))


def battery_needed_home(drone: dict, launch: list[float]) -> float:
    s = drone.get("state", {})
    if "lon" not in s:
        return RESERVE_PCT
    return distance(s["lon"], s["lat"], launch[0], launch[1]) / transit(drone) * drain_per_s(drone) + RESERVE_PCT


# ---------------------------------------------------------------- jobs
def build_jobs(area: dict, targets: list[dict], drones: list[dict]) -> list[dict]:
    """Turn monitoring targets into jobs, most important first."""
    bbox, launch = area["bbox"], area["launch"]
    width, height = _bbox_m(bbox)
    jobs: list[dict] = []
    # Search sectors (search-and-rescue profile): an expanding-square search from each sector's datum.
    for t in [x for x in targets if x.get("kind") == "search"][:5]:
        jobs.append({"key": f"search:{t['id']}", "kind": "search", "label": f"Search {t['label']}", "priority": round(0.6 + 0.4 * t["priority"], 3),
                     "needs": t.get("needs", []), "target_id": t["id"],
                     "params": {"lon": t["lon"], "lat": t["lat"], "size_m": round(min(4000.0, t["radius_m"] * 2)), "lane_m": 120, "agl_m": 90, "speed_mps": 8}})
    targets = [x for x in targets if x.get("kind") != "search"]
    for t in sorted(targets, key=lambda x: -x["priority"])[:4]:
        agl = min(300.0, max(120.0, t["radius_m"] * 0.45))
        radius = max(120.0, agl / math.tan(math.radians(40)))  # camera looks down at about 40 degrees
        jobs.append({"key": f"orbit:{t['id']}", "kind": "orbit", "label": f"Orbit {t['label']}", "priority": round(0.6 + 0.4 * t["priority"], 3),
                     "needs": t.get("needs", []), "target_id": t["id"],
                     "params": {"lon": t["lon"], "lat": t["lat"], "radius_m": round(radius), "agl_m": round(agl), "speed_mps": 9, "target": t["label"]}})
    if targets:
        top = max(targets, key=lambda x: x["priority"])
        half = min(1500.0, max(500.0, top["radius_m"] * 1.3))
        w, s = offset(top["lon"], top["lat"], -half, -half); e, n = offset(top["lon"], top["lat"], half, half)
        jobs.append({"key": f"survey:{top['id']}", "kind": "survey", "label": f"Survey around {top['label']}", "priority": 0.5, "needs": [], "target_id": top["id"],
                     "params": {"bbox": [round(w, 6), round(s, 6), round(e, 6), round(n, 6)], "agl_m": 200, "speed_mps": 11, "lane_m": round(half / 3)}})
    if max(width, height) > 4000:
        jobs.append({"key": "perimeter", "kind": "perimeter", "label": "Map the perimeter", "priority": 0.55, "needs": [], "prefers": "fixed-wing",
                     "params": {"bbox": bbox, "agl_m": 430, "speed_mps": 22, "inset": 0.08}})
    # A relay when anyone flies beyond direct radio range of the ground station, or reports a weak link.
    far = [d for d in drones if d.get("link_state") == "online" and "lon" in d.get("state", {}) and (
        distance(d["state"]["lon"], d["state"]["lat"], launch[0], launch[1]) > RELAY_RANGE_M or (d["state"].get("link_dbm") or 0) < LINK_FLOOR_DBM)]
    if far or max(width, height) > 2 * RELAY_RANGE_M:
        ref = max(far, key=lambda d: distance(d["state"]["lon"], d["state"]["lat"], launch[0], launch[1])) if far else None
        px, py = ((ref["state"]["lon"] + launch[0]) / 2, (ref["state"]["lat"] + launch[1]) / 2) if ref else ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        # Urgent when someone is out of range or weak; precautionary over a large area.
        jobs.append({"key": "relay", "kind": "relay", "label": "Radio relay", "priority": 0.75 if far else 0.45, "needs": ["relay"],
                     "params": {"lon": round(px, 6), "lat": round(py, 6), "agl_m": 300, "radius_m": 80, "speed_mps": 7}})
    else:
        jobs.append({"key": "overwatch", "kind": "relay", "label": "Overwatch the centre", "priority": 0.35, "needs": [],
                     "params": {"lon": (bbox[0] + bbox[2]) / 2, "lat": (bbox[1] + bbox[3]) / 2, "agl_m": 290, "radius_m": 250, "speed_mps": 7}})
    return jobs


def search_points(params: dict) -> list[tuple[float, float]]:
    """An expanding-square search from a datum (the standard pattern for a small search area): legs of one, one,
    two, two, three, three … lane spacings, turning right each time, until the square reaches the search size."""
    lon, lat = params["lon"], params["lat"]; lane = float(params.get("lane_m", 120)); size = float(params.get("size_m", 1200))
    pts = [(lon, lat)]; x = y = 0.0; heading = 0; leg = 1
    while max(abs(x), abs(y)) * 2 < size and len(pts) < 80:
        for _ in range(2):
            dx, dy = [(0, 1), (1, 0), (0, -1), (-1, 0)][heading % 4]
            x += dx * leg * lane; y += dy * leg * lane; heading += 1
            pts.append(offset(lon, lat, x, y))
        leg += 1
    return pts


def job_entry(job: dict) -> tuple[float, float]:
    p = job["params"]
    if "bbox" in p:
        w, s, e, n = p["bbox"]; return (w + e) / 2, (s + n) / 2
    return p["lon"], p["lat"]


# ---------------------------------------------------------------- bids (each drone agent computes its own)
def bid(drone: dict, job: dict, launch: list[float], current_job: str | None) -> dict:
    s = drone.get("state", {}); caps = drone.get("capabilities", {})
    if drone.get("link_state") != "online" or "lon" not in s:
        return {"job_key": job["key"], "cost": 1e9, "feasible": False, "detail": {"reason": "no recent telemetry"}}
    if job["kind"] not in caps.get("tasks", [job["kind"]]):
        return {"job_key": job["key"], "cost": 1e9, "feasible": False, "detail": {"reason": f"its ground station cannot fly {job['kind']} tasks"}}
    missing = [n for n in job.get("needs", []) if n not in caps.get("sensors", []) and not (n == "relay" and caps.get("relay"))]
    if missing:
        return {"job_key": job["key"], "cost": 1e9, "feasible": False, "detail": {"reason": f"lacks {', '.join(missing)}"}}
    ex, ey = job_entry(job)
    to_job = distance(s["lon"], s["lat"], ex, ey); home = distance(ex, ey, launch[0], launch[1])
    eta = to_job / transit(drone)
    need = (eta + STATION_S + home / transit(drone)) * drain_per_s(drone) + RESERVE_PCT
    battery = float(s.get("battery_pct") if s.get("battery_pct") is not None else 100)
    feasible = battery >= need
    cost = eta + (100 - battery) * 4
    if job.get("prefers") and job["prefers"] != drone.get("aircraft"):
        cost += 400
    if drone.get("aircraft") == "fixed-wing" and job["kind"] in ("orbit", "relay") and job.get("prefers") != "fixed-wing":
        cost += 250  # wide turns: quads hold a point better
    if current_job == job["key"]:
        cost -= STICKY_S
    return {"job_key": job["key"], "cost": round(cost, 1), "feasible": feasible,
            "detail": {"eta_s": round(eta), "battery_needed": round(need, 1), "battery": round(battery, 1)}}


# ---------------------------------------------------------------- awards
def award(jobs: list[dict], bids: list[dict], available: list[str], holders: dict[str, str]) -> tuple[dict[str, str], list[dict]]:
    """Assign aircraft to jobs to maximise the total priority covered, then minimise the total cost.

    An exhaustive search over the most important jobs (fleets of a few aircraft), so a scarce capability such as a
    relay radio is not spent on a job any aircraft could do. Larger fleets fall back to greedy by priority.
    Returns {drone: job} and notes for hand-offs and unfilled jobs."""
    offers: dict[str, dict[str, float]] = {}
    for b in bids:
        if b["feasible"] and b["drone_id"] in available:
            offers.setdefault(b["job_key"], {})[b["drone_id"]] = min(b["cost"], offers.get(b["job_key"], {}).get(b["drone_id"], 1e18))
    ranked = sorted(jobs, key=lambda j: -j["priority"])
    value = lambda job, cost: job["priority"] * 10_000 - cost  # noqa: E731 - priority dominates, cost breaks ties
    best: dict = {"score": -1e18, "plan": {}}
    if len(ranked) <= 9 and len(available) <= 9:
        def search(i: int, used: frozenset, score: float, plan: dict) -> None:
            if score + sum(j["priority"] * 10_000 for j in ranked[i:]) <= best["score"]:
                return
            if i == len(ranked):
                best.update(score=score, plan=dict(plan)); return
            job = ranked[i]
            for drone, cost in sorted(offers.get(job["key"], {}).items(), key=lambda kv: kv[1]):
                if drone not in used:
                    plan[drone] = job["key"]; search(i + 1, used | {drone}, score + value(job, cost), plan); del plan[drone]
            search(i + 1, used, score, plan)
        search(0, frozenset(), 0.0, {})
        out = best["plan"]
    else:
        out, used = {}, set()
        for job in ranked:
            choice = sorted((c, d) for d, c in offers.get(job["key"], {}).items() if d not in used)
            if choice:
                out[choice[0][1]] = job["key"]; used.add(choice[0][1])
    notes: list[dict] = []
    assigned = {k: d for d, k in out.items()}
    for job in ranked:
        winner = assigned.get(job["key"])
        if not winner:
            notes.append({"kind": "unfilled", "job": job["key"], "text": f"No aircraft free for “{job['label']}” now."})
        elif holders.get(job["key"]) and holders[job["key"]] != winner:
            notes.append({"kind": "handoff", "job": job["key"], "from": holders[job["key"]], "to": winner, "text": f"{winner} takes over “{job['label']}” from {holders[job['key']]}."})
    return out, notes


# ---------------------------------------------------------------- airspace
def volume_for(kind: str, params: dict, agl: float) -> dict:
    if kind == "search":
        geometry = {"type": "circle", "lon": params["lon"], "lat": params["lat"], "radius_m": float(params.get("size_m", 1200)) * 0.75 + 60}
    elif kind in ("orbit", "relay", "hold", "standby"):
        geometry = {"type": "circle", "lon": params["lon"], "lat": params["lat"], "radius_m": float(params.get("radius_m", 60)) + 60}
    elif kind == "return":
        geometry = {"type": "circle", "lon": params["lon"], "lat": params["lat"], "radius_m": 150.0}
    else:
        w, s, e, n = params["bbox"]; geometry = {"type": "box", "bbox": [w, s, e, n]}
    return {"geometry": geometry, "lower": max(0.0, agl - 20), "upper": agl + 20}


def deconflict(plans: list[dict]) -> list[dict]:
    """Give overlapping operation volumes different altitude layers. Plans are dicts with drone, kind, params,
    aircraft and priority; returns them with `agl_m` set and a list of what was moved in `moved`."""
    placed: list[dict] = []
    for p in sorted(plans, key=lambda x: -x["priority"]):
        layers = WING_LAYERS if p["aircraft"] == "fixed-wing" else QUAD_LAYERS
        wanted = float(p["params"].get("agl_m", layers[0]))
        options = [wanted] + sorted(layers, key=lambda h: abs(h - wanted))
        chosen = wanted
        for h in options:
            vol = volume_for(p["kind"], p["params"], h)
            clash = [q for q in placed if overlaps(vol["geometry"], q["volume"]["geometry"]) and abs(q["agl_m"] - h) < SEPARATION_M]
            if not clash:
                chosen = h; break
        p = {**p, "agl_m": chosen, "volume": volume_for(p["kind"], p["params"], chosen), "moved": chosen != wanted}
        placed.append(p)
    return placed


# ---------------------------------------------------------------- health and conformance (drone agents)
def assess(drone: dict, task: dict | None, operation: dict | None, others: list[dict], launch: list[float]) -> list[dict]:
    """What a drone's agent should report: lost contact, low battery, weak link, leaving its volume, traffic nearby."""
    found: list[dict] = []
    if drone.get("link_state") == "lost":
        return [{"kind": "lost", "urgent": True, "text": f"No telemetry from {drone['name']} for {drone.get('age_s', '?')} s; its job is released. The autopilot's own failsafe applies."}]
    if drone.get("link_state") == "stale":
        found.append({"kind": "stale", "urgent": False, "text": f"Telemetry from {drone['name']} is {drone.get('age_s', '?')} s old."})
    s = drone.get("state", {})
    if "lon" not in s:
        return found
    battery = s.get("battery_pct")
    if battery is not None and (task or {}).get("kind") != "return" and not s.get("landed"):
        need = battery_needed_home(drone, launch)
        if battery < need + LOW_MARGIN_PCT:
            found.append({"kind": "battery", "urgent": True, "text": f"{drone['name']} has {battery:.0f}% and needs about {need:.0f}% to get home: requesting relief."})
    if s.get("link_dbm") is not None and s["link_dbm"] < LINK_FLOOR_DBM:
        found.append({"kind": "link", "urgent": True, "text": f"{drone['name']} link is weak ({s['link_dbm']:.0f} dBm): asking for a relay."})
    if task and operation and task.get("status") == "active":
        lon, lat, r = footprint(operation["geometry"])
        off = distance(s["lon"], s["lat"], lon, lat) - r
        agl = s.get("agl_m")
        if off > 150 or (agl is not None and not operation["alt_lower_m"] - 25 <= agl <= operation["alt_upper_m"] + 25):
            found.append({"kind": "nonconforming", "urgent": True, "text": f"{drone['name']} is outside its reserved airspace ({max(0, off):.0f} m out, {agl or 0:.0f} m above ground)."})
    # Traffic: the nearest aircraft closer than the separation minimums. Departures and arrivals near the launch
    # site are sequenced on the ground, so low traffic there is not an alert.
    near_pad = lambda st: distance(st["lon"], st["lat"], launch[0], launch[1]) < 250 and (st.get("agl_m") or 0) < 100  # noqa: E731
    closest = None
    for o in others:
        os_ = o.get("state", {})
        if o["id"] == drone["id"] or o.get("link_state") != "online" or "lon" not in os_ or s.get("landed") or os_.get("landed"):
            continue
        if near_pad(s) or near_pad(os_):
            continue
        h = distance(s["lon"], s["lat"], os_["lon"], os_["lat"]); v = abs((s.get("agl_m") or 0) - (os_.get("agl_m") or 0))
        if h < PROXIMITY_H_M and v < PROXIMITY_V_M and (closest is None or h < closest[0]):
            closest = (h, o)
    if closest:
        found.append({"kind": "proximity", "urgent": True, "text": f"{drone['name']} is {closest[0]:.0f} m from {closest[1]['name']} at similar height."})
    return found


# ---------------------------------------------------------------- one coordination round
def plan_round(area: dict, targets: list[dict], drones: list[dict], tasks: list[dict], bids: list[dict], controlled: set[str], now: float,
               refused: set[tuple[str, str]] = frozenset(), benched: set[str] = frozenset()) -> dict:
    """Decide jobs, awards, tasks and airspace for this round. Returns plain decisions to apply."""
    launch = area["launch"]
    # Aircraft far from this operation (another area, another country) are left alone: not tasked, not sent home.
    reach = max(OUT_OF_AREA_M, 2 * max(_bbox_m(area["bbox"])))
    away = {d["id"] for d in drones if "lon" in d.get("state", {}) and distance(d["state"]["lon"], d["state"]["lat"], launch[0], launch[1]) > reach}
    drones = [d for d in drones if d["id"] not in away]
    tasks = [t for t in tasks if t["drone_id"] not in away]
    online = [d for d in drones if d.get("link_state") == "online"]
    current = {t["drone_id"]: t for t in tasks}
    jobs = build_jobs(area, targets, drones)
    job_by_key = {j["key"]: j for j in jobs}
    decisions: dict = {"jobs": jobs, "tasks": [], "retarget": [], "messages": [], "operations": [], "released": [], "away": sorted(away)}
    for drone_id in sorted(away):
        decisions["messages"].append({"kind": "out-of-area", "text": f"{drone_id} is more than {reach / 1000:.0f} km from this operation; it is not tasked."})

    # People's tasks and manual flight take precedence until they end.
    pinned = {d for d, t in current.items() if t["created_by"] == "operator" and (t.get("hold_until") or 0) > now} | set(controlled)
    # Aircraft that must go home now (battery), or that no longer answer.
    returning: set[str] = set()
    for d in online:
        t = current.get(d["id"]); s = d.get("state", {})
        if d["id"] in pinned or s.get("landed"):
            continue
        b = s.get("battery_pct")
        if b is not None and (t or {}).get("kind") != "return" and b < battery_needed_home(d, launch) + LOW_MARGIN_PCT:
            decisions["tasks"].append({"drone_id": d["id"], "kind": "return", "label": "Return to launch", "job_key": None, "priority": 1.0,
                                       "params": {"lon": launch[0], "lat": launch[1], "agl_m": 90, "speed_mps": transit(d)},
                                       "reason": f"Battery {b:.0f}%: returning with reserve."})
            returning.add(d["id"])
        elif (t or {}).get("kind") == "return":
            returning.add(d["id"])  # stays on its way home until it lands
    unresponsive: set[str] = set()
    for t in tasks:
        d = next((x for x in drones if x["id"] == t["drone_id"]), None)
        if d and d.get("link_state") == "lost":
            decisions["released"].append(t["id"])
        # A real aircraft that never acknowledged its task is not counted on, and is not re-tasked this round.
        elif d and d["origin"] in ("real", "sitl") and t["status"] == "assigned" and t["created_by"] != "operator" and now - t["created_at"] > 30:
            decisions["released"].append(t["id"]); unresponsive.add(d["id"])
            decisions["messages"].append({"kind": "unacknowledged", "text": f"{d['name']} did not acknowledge “{t['label']}” within 30 s; the job is reopened."})

    # Auction among aircraft free to take work. Agents' bids are used when fresh; for any silent agent the
    # coordinator bids on its behalf from the database so the fleet never stalls.
    for drone_id in sorted(benched):
        if any(d["id"] == drone_id for d in online):
            decisions["messages"].append({"kind": "benched", "text": f"{drone_id} rejected its recent tasks; not tasked for two minutes, then tried again."})
    available = [d["id"] for d in online if d["id"] not in pinned and d["id"] not in returning and d["id"] not in unresponsive and d["id"] not in benched and not (d.get("state", {}).get("landed") and (d.get("state", {}).get("battery_pct") or 100) < 60)]
    fresh = {(b["drone_id"], b["job_key"]) for b in bids}
    # A job an aircraft refused recently is not offered to it again for a while.
    all_bids = [b for b in bids if b["job_key"] in job_by_key and (b["drone_id"], b["job_key"]) not in refused]
    for d in online:
        if d["id"] not in available:
            continue
        held = (current.get(d["id"]) or {}).get("job_key")
        for j in jobs:
            if (d["id"], j["key"]) not in fresh and (d["id"], j["key"]) not in refused:
                all_bids.append({**bid(d, j, launch, held), "drone_id": d["id"], "by": "coordinator"})
    holders = {t["job_key"]: t["drone_id"] for t in tasks if t.get("job_key") and t["id"] not in decisions["released"]}
    awards, notes = award(jobs, all_bids, available, holders)
    decisions["messages"].extend(notes)

    # Airspace: every flying aircraft reserves a volume; overlapping volumes get different altitude layers.
    plans = []
    for drone_id, key in awards.items():
        j = job_by_key[key]; d = next(x for x in drones if x["id"] == drone_id)
        plans.append({"drone_id": drone_id, "job_key": key, "kind": j["kind"], "label": j["label"], "params": dict(j["params"]), "priority": j["priority"], "aircraft": d["aircraft"]})
    for d in online:
        if d["id"] in pinned and current.get(d["id"]):
            t = current[d["id"]]
            plans.append({"drone_id": d["id"], "job_key": t.get("job_key"), "kind": t["kind"], "label": t["label"], "params": dict(t["params"]), "priority": 1.1, "aircraft": d["aircraft"], "keep": True})
    placed = deconflict(plans)
    for p in placed:
        params = {**p["params"], "agl_m": p["agl_m"]}
        held = current.get(p["drone_id"])
        if p.get("keep"):
            pass
        elif held and held.get("job_key") == p["job_key"] and held["status"] in ("assigned", "acknowledged", "active") and held["id"] not in decisions["released"]:
            if held["params"].get("agl_m") != p["agl_m"] or held["params"].get("lon") != params.get("lon") or held["params"].get("lat") != params.get("lat"):
                decisions["retarget"].append({"task_id": held["id"], "params": params})
        else:
            j = job_by_key[p["job_key"]]
            previous = holders.get(j["key"])
            if previous and previous != p["drone_id"]:
                params["relieves"] = previous  # the newcomer tells the drone it relieves when it is on station
            decisions["tasks"].append({"drone_id": p["drone_id"], "kind": j["kind"], "label": j["label"], "job_key": j["key"], "priority": j["priority"], "params": params,
                                       "reason": "Awarded at auction" + (f"; moved to {p['agl_m']:.0f} m to stay clear of other aircraft" if p["moved"] else "")})
        if p["moved"]:
            decisions["messages"].append({"kind": "separation", "text": f"{p['drone_id']} assigned {p['agl_m']:.0f} m above ground to keep {SEPARATION_M:.0f} m from overlapping airspace."})
        decisions["operations"].append({"drone_id": p["drone_id"], "job_key": p["job_key"], "volume": p["volume"]})
    # Aircraft left without work loiter low near the launch site, ready to be called.
    for d in online:
        if d["id"] in available and d["id"] not in awards and (current.get(d["id"]) or {}).get("kind") != "standby" and not d.get("state", {}).get("landed"):
            angle = math.radians(zlib.crc32(d["id"].encode()) % 360)  # a stable slot per aircraft around the launch site
            px, py = offset(launch[0], launch[1], 120 * math.cos(angle), 120 * math.sin(angle))
            decisions["tasks"].append({"drone_id": d["id"], "kind": "standby", "label": "Standby near launch", "job_key": None, "priority": 0.1,
                                       "params": {"lon": px, "lat": py, "agl_m": 60, "radius_m": 40, "speed_mps": 6}, "reason": "No job to take right now."})
    for key in awards.values():
        job_by_key[key]["assigned_drone"] = next(d for d, k in awards.items() if k == key)
    return decisions
