"""Activities for the durable fleet agents: each reads the central database, decides, and writes back."""
from __future__ import annotations

import asyncio
import time

from temporalio import activity

from .db import fleet_db
from .planner import assess, bid, distance, plan_round

AGENT_STALE_S = 45  # an agent's slowest cadence is 20 s (aircraft out of contact); twice that and it is restarted or woken
NOTE_EVERY_S = 120          # a field note on arrival, then every two minutes on station


def _elevation(lon: float, lat: float) -> float | None:
    from ..conditions import elevations
    try:
        return asyncio.run(elevations([(lon, lat)]))[0]
    except Exception:
        return None


def write_field_note(db, drone: dict, task: dict, now: float) -> dict | None:
    """Look where the camera points, read the real data there, and keep a geotagged note of it."""
    from .observe import area_data, ground_point, note_text, observe
    s = drone.get("state", {}); area = db.area()
    if not area or "lon" not in s:
        return None
    last = db.last_note(drone["id"], task["id"])
    if last and now - last["t"] < NOTE_EVERY_S:
        return None
    g = s.get("gimbal") or {}
    lon, lat = ground_point(s["lon"], s["lat"], float(s.get("agl_m") or 100), float(g.get("heading_deg", s.get("heading_deg", 0))), float(g.get("pitch_deg", -45)))
    o = observe(area_data(area["bbox"], area.get("investigation_id")), lon, lat, _elevation(lon, lat))
    reason = "On station" if last is None else "Observation"
    kind, extra = ("station" if last is None else "observation"), ""
    if o.get("surface_c") is not None and o.get("surface_p95_c") is not None and o["surface_c"] >= o["surface_p95_c"]:
        kind, extra = "warm", f" Warmer than 95% of the area ({o['surface_p95_c']:.0f} °C), worth a closer look."
    elif o.get("change_db") is not None and abs(o["change_db"]) >= 3:
        kind, extra = "change", " The radar measured a strong change here."
    text = note_text(drone["name"], task["label"], o, reason) + extra
    # Sent to check a radar patch: judge what was measured against the patch's polarimetric hypothesis.
    target_id = (task.get("job_key") or "").split(":", 1)[1] if ":" in (task.get("job_key") or "") else None
    brief = next((t.get("brief") for t in db.targets() if t["id"] == target_id), None) if target_id else None
    if brief and brief.get("hypothesis"):
        from ..signatures import judge
        verdict = judge(brief["hypothesis"], o, {"surface_median_c": o.get("surface_median_c")})
        o = {**o, "target_id": target_id, "hypothesis": brief["hypothesis"], "verdict": verdict["verdict"], "verdict_reasons": verdict["reasons"]}
        word = {"supports": "supports", "contradicts": "does not support", "inconclusive": "is inconclusive on"}[verdict["verdict"]]
        text += f" Check of patch {brief.get('rank')} ({brief.get('question') or brief['hypothesis']}): this {word} it; {verdict['reasons'][0]}."
        if verdict["verdict"] != "inconclusive" and kind not in ("warm", "change"):
            kind = "verify"
        db.message(f"agent:{drone['id']}", "all", "verification", {"text": f"{drone['name']} checked patch {brief.get('rank')}: {word} {brief['hypothesis'].replace('_', ' ')} ({verdict['reasons'][0]}).",
                                                                     "target_id": target_id, "verdict": verdict["verdict"], "lon": lon, "lat": lat})
    db.add_note(drone["id"], task["id"], kind, lon, lat, text, o)
    if kind in ("warm", "change"):
        reading = f"{o['surface_c']:.0f} °C surface, " if o.get("surface_c") is not None else ""
        db.message(f"agent:{drone['id']}", "all", "observation", {"text": f"{drone['name']}: {extra.strip()} ({reading}{lat:.4f}, {lon:.4f})", "lon": lon, "lat": lat, "note_kind": kind})
    relieves = task["params"].get("relieves")
    if last is None and relieves:
        db.message(f"agent:{drone['id']}", f"agent:{relieves}", "relieved", {"text": f"{drone['name']} is on station for “{task['label']}”. {relieves}, you are relieved; head home."})
    return {"kind": kind, "text": text}


def _note_once(db, sender: str, recipient: str, notes: list[dict], previous: list[str]) -> list[str]:
    """Post only messages that were not already posted last round, so the log records changes, not repetition."""
    texts = [n["text"] for n in notes]
    for n in notes:
        if n["text"] not in previous:
            db.message(sender, recipient, n.get("kind", "note"), {k: v for k, v in n.items() if k != "kind"})
    return texts


def command_round(db, mode: dict, area: dict, drones: list[dict], tasks: list[dict], t: float) -> list[dict]:
    """A fleet-wide command overrides the auction: every airborne aircraft holds where it is, or comes home."""
    current = {x["drone_id"]: x for x in tasks}
    created = []
    for d in drones:
        s = d.get("state", {})
        if d["link_state"] == "lost" or "lon" not in s or s.get("landed"):
            continue
        task = current.get(d["id"])
        if mode["mode"] == "hold" and not (task and task["kind"] == "hold" and task["created_by"] == "fleet-command"):
            params = {"lon": s["lon"], "lat": s["lat"], "agl_m": max(40.0, float(s.get("agl_m") or 100)), "radius_m": 30, "speed_mps": 5}
            created.append(db.create_task(d["id"], "hold", "Hold position (fleet held)", params, "fleet-command", mode.get("reason") or "Held by an operator", 1.2, None, None))
        elif mode["mode"] == "recall" and not (task and task["kind"] == "return"):
            params = {"lon": area["launch"][0], "lat": area["launch"][1], "agl_m": 90}
            created.append(db.create_task(d["id"], "return", "Return to launch (fleet recalled)", params, "fleet-command", mode.get("reason") or "Recalled by an operator", 1.2, None, None))
    for task in created:
        db.message("coordinator", task["drone_id"], "command", {"text": f"{task['drone_id']}: {task['label']}.", "task_id": task["id"]})
    return created


def cadence_for_coordinator(changed: bool, pending_acks: int, online: int, mode: str) -> float:
    """Seconds until the next round: quick while things change, slow when the fleet is settled or absent."""
    if changed or pending_acks or mode != "normal":
        return 2.0
    return 6.0 if online else 15.0


def cadence_for_agent(drone: dict, task: dict | None, urgent: bool) -> float:
    s = drone.get("state", {})
    if urgent or (task and task["status"] in ("assigned", "acknowledged")):
        return 2.0
    if drone["link_state"] == "lost":
        return 20.0
    if drone["link_state"] == "stale":
        return 5.0
    if s.get("landed") and not task:
        return 10.0
    return 3.0


def run_coordination_round(payload: dict) -> dict:
    db = fleet_db(); t = time.time()
    area = db.area()
    if not area:
        db.heartbeat("coordinator", "coordinator", payload.get("workflow_id"), "idle", {"round": payload.get("round"), "text": "Waiting for a monitoring area.", "next_s": 15.0})
        return {"summary": {"status": "idle"}, "need_agents": [], "next_s": 15.0}
    drones = db.drones(); tasks = db.tasks(); controlled = set(db.control())
    mode = db.mode()
    previous_mode = (db.heartbeat_summary("coordinator") or {}).get("mode", "normal")
    if mode["mode"] == "normal" and previous_mode != "normal":
        if db.supersede_commands():
            db.message("coordinator", "all", "command", {"text": "The fleet resumed: the auction assigns jobs again."})
        tasks = db.tasks()
    if mode["mode"] != "normal":
        created = command_round(db, mode, area, drones, tasks, t)
        online = [d for d in drones if d["link_state"] == "online"]
        word = "held: every airborne aircraft holds its position" if mode["mode"] == "hold" else "recalled: every aircraft returns to launch"
        summary = {"round": payload.get("round"), "online": len(online), "jobs": 0, "assigned": 0, "new_tasks": len(created), "mode": mode["mode"],
                   "next_s": 2.0, "text": f"Fleet {word} ({mode.get('reason') or 'operator command'})."}
        db.heartbeat("coordinator", "coordinator", payload.get("workflow_id"), "running", summary)
        agents = {a["id"]: a for a in db.agents()}
        need = [d["id"] for d in online if f"agent:{d['id']}" not in agents or t - agents[f"agent:{d['id']}"]["heartbeat"] > AGENT_STALE_S]
        return {"summary": summary, "need_agents": need, "wake": [x["drone_id"] for x in created], "next_s": 2.0}
    decisions = plan_round(area, db.targets(), drones, tasks, db.bids(), controlled, t, db.refusals(), db.benched())

    for task_id in sorted(set(decisions["released"])):
        task = next((x for x in tasks if x["id"] == task_id), None)
        if task:
            db.ack(task_id, task["drone_id"], "failed", "released by the coordinator")
    created = []
    for nt in decisions["tasks"]:
        task = db.create_task(nt["drone_id"], nt["kind"], nt["label"], nt["params"], "coordinator", nt["reason"], nt["priority"], nt.get("job_key"))
        created.append(task)
        if nt["kind"] != "standby":
            db.message("coordinator", nt["drone_id"], "award" if nt["kind"] != "return" else "return", {"text": f"{nt['drone_id']}: {nt['label']}. {nt['reason']}", "task_id": task["id"]})
    for rt in decisions["retarget"]:
        db.update_task_params(rt["task_id"], rt["params"])
    current = {x["drone_id"]: x for x in db.tasks()}
    for op in decisions["operations"]:
        task = current.get(op["drone_id"])
        if task:
            v = op["volume"]
            db.put_operation(op["drone_id"], task["id"], v["geometry"], v["lower"], v["upper"], t + 15 * 60)
    db.set_jobs(decisions["jobs"])

    online = [d for d in drones if d["link_state"] == "online"]
    # Supervision: every aircraft in contact has a live agent (started, or woken if its heartbeat went quiet), and
    # agents of aircraft that have left the fleet are forgotten.
    db.forget_agents({f"agent:{d['id']}" for d in drones})
    agents = {a["id"]: a for a in db.agents()}
    need = [d["id"] for d in online if f"agent:{d['id']}" not in agents or t - agents[f"agent:{d['id']}"]["heartbeat"] > AGENT_STALE_S]
    assigned = sum(1 for j in decisions["jobs"] if j.get("assigned_drone"))
    pending = sum(1 for x in db.tasks() if x["status"] == "assigned")
    changed = bool(created or decisions["released"] or decisions["retarget"])
    next_s = cadence_for_coordinator(changed, pending, len(online), "normal")
    summary = {"round": payload.get("round"), "online": len(online), "jobs": len(decisions["jobs"]), "assigned": assigned,
               "new_tasks": len(created), "reasons": payload.get("reasons", [])[-3:], "mode": "normal", "next_s": next_s, "pending_acks": pending,
               "agents": len([a for a in agents.values() if a["kind"] == "drone" and a["age_s"] < AGENT_STALE_S]), "restarting": need,
               "text": f"{assigned} of {len(decisions['jobs'])} jobs covered by {len(online)} aircraft online."}
    previous = db.heartbeat("coordinator", "coordinator", payload.get("workflow_id"), "running", summary)
    summary["notes"] = _note_once(db, "coordinator", "all", decisions["messages"], previous.get("notes", []))
    db.heartbeat("coordinator", "coordinator", payload.get("workflow_id"), "running", summary)
    return {"summary": {k: v for k, v in summary.items() if k != "notes"}, "need_agents": need, "wake": [x["drone_id"] for x in created], "next_s": next_s}


def run_drone_agent_round(payload: dict) -> dict:
    db = fleet_db(); drone_id = payload["drone_id"]
    drones = db.drones(); drone = next((d for d in drones if d["id"] == drone_id), None)
    if not drone:
        return {"retire": True}
    area = db.area(); s = drone.get("state", {})
    launch = area["launch"] if area else [s.get("lon", 0), s.get("lat", 0)]
    task = db.current_task(drone_id)
    operation = next((o for o in db.operations() if o["drone_id"] == drone_id), None)
    # Bid on every open job from this aircraft's own position, battery and sensors.
    bids = [bid(drone, j, launch, (task or {}).get("job_key")) for j in db.jobs()] if drone["link_state"] == "online" else []
    db.put_bids(drone_id, bids)
    findings = assess(drone, task, operation, drones, launch)
    # Mark a target observed while its orbit is flown on station.
    if task and task["kind"] == "orbit" and task["status"] == "active" and "lon" in s and (task.get("job_key") or "").startswith("orbit:"):
        p = task["params"]
        if distance(s["lon"], s["lat"], p["lon"], p["lat"]) < p.get("radius_m", 200) * 1.4:
            db.mark_observed(task["job_key"].split(":", 1)[1], time.time())
    if operation:
        nonconforming = any(f["kind"] == "nonconforming" for f in findings)
        if nonconforming and operation["state"] != "nonconforming":
            db.set_operation_state(drone_id, "nonconforming")
        elif not nonconforming and operation["state"] == "nonconforming":
            db.set_operation_state(drone_id, "activated")
    # Field notes while working a task on station (never while landed or out of contact).
    note = None
    if task and task["status"] == "active" and drone["link_state"] == "online" and not s.get("landed") and task["kind"] not in ("return", "standby"):
        try:
            note = write_field_note(db, drone, task, time.time())
        except Exception:  # observations are best effort; the agent's other duties continue
            note = None
    feasible = sum(1 for b in bids if b["feasible"])
    summary = {"round": payload.get("round"), "findings": [f["kind"] for f in findings], "feasible_bids": feasible, "bids": len(bids), "next_s": cadence_for_agent(drone, task, False),
               "task": (task or {}).get("label"), "text": f"{drone['name']}: {(task or {}).get('label', 'no task')}; can take {feasible} of {len(bids)} jobs."}
    previous = db.heartbeat(f"agent:{drone_id}", "drone", payload.get("workflow_id"), drone["link_state"], summary)
    new = [f for f in findings if f["kind"] not in previous.get("findings", [])]
    for f in new:
        db.message(f"agent:{drone_id}", "coordinator", f["kind"], {"text": f["text"]})
    urgent = [f for f in new if f.get("urgent")]
    retire = drone["link_state"] == "lost" and (drone.get("age_s") or 0) > 600
    return {"urgent": bool(urgent), "reason": urgent[0]["kind"] if urgent else "", "retire": retire, "summary": summary, "note": (note or {}).get("kind"),
            "next_s": cadence_for_agent(drone, task, bool(urgent))}


@activity.defn
async def coordination_round(payload: dict) -> dict:
    return await asyncio.to_thread(run_coordination_round, payload)


@activity.defn
async def drone_agent_round(payload: dict) -> dict:
    return await asyncio.to_thread(run_drone_agent_round, payload)
