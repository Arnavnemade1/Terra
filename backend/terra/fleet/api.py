"""Fleet API: the ground-station protocol (bearer token) and the workspace's view and controls (same origin).

Ground station → Terra (Authorization: Bearer $TERRA_DRONE_TOKEN):
  POST /api/fleet/register             who the aircraft is and what it can do
  POST /api/fleet/telemetry            position, height, speed, battery, link, gimbal; numbered (seq) so late or
                                       repeated messages are ignored. The reply carries the current task, any manual
                                       setpoint (virtual aircraft only) and the monitoring area.
  POST /api/fleet/tasks/{id}/ack       task lifecycle: acknowledged → active → completed | failed | rejected
  GET  /api/fleet/area                 the monitoring area and launch site
  The bearer token is either the server's TERRA_DRONE_TOKEN or a per-aircraft token from enrollment (which only
  works for the aircraft id it was issued to and can be revoked).

Workspace → Terra (same-origin browser requests from this machine, or anyone with TERRA_DRONE_TOKEN; requests from
another machine always need the token, and an aircraft's own token never grants these):
  GET  /api/fleet                      drones, tracks, tasks, airspace, targets, jobs, agents, messages
  POST /api/fleet/area                 the monitoring area and its targets (starts the coordinator)
  POST /api/fleet/tasks                a person's task for one aircraft (held for 15 minutes or until it ends)
  POST /api/fleet/drones/{id}/control  manual flight setpoints for a virtual aircraft (never a real one)
  POST /api/fleet/drones/{id}/release  hand a manually flown aircraft back to the coordinator
  GET  /api/fleet/operations           reserved airspace as operational intents (ASTM F3548-shaped)
  POST /api/fleet/command              fleet-wide: hold (every airborne aircraft holds), recall (all return), resume
  POST /api/fleet/dispatch             verify an investigation: each changed patch becomes a target with its question
  GET  /api/fleet/evidence/{id}        the drones' field evidence for each patch of an investigation, judged
  POST /api/fleet/enroll               a per-aircraft token and the command that connects it (this machine only)
  GET  /api/fleet/enrollments          issued aircraft tokens (never the tokens themselves)
  POST /api/fleet/enrollments/{id}/revoke   stop accepting an aircraft's token
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import math
import re
import secrets
import shlex
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .db import fleet_db
from .workflows import COORDINATOR_ID

router = APIRouter(prefix="/api/fleet")
LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}
_refused: set[tuple] = set()  # testclient: FastAPI's in-process test transport
ID = r"^[A-Za-z0-9_-]{1,32}$"
log = logging.getLogger(__name__)
_coordinator = {"checked": 0.0, "ok": False}
_client: dict = {"value": None}


class Capabilities(BaseModel):
    sensors: list[Literal["eo", "ir", "lidar", "multispectral", "gas", "sar"]] = Field(default_factory=lambda: ["eo"], max_length=6)
    relay: bool = False
    cruise_mps: float = Field(default=10, gt=0, le=80)
    max_mps: float = Field(default=18, gt=0, le=120)
    endurance_min: float = Field(default=30, gt=1, le=1440)
    protocol: Literal["mavlink", "terra-sim", "dji-cloud", "other"] = "other"
    # The kinds of task this aircraft's ground station can carry out (a MAVLink bridge cannot fly survey routes itself).
    tasks: list[Literal["orbit", "survey", "perimeter", "search", "relay", "hold", "standby", "return"]] = Field(
        default_factory=lambda: ["orbit", "survey", "perimeter", "search", "relay", "hold", "standby", "return"], max_length=8)
    autopilot: str | None = Field(default=None, max_length=40)


class Register(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=40)
    origin: Literal["real", "sitl", "virtual"] = "real"  # sitl: real autopilot firmware on a simulated airframe
    aircraft: Literal["quad", "fixed-wing", "vtol", "other"] = "quad"
    capabilities: Capabilities = Field(default_factory=Capabilities)


class Gimbal(BaseModel):
    heading_deg: float = Field(ge=0, le=360)
    pitch_deg: float = Field(ge=-90, le=30)


class Telemetry(BaseModel):
    id: str = Field(pattern=ID)
    seq: int = Field(ge=0)
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    alt_m: float = Field(ge=-500, le=12_000, description="Above mean sea level")
    agl_m: float | None = Field(default=None, ge=-100, le=10_000)
    heading_deg: float = Field(default=0, ge=0, le=360)
    speed_mps: float = Field(default=0, ge=0, le=150)
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    link_dbm: float | None = Field(default=None, ge=-150, le=0)
    mode: str | None = Field(default=None, max_length=32)
    landed: bool = False
    gimbal: Gimbal | None = None
    video_url: str | None = Field(default=None, max_length=500)

    @field_validator("video_url")
    @classmethod
    def http_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("Video must be an http(s) URL without credentials.")
        return value


class Ack(BaseModel):
    id: str = Field(pattern=ID)
    status: Literal["acknowledged", "active", "completed", "failed", "rejected"]
    detail: str | None = Field(default=None, max_length=200)


class Target(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,48}$")
    kind: Literal["change", "summit", "hazard", "point", "hotspot", "search"] = "point"
    label: str = Field(min_length=1, max_length=60)
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    radius_m: float = Field(default=300, ge=20, le=20_000)
    priority: float = Field(default=0.5, ge=0, le=1)
    needs: list[Literal["eo", "ir", "lidar", "multispectral", "gas", "sar", "relay"]] = Field(default_factory=list, max_length=4)


class Area(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    bbox: list[float] = Field(min_length=4, max_length=4)
    targets: list[Target] = Field(default_factory=list, max_length=12)
    # What the fleet is for: watching measured change, watching the warmest ground (Landsat thermal), or searching.
    profile: Literal["change", "thermal", "search"] = "change"
    investigation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")
    # Where the crew sets up (a deployment anywhere); the area's centre when omitted.
    launch: list[float] | None = Field(default=None, min_length=2, max_length=2)

    @field_validator("bbox")
    @classmethod
    def small_box(cls, v: list[float]) -> list[float]:
        w, s, e, n = v
        if not (-180 <= w < e <= 180 and -90 <= s < n <= 90 and e - w <= 2 and n - s <= 2):
            raise ValueError("Use an area up to 2 degrees across.")
        return v

    @field_validator("launch")
    @classmethod
    def near_the_area(cls, v: list[float] | None, info) -> list[float] | None:
        box = info.data.get("bbox")
        if v is None or not box:
            return v
        lon, lat = v
        if not (box[0] - 0.25 <= lon <= box[2] + 0.25 and box[1] - 0.25 <= lat <= box[3] + 0.25):
            raise ValueError("Launch from inside the area or close to it.")
        return v


class OperatorTask(BaseModel):
    drone_id: str = Field(pattern=ID)
    kind: Literal["orbit", "survey", "perimeter", "search", "relay", "hold", "return"]
    label: str = Field(default="", max_length=60)
    lon: float | None = Field(default=None, ge=-180, le=180)
    lat: float | None = Field(default=None, ge=-90, le=90)
    radius_m: float = Field(default=250, ge=20, le=5000)
    agl_m: float = Field(default=150, ge=20, le=1000)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    size_m: float = Field(default=1200, ge=200, le=4000)  # search: the side of the square searched

    @field_validator("bbox")
    @classmethod
    def real_box(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return v
        w, s, e, n = v
        if not (-180 <= w < e <= 180 and -90 <= s < n <= 90 and e - w <= 1 and n - s <= 1):
            raise ValueError("Use a west, south, east, north box on Earth up to 1 degree across.")
        return v


class Command(BaseModel):
    action: Literal["hold", "recall", "resume"]
    reason: str = Field(default="", max_length=120)


class Dispatch(BaseModel):
    investigation_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")
    launch: list[float] | None = Field(default=None, min_length=2, max_length=2)


class Setpoint(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    agl_m: float = Field(ge=10, le=1200)
    heading_deg: float = Field(ge=0, le=360)
    gimbal_pitch_deg: float = Field(default=-35, ge=-90, le=20)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token(authorization: str | None, drone_id: str | None = None) -> None:
    """Accept the server's master token, or an enrolled aircraft's own token (for that aircraft only)."""
    expected = os.getenv("TERRA_DRONE_TOKEN", "")
    if not expected:
        raise HTTPException(403, "Drone connections are off. Set TERRA_DRONE_TOKEN on the server to accept them.")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if supplied and hmac.compare_digest(supplied.encode(), expected.encode()):
        return
    credential = fleet_db().credential(_hash(supplied)) if supplied.startswith("terra_") else None
    if credential is None:
        raise HTTPException(401, "The drone token is missing, wrong or revoked.")
    if drone_id is not None and credential["drone_id"] != drone_id:
        raise HTTPException(403, "This token was issued to a different aircraft.")


def operator(request: Request, authorization: str | None = Header(default=None)) -> None:
    """Fleet controls and the fleet picture: the workspace in a browser on this machine, or a caller with the server's
    drone token. A request from another machine always needs the token (its address cannot be forged; headers can)."""
    expected = os.getenv("TERRA_DRONE_TOKEN", "")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    workspace = request.headers.get("x-terra-workspace-token", "")
    if workspace:
        secret = os.getenv("TERRA_WORKSPACE_TOKEN", "")
        if not secret or not hmac.compare_digest(workspace.encode(), secret.encode()):
            raise HTTPException(403, "The simulation workspace connection could not be authenticated.")
        path = request.url.path.removeprefix("/api/fleet")
        allowed = (request.method == "GET" and (path in ("", "/operations") or path.startswith("/evidence/"))) or (
            request.method == "POST" and (path in ("/area", "/command", "/tasks", "/dispatch") or
            re.fullmatch(r"/drones/[A-Za-z0-9_-]{1,32}/(?:control|release)", path)))
        if os.getenv("TERRA_FLEET") != "virtual" or not allowed:
            raise HTTPException(403, "This workspace can operate the simulated fleet only. Aircraft connections require operator access.")
        if request.headers.get("sec-fetch-site") != "same-origin" or (request.headers.get("origin") and not _workspace_origin(request.headers["origin"], request)):
            raise HTTPException(403, "Open fleet controls from the Terra workspace.")
        # Fail closed if a ground station later connects physical aircraft to this
        # deployment. A public simulation capability must never become a pilot's.
        if any(d["origin"] == "real" for d in fleet_db().drones()):
            raise HTTPException(403, "Connected aircraft require authenticated operator access.")
        request.state.fleet_access = "simulation"
        return
    if supplied:
        if expected and hmac.compare_digest(supplied.encode(), expected.encode()):
            return
        raise HTTPException(401, "The token is wrong. Fleet controls take the server's drone token, not an aircraft's.")
    if (request.client.host if request.client else "") not in LOCAL:
        raise HTTPException(403, "From another machine, fleet controls need the drone token (Authorization: Bearer).")
    origin = request.headers.get("origin")
    site = request.headers.get("sec-fetch-site")
    if site != "same-origin" or (origin and not _workspace_origin(origin, request)):
        reason = (site, origin, request.headers.get("user-agent", "")[:60])
        if reason not in _refused:  # one log line per kind of caller, so a misconfigured page is easy to spot
            _refused.add(reason); log.warning("fleet control refused: sec-fetch-site=%s origin=%s agent=%s", *reason)
        raise HTTPException(403, "Fleet controls answer the Terra workspace (same-origin browser requests) or callers with the drone token.")


def _workspace_origin(origin: str, request: Request) -> bool:
    """A page served from this machine. The dev server's proxy rewrites Host to the API's own address, so the
    page's origin (for example http://127.0.0.1:5173) is compared with loopback names, not with Host."""
    if origin == f"{request.url.scheme}://{request.url.netloc}" or origin in filter(None, os.getenv("TERRA_ALLOWED_ORIGINS", "").split(",")):
        return True
    parts = urlsplit(origin)
    host = parts.hostname or ""
    return parts.scheme in ("http", "https") and (host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"))


async def wake_agent(drone_id: str, reason: str) -> None:
    """Cut an aircraft's agent's wait short (its next round runs now). Quietly does nothing if it is not running."""
    try:
        client = _client["value"]
        if client is not None:
            await client.get_workflow_handle(f"drone-agent-{drone_id}").signal("wake", reason)
    except Exception:
        pass


async def _db(fn, *args):
    return await asyncio.to_thread(fn, *args)


async def ensure_coordinator(reason: str) -> bool:
    """Start the durable coordinator if it is not running, then nudge it. False when Temporal is unreachable."""
    try:
        from temporalio.client import Client
        from temporalio.common import WorkflowIDConflictPolicy
        from ..config import get_settings
        settings = get_settings()
        client = _client["value"] or await asyncio.wait_for(Client.connect(settings.temporal_address, namespace=settings.temporal_namespace), timeout=2.5)
        _client["value"] = client
        handle = await client.start_workflow("FleetCoordinatorWorkflow", 0, id=COORDINATOR_ID, task_queue=settings.task_queue,
                                             id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        await handle.signal("nudge", reason)
        _coordinator.update(checked=time.time(), ok=True)
        return True
    except Exception as exc:  # the workspace shows the coordinator as offline; data is kept and used when it starts
        log.warning("Fleet coordinator unavailable: %s", exc.__class__.__name__)
        _client["value"] = None
        _coordinator.update(checked=time.time(), ok=False)
        return False


def _task_view(task: dict | None) -> dict | None:
    if not task:
        return None
    return {k: task[k] for k in ("id", "kind", "label", "params", "status", "created_by", "reason", "created_at", "updated_at", "acked_at")}


# ---------------------------------------------------------------- ground stations
@router.post("/register", status_code=201)
async def register(body: Register, authorization: str | None = Header(default=None)):
    await _db(_token, authorization, body.id)
    if body.origin == "real" and os.getenv("TERRA_FLEET") == "virtual" and os.getenv("TERRA_WORKSPACE_TOKEN"):
        raise HTTPException(403, "This deployment hosts a public simulation. Connect physical aircraft to an authenticated operator deployment.")
    db = fleet_db()
    await _db(db.register, body.model_dump())
    asyncio.create_task(ensure_coordinator(f"{body.id} joined"))
    asyncio.create_task(wake_agent(body.id, "registered"))
    return {"registered": body.id, "server_time": time.time(), "area": await _db(db.area)}


# When the workspace last polled GET /fleet. Only the map page does, so this says whether anyone is looking.
_viewer = {"seen": 0.0}
VIEWER_TTL_S = 20.0


@router.post("/telemetry", status_code=202)
async def telemetry(body: Telemetry, authorization: str | None = Header(default=None)):
    await _db(_token, authorization, body.id)
    db = fleet_db()
    try:
        accepted = await _db(db.record, body.model_dump())
    except KeyError:
        raise HTTPException(409, "Register this aircraft first (POST /api/fleet/register).") from None
    task = await _db(db.current_task, body.id)
    drones = await _db(db.drones)
    me = next((d for d in drones if d["id"] == body.id), None)
    control = (await _db(db.control, body.id)).get(body.id) if me and me["origin"] in ("virtual", "sitl") else None
    return {"accepted": accepted, "server_time": time.time(), "task": _task_view(task), "control": control, "area": await _db(db.area),
            "watched": time.time() - _viewer["seen"] < VIEWER_TTL_S}


@router.post("/tasks/{task_id}/ack")
async def acknowledge(task_id: str, body: Ack, authorization: str | None = Header(default=None)):
    await _db(_token, authorization, body.id)
    if not re.fullmatch(r"^[a-f0-9]{12}$", task_id):
        raise HTTPException(404, "Unknown task.")
    db = fleet_db()
    task = await _db(db.ack, task_id, body.id, body.status, body.detail)
    if task is None:
        raise HTTPException(404, "Unknown task for this aircraft.")
    if body.status in ("completed", "failed", "rejected"):
        asyncio.create_task(ensure_coordinator(f"{body.id} reported {body.status}"))
    asyncio.create_task(wake_agent(body.id, f"task {body.status}"))
    return {"task": _task_view(task)}


@router.get("/area")
async def area_for_ground_stations(authorization: str | None = Header(default=None)):
    await _db(_token, authorization)
    return {"area": await _db(fleet_db().area), "server_time": time.time()}


# ---------------------------------------------------------------- workspace
@router.get("", dependencies=[Depends(operator)])
async def fleet_state(request: Request):
    _viewer["seen"] = time.time()
    db = fleet_db()
    drones, tasks, operations, agents, messages, area, targets, jobs, notes, mode = await asyncio.gather(
        _db(db.drones), _db(db.tasks), _db(db.operations), _db(db.agents), _db(db.messages, 60), _db(db.area), _db(db.targets), _db(db.jobs), _db(db.notes, 80), _db(db.mode))
    by_drone = {t["drone_id"]: t for t in tasks}
    ops = {o["drone_id"]: o for o in operations}
    tracks = await asyncio.gather(*[_db(db.track, d["id"], 20) for d in drones])
    agent = {a["id"]: a for a in agents}
    coordinator = agent.get("coordinator")
    # Keep the coordinator alive while someone is watching an area.
    if area and (not coordinator or coordinator["age_s"] > 20) and time.time() - _coordinator["checked"] > 15:
        asyncio.create_task(ensure_coordinator("workspace opened"))
    return {
        "server_time": time.time(), "connections_enabled": bool(os.getenv("TERRA_DRONE_TOKEN")), "area": area, "targets": targets, "jobs": jobs,
        "operator_access": getattr(request.state, "fleet_access", "operator") == "operator",
        "simulation": {"managed": os.getenv("TERRA_FLEET") == "virtual", "aircraft_count": int(os.getenv("TERRA_VIRTUAL_FLEET", "5"))},
        "coordinator": {"running": bool(coordinator and coordinator["age_s"] < 15), "summary": (coordinator or {}).get("summary", {}), "age_s": (coordinator or {}).get("age_s")},
        "drones": [{**{k: d[k] for k in ("id", "name", "origin", "aircraft", "capabilities", "age_s", "link_state", "registered_at")}, "state": d["state"],
                    "track": tr, "task": _task_view(by_drone.get(d["id"])), "operation": ops.get(d["id"]),
                    "agent": {k: agent[f"agent:{d['id']}"][k] for k in ("status", "age_s", "summary")} if f"agent:{d['id']}" in agent else None}
                   for d, tr in zip(drones, tracks)],
        "messages": messages,
        "notes": [{k: n[k] for k in ("id", "t", "drone_id", "task_id", "kind", "lon", "lat", "text", "data")} for n in notes],
        "mode": mode,
        "agents": [{"id": a["id"], "kind": a["kind"], "workflow_id": a["workflow_id"], "status": a["status"], "age_s": a["age_s"],
                    "next_s": a["summary"].get("next_s"), "round": a["summary"].get("round"), "text": a["summary"].get("text")} for a in agents],
    }


def search_sectors(bbox: list[float]) -> list[dict]:
    """Four search sectors (quadrants), each searched from its centre outwards; the sector nearest launch first."""
    w, s, e, n = bbox; cx, cy = (w + e) / 2, (s + n) / 2
    size = min(3000.0, max(400.0, min((e - w) * 111_320 * math.cos(math.radians(cy)), (n - s) * 110_574) / 2))
    names = {(0, 1): "north-west", (1, 1): "north-east", (0, 0): "south-west", (1, 0): "south-east"}
    return [{"id": f"search:{names[(i, j)]}", "kind": "search", "label": f"{names[(i, j)]} sector", "lon": round(w + (e - w) * (0.25 + 0.5 * i), 6),
             "lat": round(s + (n - s) * (0.25 + 0.5 * j), 6), "radius_m": round(size / 2), "priority": 0.6, "needs": [], "size_m": round(size)}
            for i in (0, 1) for j in (1, 0)]


async def thermal_targets(bbox: list[float]) -> list[dict]:
    """The warmest ground in the most recent mostly clear Landsat scene, for an IR camera to look at."""
    from ..thermal import hot_spots, thermal
    try:
        scene = await thermal(bbox)
        spots = hot_spots(scene, count=4, separation_m=600)
    except Exception:
        log.exception("thermal targets failed")
        return []
    # The scene's colour scale stops at its 99.5th percentile; a spot at the top of the scale is that warm or warmer.
    top = scene.get("maxC", 1e9)
    reading = lambda c: f"≥{top:.0f} °C" if c >= top - 0.2 else f"{c:.0f} °C"  # noqa: E731
    return [{"id": f"hot:{i + 1}", "kind": "hotspot", "label": f"Warm ground {reading(spot['surface_c'])}", "lon": spot["lon"], "lat": spot["lat"],
             "radius_m": 250, "priority": round(0.9 - 0.1 * i, 2), "needs": ["ir"], "surface_c": spot["surface_c"]} for i, spot in enumerate(spots)]


@router.post("/area", dependencies=[Depends(operator)])
async def set_area(body: Area):
    db = fleet_db()
    targets = [t.model_dump() for t in body.targets]
    if body.profile == "thermal":
        targets = (await thermal_targets(body.bbox)) + [t for t in targets if t["kind"] == "summit"][:2]
    elif body.profile == "search":
        targets = search_sectors(body.bbox)
    area = await _db(db.set_area, body.name, body.bbox, targets, body.investigation_id, body.profile, body.launch)
    running = await ensure_coordinator("monitoring area updated")
    return {"area": area, "coordinator": running, "targets": len(targets)}


@router.post("/command", dependencies=[Depends(operator)])
async def fleet_command(body: Command):
    """Fleet-wide orders, applied by the coordinator at its next round (it is woken at once)."""
    db = fleet_db()
    mode = {"hold": "hold", "recall": "recall", "resume": "normal"}[body.action]
    reason = body.reason.strip() or {"hold": "Every airborne aircraft holds its position.", "recall": "Every aircraft returns to launch.",
                                     "resume": "The auction assigns jobs again."}[body.action]
    state = await _db(db.set_mode, mode, reason)
    await _db(db.message, "operator", "all", "command", {"text": f"Fleet {body.action}: {reason}"})
    running = await ensure_coordinator(f"fleet {body.action}")
    return {"mode": state, "coordinator": running}


@router.post("/tasks", status_code=201, dependencies=[Depends(operator)])
async def operator_task(body: OperatorTask):
    db = fleet_db()
    drones = await _db(db.drones)
    drone = next((d for d in drones if d["id"] == body.drone_id), None)
    if not drone:
        raise HTTPException(404, "That aircraft is not connected.")
    area = await _db(db.area)
    launch = (area or {}).get("launch") or [drone["state"].get("lon"), drone["state"].get("lat")]
    params: dict = {"agl_m": body.agl_m, "speed_mps": drone["capabilities"].get("cruise_mps", 10)}
    if body.kind in ("orbit", "relay", "hold"):
        if body.lon is None or body.lat is None:
            raise HTTPException(422, "This task needs a position.")
        params.update(lon=body.lon, lat=body.lat, radius_m=body.radius_m if body.kind != "hold" else 30)
    elif body.kind in ("survey", "perimeter"):
        box = body.bbox or (area or {}).get("bbox")
        if not box:
            raise HTTPException(422, "This task needs an area.")
        w, s, e, n = box
        if not (w < e and s < n and e - w <= 1 and n - s <= 1):
            raise HTTPException(422, "Use an area up to 1 degree across.")
        params.update(bbox=box, lane_m=150, inset=0.08)
    elif body.kind == "search":
        if body.lon is None or body.lat is None:
            raise HTTPException(422, "A search needs a starting point (datum).")
        params.update(lon=body.lon, lat=body.lat, size_m=body.size_m, lane_m=120, agl_m=min(body.agl_m, 120), speed_mps=min(params["speed_mps"], 8))
    else:
        if launch[0] is None or launch[1] is None:
            raise HTTPException(422, "There is no launch site to return to yet (no monitoring area and no position from this aircraft).")
        params.update(lon=launch[0], lat=launch[1], agl_m=min(body.agl_m, 120))
    label = body.label or {"orbit": "Orbit", "survey": "Survey", "perimeter": "Map the perimeter", "search": "Search", "relay": "Radio relay", "hold": "Hold", "return": "Return to launch"}[body.kind]
    task = await _db(db.create_task, body.drone_id, body.kind, label, params, "operator", "Assigned in the workspace", 1.0, None, time.time() + 15 * 60)
    await _db(db.message, "operator", body.drone_id, "operator-task", {"text": f"{drone['name']}: {label} (assigned by a person; held for 15 min).", "task_id": task["id"]})
    asyncio.create_task(ensure_coordinator("operator task"))
    return {"task": _task_view(task), "advisory": drone["origin"] == "real"}


@router.post("/drones/{drone_id}/control", dependencies=[Depends(operator)])
async def manual_control(drone_id: str, body: Setpoint):
    if not re.fullmatch(ID, drone_id):
        raise HTTPException(404, "Unknown aircraft.")
    db = fleet_db()
    drone = next((d for d in await _db(db.drones) if d["id"] == drone_id), None)
    if not drone:
        raise HTTPException(404, "That aircraft is not connected.")
    if drone["origin"] not in ("virtual", "sitl"):
        raise HTTPException(403, "Real aircraft are never flown from the browser; give them a task instead.")
    first = not (await _db(db.control, drone_id))
    await _db(db.set_control, drone_id, body.model_dump())
    if first:
        await _db(db.message, "operator", drone_id, "manual", {"text": f"{drone['name']} is being flown manually from the workspace."})
        asyncio.create_task(ensure_coordinator("manual flight"))
    return {"ok": True}


@router.post("/drones/{drone_id}/release", dependencies=[Depends(operator)])
async def release_control(drone_id: str):
    if not re.fullmatch(ID, drone_id):
        raise HTTPException(404, "Unknown aircraft.")
    db = fleet_db()
    if not any(d["id"] == drone_id for d in await _db(db.drones)):
        raise HTTPException(404, "That aircraft is not connected.")
    await _db(db.set_control, drone_id, None)
    await _db(db.message, "operator", drone_id, "manual-end", {"text": f"{drone_id} handed back to the coordinator."})
    asyncio.create_task(ensure_coordinator("manual flight ended"))
    return {"ok": True}


@router.get("/operations", dependencies=[Depends(operator)])
async def operational_intents():
    """Reserved airspace in the shape of ASTM F3548 operational intents, for exchange with a UTM service supplier.
    Altitudes here are above ground level; convert to WGS84 with terrain before submitting to a USS."""
    ops = await _db(fleet_db().operations)
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: E731
    state = {"accepted": "Accepted", "activated": "Activated", "nonconforming": "Nonconforming"}
    out = []
    for o in ops:
        g = o["geometry"]
        outline = ({"outline_circle": {"center": {"lng": g["lon"], "lat": g["lat"]}, "radius": {"value": round(g["radius_m"], 1), "units": "M"}}}
                   if g["type"] == "circle" else
                   {"outline_polygon": {"vertices": [{"lng": g["bbox"][0], "lat": g["bbox"][1]}, {"lng": g["bbox"][2], "lat": g["bbox"][1]},
                                                     {"lng": g["bbox"][2], "lat": g["bbox"][3]}, {"lng": g["bbox"][0], "lat": g["bbox"][3]}]}})
        out.append({"reference": {"id": o["id"], "manager": "terra", "state": state.get(o["state"], "Accepted"), "time_start": {"value": iso(o["t_start"]), "format": "RFC3339"},
                                  "time_end": {"value": iso(o["t_end"]), "format": "RFC3339"}, "aircraft": o["drone_id"]},
                    "details": {"volumes": [{"volume": {**outline, "altitude_lower": {"value": o["alt_lower_m"], "reference": "AGL", "units": "M"},
                                                       "altitude_upper": {"value": o["alt_upper_m"], "reference": "AGL", "units": "M"}},
                                             "time_start": {"value": iso(o["t_start"]), "format": "RFC3339"}, "time_end": {"value": iso(o["t_end"]), "format": "RFC3339"}}],
                                "priority": 0}})
    return {"operational_intents": out, "note": "Altitudes are above ground level. Terra is not a certified USS; share these through one."}


# ---------------------------------------------------------------- enrollment (adding an aircraft)
class Enroll(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["mavlink", "sitl"] = "mavlink"
    connection: str = Field(default="udpin:0.0.0.0:14550", max_length=120, pattern=r"^[A-Za-z0-9_.:/@=,-]+$")
    aircraft: Literal["quad", "fixed-wing", "vtol"] = "quad"
    sensors: list[Literal["eo", "ir", "lidar", "multispectral", "gas"]] = Field(default_factory=lambda: ["eo"], min_length=1, max_length=5)
    relay: bool = False


@router.post("/enroll", status_code=201, dependencies=[Depends(operator)])
async def enroll(body: Enroll, request: Request):
    """Issue a token for one aircraft and the command that connects its ground station. The token is shown once;
    only its SHA-256 is stored. It works only for this aircraft id and can be revoked."""
    if not os.getenv("TERRA_DRONE_TOKEN"):
        raise HTTPException(403, "Drone connections are off. Set TERRA_DRONE_TOKEN on the server to accept them.")
    token = "terra_" + secrets.token_urlsafe(32)
    await _db(fleet_db().add_credential, _hash(token), body.id, body.name)
    flags = [f"--id {shlex.quote(body.id)}", f"--name {shlex.quote(body.name)}", f"--aircraft {body.aircraft}", f"--sensors {','.join(body.sensors)}"]
    if body.relay:
        flags.append("--relay")
    if body.kind == "sitl":
        command = (f"TERRA_DRONE_TOKEN={token} python3 scripts/sitl.py add --id {shlex.quote(body.id)} --name {shlex.quote(body.name)} "
                   f"--aircraft {'fixed-wing' if body.aircraft == 'fixed-wing' else 'quad'} --sensors {','.join(body.sensors)}" + (" --relay" if body.relay else ""))
    else:
        command = (f"TERRA_DRONE_TOKEN={token} python3 scripts/drone_bridge.py --mavlink {shlex.quote(body.connection)} " + " ".join(flags)
                   + "   # advisory: add --allow-commands to let Terra upload missions")
    log.info("issued an aircraft token for %s", body.id)
    return {"id": body.id, "token": token, "command": command, "server": str(request.base_url).rstrip("/")}


@router.get("/enrollments", dependencies=[Depends(operator)])
async def enrollments():
    return {"enrollments": await _db(fleet_db().credentials)}


@router.post("/enrollments/{drone_id}/revoke", dependencies=[Depends(operator)])
async def revoke(drone_id: str):
    if not re.fullmatch(ID, drone_id):
        raise HTTPException(404, "Unknown aircraft.")
    return {"revoked": await _db(fleet_db().revoke, drone_id)}


# ---------------------------------------------------------------- radar to drones and back
def _investigation(investigation_id: str) -> dict:
    from ..config import get_settings
    from ..store import Store
    item = Store(get_settings().database).investigation(investigation_id)
    if not item:
        raise HTTPException(404, "Unknown investigation.")
    return item


@router.post("/dispatch", dependencies=[Depends(operator)])
async def dispatch(body: Dispatch):
    """Send the fleet to verify an investigation: every changed patch becomes a target carrying its polarimetric
    hypothesis, the question to answer and the sensors that help; the area is the patches' extent."""
    from .verify import change_of, targets_from
    item = await _db(_investigation, body.investigation_id)
    spots = change_of(item.get("result") or {}).get("hotspots") or []
    if not spots:
        raise HTTPException(422, "This investigation found no changed patches to verify.")
    db = fleet_db()
    sensors = {x for d in await _db(db.drones) for x in d["capabilities"].get("sensors", [])}
    targets = targets_from(item["result"], sensors)
    w = min(x["bounds"][0] for x in spots); s = min(x["bounds"][1] for x in spots); e = max(x["bounds"][2] for x in spots); n = max(x["bounds"][3] for x in spots)
    pad = max(0.015, 0.25 * max(e - w, n - s))
    bbox = [max(-180.0, w - pad), max(-90.0, s - pad), min(180.0, e + pad), min(90.0, n + pad)]
    if bbox[2] - bbox[0] > 2 or bbox[3] - bbox[1] > 2:
        raise HTTPException(422, "The changed patches span more than 2 degrees; verify a smaller investigation.")
    if body.launch and not (bbox[0] - 0.25 <= body.launch[0] <= bbox[2] + 0.25 and bbox[1] - 0.25 <= body.launch[1] <= bbox[3] + 0.25):
        raise HTTPException(422, "Launch from inside the patches' area or close to it.")
    title = (item.get("title") or "Investigation").split(" · ")[0]
    area = await _db(db.set_area, f"{title} · field verification", bbox, targets, body.investigation_id, "change", body.launch)
    await _db(db.message, "operator", "all", "dispatch", {"text": f"Verifying {len(targets)} radar patches from {title}: " +
                                                          "; ".join(f"{t['label']}" for t in targets[:4]) + ("…" if len(targets) > 4 else "")})
    running = await ensure_coordinator("verification dispatch")
    return {"area": area, "targets": targets, "coordinator": running}


@router.get("/evidence/{investigation_id}", dependencies=[Depends(operator)])
async def field_evidence(investigation_id: str):
    """For each patch of an investigation: the field notes that looked inside it, judged against its hypothesis."""
    from .verify import evidence
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", investigation_id):
        raise HTTPException(404, "Unknown investigation.")
    item = await _db(_investigation, investigation_id)
    notes = await _db(fleet_db().notes, 500)
    # Notes taken before optical sampling existed: read the Sentinel-2 before/after at their ground points now.
    if any("optical" not in (n.get("data") or {}) for n in notes) and item.get("bbox"):
        from .observe import area_data, observe

        def enrich() -> list[dict]:
            data = area_data(item["bbox"], investigation_id)
            if "optical" not in data:
                return notes
            return [n if "optical" in (n.get("data") or {}) else {**n, "data": {**(n.get("data") or {}), **{k: v for k, v in observe({"optical": data["optical"]}, n["lon"], n["lat"]).items() if k == "optical"}}} for n in notes]
        notes = await asyncio.to_thread(enrich)
    rows = evidence(item.get("result") or {}, notes)
    totals = {k: sum(r["counts"][k] for r in rows) for k in ("supports", "contradicts", "inconclusive")}
    return {"investigation_id": investigation_id, "patches": rows, "totals": totals,
            "method": "Each field note inside a patch is judged against the patch's polarimetric hypothesis using only measured values "
                      "(ESA WorldCover 2021, Landsat surface temperature). Evidence to weigh, not a classification."}
