"""Activities for the durable fleet agents: each reads the central database, decides, and writes back."""
from __future__ import annotations

import asyncio
import time

from temporalio import activity

from .db import fleet_db
from .planner import assess, bid, distance, plan_round

AGENT_STALE_S = 20


def _note_once(db, sender: str, recipient: str, notes: list[dict], previous: list[str]) -> list[str]:
    """Post only messages that were not already posted last round, so the log records changes, not repetition."""
    texts = [n["text"] for n in notes]
    for n in notes:
        if n["text"] not in previous:
            db.message(sender, recipient, n.get("kind", "note"), {k: v for k, v in n.items() if k != "kind"})
    return texts


def run_coordination_round(payload: dict) -> dict:
    db = fleet_db(); t = time.time()
    area = db.area()
    if not area:
        db.heartbeat("coordinator", "coordinator", payload.get("workflow_id"), "idle", {"round": payload.get("round"), "text": "Waiting for a monitoring area."})
        return {"summary": {"status": "idle"}, "need_agents": []}
    drones = db.drones(); tasks = db.tasks(); controlled = set(db.control())
    decisions = plan_round(area, db.targets(), drones, tasks, db.bids(), controlled, t, db.refusals())

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
    agents = {a["id"]: a for a in db.agents()}
    need = [d["id"] for d in online if f"agent:{d['id']}" not in agents or t - agents[f"agent:{d['id']}"]["heartbeat"] > AGENT_STALE_S]
    assigned = sum(1 for j in decisions["jobs"] if j.get("assigned_drone"))
    summary = {"round": payload.get("round"), "online": len(online), "jobs": len(decisions["jobs"]), "assigned": assigned,
               "new_tasks": len(created), "reasons": payload.get("reasons", [])[-3:],
               "text": f"{assigned} of {len(decisions['jobs'])} jobs covered by {len(online)} aircraft online."}
    previous = db.heartbeat("coordinator", "coordinator", payload.get("workflow_id"), "running", summary)
    summary["notes"] = _note_once(db, "coordinator", "all", decisions["messages"], previous.get("notes", []))
    db.heartbeat("coordinator", "coordinator", payload.get("workflow_id"), "running", summary)
    return {"summary": {k: v for k, v in summary.items() if k != "notes"}, "need_agents": need}


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
    if task and task["kind"] == "orbit" and task["status"] == "active" and "lon" in s and task.get("job_key", "").startswith("orbit:"):
        p = task["params"]
        if distance(s["lon"], s["lat"], p["lon"], p["lat"]) < p.get("radius_m", 200) * 1.4:
            db.mark_observed(task["job_key"].split(":", 1)[1], time.time())
    if operation:
        nonconforming = any(f["kind"] == "nonconforming" for f in findings)
        if nonconforming and operation["state"] != "nonconforming":
            db.set_operation_state(drone_id, "nonconforming")
        elif not nonconforming and operation["state"] == "nonconforming":
            db.set_operation_state(drone_id, "activated")
    feasible = sum(1 for b in bids if b["feasible"])
    summary = {"round": payload.get("round"), "findings": [f["kind"] for f in findings], "feasible_bids": feasible, "bids": len(bids),
               "task": (task or {}).get("label"), "text": f"{drone['name']}: {(task or {}).get('label', 'no task')}; can take {feasible} of {len(bids)} jobs."}
    previous = db.heartbeat(f"agent:{drone_id}", "drone", payload.get("workflow_id"), drone["link_state"], summary)
    new = [f for f in findings if f["kind"] not in previous.get("findings", [])]
    for f in new:
        db.message(f"agent:{drone_id}", "coordinator", f["kind"], {"text": f["text"]})
    urgent = [f for f in new if f.get("urgent")]
    retire = drone["link_state"] == "lost" and (drone.get("age_s") or 0) > 600
    return {"urgent": bool(urgent), "reason": urgent[0]["kind"] if urgent else "", "retire": retire, "summary": summary}


@activity.defn
async def coordination_round(payload: dict) -> dict:
    return await asyncio.to_thread(run_coordination_round, payload)


@activity.defn
async def drone_agent_round(payload: dict) -> dict:
    return await asyncio.to_thread(run_drone_agent_round, payload)
