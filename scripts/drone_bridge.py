#!/usr/bin/env python3
"""Ground-station bridge: connect drones to Terra's fleet coordinator through the central database.

Real aircraft over MAVLink (PX4 or ArduPilot; `pip install pymavlink`):
  TERRA_DRONE_TOKEN=... python scripts/drone_bridge.py --mavlink udpin:0.0.0.0:14550 --id uav-1 --name "Ridge One" --allow-commands

ArduPilot SITL (the real flight firmware on a simulated airframe; see scripts/sitl.py, which starts these for you):
  python scripts/drone_bridge.py --mavlink tcp:127.0.0.1:5760 --id D-02 --origin sitl --allow-commands --allow-arming --allow-manual

Terra's lightweight virtual fleet (kinematic models, no autopilot; for machines without SITL):
  TERRA_DRONE_TOKEN=... python scripts/drone_bridge.py --virtual 5

Protocol (all over HTTPS with the bearer token):
  1. POST /api/fleet/register     identity and capabilities (sensors, relay radio, speed, endurance, autopilot)
  2. POST /api/fleet/telemetry    twice a second, with an increasing sequence number; the reply carries the task
  3. POST /api/fleet/tasks/{id}/ack   acknowledged when the autopilot accepts the mission, active on station,
                                      completed / failed / rejected

With --allow-commands every task becomes a MAVLink mission uploaded with the mission protocol: orbits are
LOITER_TURNS, holds and relays LOITER_UNLIM, surveys and perimeters waypoint loops, return is RTL. Without it, tasks
are advisory (acknowledged, shown to the pilot, not sent). --allow-arming lets the bridge arm and take off for a new
task (SITL, or with a pilot ready to take over); --allow-manual accepts browser setpoints in GUIDED mode and is
refused for anything but simulated aircraft. The pilot in command can always override.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
EARTH = 6_371_000.0


def token_from_env() -> str:
    if os.getenv("TERRA_DRONE_TOKEN"):
        return os.environ["TERRA_DRONE_TOKEN"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("TERRA_DRONE_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def offset(lon: float, lat: float, east_m: float, north_m: float) -> tuple[float, float]:
    return lon + math.degrees(east_m / (EARTH * math.cos(math.radians(lat)))), lat + math.degrees(north_m / EARTH)


def distance(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH * math.asin(min(1.0, math.sqrt(a)))


def bearing(lon1, lat1, lon2, lat2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360


class Terra:
    """The connection to Terra: retries quietly, exits on authentication errors."""

    def __init__(self, server: str, token: str):
        self.client = httpx.Client(base_url=server.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=5)

    def call(self, method: str, path: str, body: dict | None = None) -> dict | None:
        try:
            response = self.client.request(method, path, json=body)
            if response.status_code in (401, 403):
                sys.exit(f"Terra refused the connection: {response.json().get('detail')}")
            if response.status_code == 409:
                return {"register": True}
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            print(f"  {path}: not delivered ({exc.__class__.__name__}); will retry", file=sys.stderr, flush=True)
            return None


# ---------------------------------------------------------------- virtual aircraft
FLEET = [
    ("D-01", "Kestrel", "fixed-wing", {"sensors": ["eo", "multispectral"], "relay": False, "cruise_mps": 22, "max_mps": 30, "endurance_min": 95}),
    ("D-02", "Harrier", "quad", {"sensors": ["eo", "ir"], "relay": False, "cruise_mps": 10, "max_mps": 16, "endurance_min": 38}),
    ("D-03", "Swift", "quad", {"sensors": ["eo", "ir"], "relay": False, "cruise_mps": 11, "max_mps": 17, "endurance_min": 35}),
    ("D-04", "Osprey", "quad", {"sensors": ["eo", "lidar"], "relay": False, "cruise_mps": 10, "max_mps": 16, "endurance_min": 40}),
    ("D-05", "Wren", "quad", {"sensors": ["eo"], "relay": True, "cruise_mps": 8, "max_mps": 14, "endurance_min": 45}),
]


class Virtual:
    """A virtual aircraft: flies its task like an autopilot in an automatic mode would, drains its battery,
    lands to swap batteries, and acknowledges tasks exactly as a real ground station should."""

    def __init__(self, ident: str, name: str, aircraft: str, caps: dict, launch: tuple[float, float], slot: int):
        self.id, self.name, self.aircraft, self.caps = ident, name, aircraft, {**caps, "protocol": "terra-sim", "autopilot": "Terra virtual autopilot"}
        self.launch = launch; self.slot = slot
        self.lon, self.lat = offset(launch[0], launch[1], 25 * math.cos(slot * 1.3), 25 * math.sin(slot * 1.3))
        self.agl, self.heading, self.speed = 0.0, random.uniform(0, 360), 0.0
        self.battery = [100, 92, 88, 96, 84][slot % 5] * 1.0
        self.landed, self.ground_since = True, time.monotonic() - 60
        self.task: dict | None = None; self.acked: dict[str, str] = {}; self.waypoint = 0; self.seq = 0
        self.gimbal = {"heading_deg": self.heading, "pitch_deg": -30.0}

    def relocate(self, launch: tuple[float, float]) -> None:
        """A new monitoring area far away: the aircraft are taken there and wait on the ground."""
        self.launch = launch
        self.lon, self.lat = offset(launch[0], launch[1], 25 * math.cos(self.slot * 1.3), 25 * math.sin(self.slot * 1.3))
        self.agl, self.speed, self.landed, self.ground_since, self.task = 0.0, 0.0, True, time.monotonic(), None

    def _route(self, kind: str, p: dict) -> list[tuple[float, float]]:
        if kind == "search":
            return search_route(p)
        if kind == "survey":
            w, s, e, n = p["bbox"]; lanes = max(3, int(distance(w, s, w, n) / max(60, p.get("lane_m", 150))))
            pts = []
            for i in range(lanes + 1):
                lat = s + (n - s) * i / lanes
                pts += [(w, lat), (e, lat)] if i % 2 == 0 else [(e, lat), (w, lat)]
            return pts
        w, s, e, n = p["bbox"]; k = p.get("inset", 0.08)
        dx, dy = (e - w) * k, (n - s) * k
        return [(w + dx, s + dy), (e - dx, s + dy), (e - dx, n - dy), (w + dx, n - dy)]

    def step(self, dt: float, control: dict | None) -> tuple[dict, list[tuple[str, str, str | None]]]:
        """Advance dt seconds. Returns telemetry and any acknowledgements to send (task id, status, detail)."""
        acks: list[tuple[str, str, str | None]] = []
        task = self.task; kind = (task or {}).get("kind"); p = (task or {}).get("params", {})
        if not task and not control and not self.landed:
            # No task (released, or between tasks): loiter where it is rather than fly on.
            self.hold_point = getattr(self, "hold_point", None) or (self.lon, self.lat)
            kind, p = "standby", {"lon": self.hold_point[0], "lat": self.hold_point[1], "radius_m": 60, "agl_m": max(60, self.agl)}
        elif task:
            self.hold_point = None
        cruise, top = self.caps["cruise_mps"], self.caps["max_mps"]
        want_speed, want_agl, target, on_station, mode = 0.0, self.agl, None, False, "HOLD"
        gimbal = {"heading_deg": self.heading, "pitch_deg": -30.0}
        if control:
            target = (control["lon"], control["lat"]); want_agl = control["agl_m"]; want_speed = min(top, distance(self.lon, self.lat, *target) / 1.2)
            mode = "MANUAL"; gimbal = {"heading_deg": control["heading_deg"], "pitch_deg": control.get("gimbal_pitch_deg", -35)}
        elif kind in ("orbit", "relay", "standby", "hold"):
            c = (p["lon"], p["lat"]); r = float(p.get("radius_m", 40)); want_agl = p.get("agl_m", 120)
            d = distance(self.lon, self.lat, *c)
            if kind == "hold" and self.aircraft != "fixed-wing":
                target, want_speed, on_station = c, min(cruise, d / 3), d < 25 and abs(self.agl - want_agl) < 25
            else:
                r = max(r, 160 if self.aircraft == "fixed-wing" else 25)
                if abs(d - r) > 40:
                    target = offset(c[0], c[1], *(lambda b: (r * math.sin(b), r * math.cos(b)))(math.radians(bearing(*c, self.lon, self.lat))))
                    want_speed = cruise
                else:  # on the circle: fly tangentially, clockwise, correcting the radius
                    b = math.radians(bearing(*c, self.lon, self.lat) + math.degrees(min(0.6, (p.get("speed_mps", cruise) * 3) / r)))
                    target = offset(c[0], c[1], r * math.sin(b), r * math.cos(b)); want_speed = p.get("speed_mps", cruise); on_station = abs(self.agl - want_agl) < 25
            gimbal = {"heading_deg": bearing(self.lon, self.lat, *c), "pitch_deg": -math.degrees(math.atan2(max(10, self.agl), max(1, d)))} if kind == "orbit" else \
                     {"heading_deg": (time.monotonic() * 8) % 360, "pitch_deg": -25.0} if kind == "relay" else {"heading_deg": self.heading, "pitch_deg": -60.0}
            mode = "AUTO"
        elif kind in ("survey", "perimeter", "search"):
            route = self._route(kind, p); want_agl = p.get("agl_m", 150)
            self.waypoint %= len(route); target = route[self.waypoint]
            if distance(self.lon, self.lat, *target) < max(30, self.speed * 2.5):
                self.waypoint = (self.waypoint + 1) % len(route); target = route[self.waypoint]
            want_speed = p.get("speed_mps", cruise)
            # On station once at working height and flying the route (not while climbing out or transiting).
            on_station = abs(self.agl - want_agl) < 25 and (self.waypoint > 0 or distance(self.lon, self.lat, *target) < 400)
            gimbal = {"heading_deg": self.heading, "pitch_deg": -80.0} if kind == "survey" else \
                     {"heading_deg": self.heading, "pitch_deg": -55.0} if kind == "search" else {"heading_deg": (self.heading - 90) % 360, "pitch_deg": -42.0}
            mode = "AUTO"
        elif kind == "return":
            target = tuple(self.launch); d = distance(self.lon, self.lat, *target)
            want_speed = min(cruise, d / 4); want_agl = p.get("agl_m", 90) if d > 40 else 0.0; mode = "RTL"
            gimbal = {"heading_deg": self.heading, "pitch_deg": -20.0}
        # Take off when there is work, one aircraft at a time (a few seconds apart, as a ground crew launches them);
        # land when told to come home and over the pad.
        if self.landed and (control or (kind and kind != "return" and self.battery > 30)):
            self.launch_at = getattr(self, "launch_at", None) or time.monotonic() + self.slot * 8
            if control or time.monotonic() >= self.launch_at:
                self.landed, self.launch_at = False, None
        if self.landed:
            if time.monotonic() - self.ground_since > 30 and self.battery < 99:
                self.battery = 100.0  # battery swapped on the ground
            return self._telemetry(gimbal, "LANDED"), acks
        # Departure: climb straight up to this aircraft's own departure height before setting off.
        departure = 35 + self.slot * 15
        if target and self.aircraft != "fixed-wing" and self.agl < min(want_agl, departure) - 3 and distance(self.lon, self.lat, *self.launch) < 120:
            want_speed = 0.0
        # Kinematics: turn, accelerate and climb at the aircraft's limits.
        if target:
            want_heading = bearing(self.lon, self.lat, *target)
            turn = ((want_heading - self.heading + 540) % 360) - 180
            rate = 22 if self.aircraft == "fixed-wing" else 120
            self.heading = (self.heading + max(-rate * dt, min(rate * dt, turn))) % 360
        floor = 14.0 if self.aircraft == "fixed-wing" else 0.0
        self.speed += max(-3 * dt, min(2.5 * dt, max(floor, want_speed) - self.speed))
        self.lon, self.lat = offset(self.lon, self.lat, math.sin(math.radians(self.heading)) * self.speed * dt, math.cos(math.radians(self.heading)) * self.speed * dt)
        self.agl += max(-3.5 * dt, min(3.5 * dt, want_agl - self.agl))
        self.battery = max(0.0, self.battery - dt * 100 / (self.caps["endurance_min"] * 60) * (0.8 + 0.4 * self.speed / top))
        if kind == "return" and self.agl < 1.5 and distance(self.lon, self.lat, *self.launch) < 40:
            self.landed, self.agl, self.speed, self.ground_since = True, 0.0, 0.0, time.monotonic()
            acks.append((task["id"], "completed", "landed at the launch site"))
        if task and self.acked.get(task["id"]) == "acknowledged" and (on_station or kind == "return"):
            acks.append((task["id"], "active", None))
        self.gimbal = gimbal
        return self._telemetry(gimbal, mode), acks

    def _telemetry(self, gimbal: dict, mode: str) -> dict:
        self.seq += 1
        d = distance(self.lon, self.lat, *self.launch)
        return {"id": self.id, "seq": self.seq, "lon": round(self.lon, 7), "lat": round(self.lat, 7), "alt_m": round(self.agl, 1), "agl_m": round(self.agl, 1),
                "heading_deg": round(self.heading % 360, 1), "speed_mps": round(self.speed, 2), "battery_pct": round(self.battery, 1),
                "link_dbm": round(-48 - 9 * math.log10(max(30, d)) - random.uniform(0, 1.5), 1), "mode": mode, "landed": self.landed,
                "gimbal": {"heading_deg": round(gimbal["heading_deg"] % 360, 1), "pitch_deg": round(max(-90, min(30, gimbal["pitch_deg"])), 1)}}


def run_virtual(terra: Terra, count: int, launch: tuple[float, float] | None, rate: float) -> None:
    area = None
    while area is None:
        info = terra.call("GET", "/api/fleet/area")
        area = (info or {}).get("area")
        if area is None:
            if launch:
                area = {"launch": list(launch)}
            else:
                print("Waiting for a monitoring area to be set in the workspace…", flush=True); time.sleep(5)
    fleet = [Virtual(i, n, a, c, tuple(area["launch"]), k) for k, (i, n, a, c) in enumerate(FLEET[:max(1, min(5, count))])]
    for v in fleet:
        terra.call("POST", "/api/fleet/register", {"id": v.id, "name": v.name, "origin": "virtual", "aircraft": v.aircraft, "capabilities": v.caps})
    print(f"Virtual fleet of {len(fleet)} connected at {area['launch'][1]:.4f}, {area['launch'][0]:.4f}.", flush=True)
    last = time.monotonic()
    while True:
        now = time.monotonic(); dt = min(1.0, now - last); last = now
        for v in fleet:
            control = getattr(v, "control", None)
            telemetry, acks = v.step(dt, control)
            reply = terra.call("POST", "/api/fleet/telemetry", telemetry)
            if not reply:
                continue
            if reply.get("register"):
                terra.call("POST", "/api/fleet/register", {"id": v.id, "name": v.name, "origin": "virtual", "aircraft": v.aircraft, "capabilities": v.caps}); continue
            v.control = reply.get("control")
            new_area = reply.get("area")
            if new_area and distance(*new_area["launch"], *v.launch) > 2_000:  # deployed elsewhere: the crew takes the fleet there
                v.relocate(tuple(new_area["launch"])); print(f"  {v.id}: moved to the new area", flush=True); continue
            task = reply.get("task")
            if task and (v.task or {}).get("id") != task["id"]:
                v.task, v.waypoint = task, 0
                terra.call("POST", f"/api/fleet/tasks/{task['id']}/ack", {"id": v.id, "status": "acknowledged"}); v.acked[task["id"]] = "acknowledged"
            elif task:
                v.task = task  # parameters may have been adjusted (for example a new altitude layer)
            elif not task and v.task and v.task.get("kind") != "return":
                v.task = None
            for task_id, status, detail in acks:
                if v.acked.get(task_id) != status:
                    terra.call("POST", f"/api/fleet/tasks/{task_id}/ack", {"id": v.id, "status": status, "detail": detail}); v.acked[task_id] = status
        time.sleep(max(0.0, 1 / rate - (time.monotonic() - now)))


# ---------------------------------------------------------------- MAVLink (real aircraft and ArduPilot SITL)
CMD_WAYPOINT, CMD_LOITER_UNLIM, CMD_LOITER_TURNS, CMD_TAKEOFF, CMD_DO_JUMP, CMD_CHANGE_SPEED = 16, 17, 18, 22, 177, 178
FRAME_GLOBAL, FRAME_RELATIVE = 0, 6


def search_route(p: dict) -> list[tuple[float, float]]:
    """An expanding-square search from a datum: legs of one, one, two, two, three, three … lane spacings, turning
    right each time, until the square covers the search size (the same pattern the coordinator plans)."""
    lon, lat = p["lon"], p["lat"]; lane = float(p.get("lane_m", 120)); size = float(p.get("size_m", 1200))
    pts = [(lon, lat)]; x = y = 0.0; heading = 0; leg = 1
    while max(abs(x), abs(y)) * 2 < size and len(pts) < 80:
        for _ in range(2):
            dx, dy = [(0, 1), (1, 0), (0, -1), (-1, 0)][heading % 4]
            x += dx * leg * lane; y += dy * leg * lane; heading += 1
            pts.append(offset(lon, lat, x, y))
        leg += 1
    return pts


def route_points(kind: str, p: dict) -> list[tuple[float, float]]:
    """Waypoints (lon, lat) for route tasks: lawnmower lanes for a survey, an inset rectangle for a perimeter,
    an expanding square for a search."""
    if kind == "search":
        return search_route(p)
    w, s, e, n = p["bbox"]
    if kind == "survey":
        lanes = max(3, int(distance(w, s, w, n) / max(60, p.get("lane_m", 150))))
        pts = []
        for i in range(lanes + 1):
            lat = s + (n - s) * i / lanes
            pts += [(w, lat), (e, lat)] if i % 2 == 0 else [(e, lat), (w, lat)]
        return pts
    k = p.get("inset", 0.08); dx, dy = (e - w) * k, (n - s) * k
    return [(w + dx, s + dy), (e - dx, s + dy), (e - dx, n - dy), (w + dx, n - dy)]


def mission_items(task: dict, home: tuple[float, float, float], airborne: bool, aircraft: str) -> list[dict]:
    """An ArduPilot mission for a Terra task. Item 0 is the home position (ArduPilot convention); altitudes are
    relative to home. Routes and orbits end with DO_JUMP back to their start so they repeat until re-tasked."""
    kind, p = task["kind"], task["params"]; plane = aircraft == "fixed-wing"
    alt = float(p.get("agl_m", 120)); speed = float(p.get("speed_mps", 22 if plane else 10))
    item = lambda command, lat=0.0, lon=0.0, z=0.0, p1=0.0, p2=0.0, p3=0.0, p4=0.0, frame=FRAME_RELATIVE: (  # noqa: E731
        {"command": command, "frame": frame, "lat": lat, "lon": lon, "alt": z, "p1": p1, "p2": p2, "p3": p3, "p4": p4})
    items = [item(CMD_WAYPOINT, home[1], home[0], home[2], frame=FRAME_GLOBAL)]
    if not airborne:
        items.append(item(CMD_TAKEOFF, home[1], home[0], min(alt, 60.0), p1=12.0 if plane else 0.0))
    items.append(item(CMD_CHANGE_SPEED, p1=0.0 if plane else 1.0, p2=speed, p3=-1.0))
    if kind == "orbit":
        radius = float(p.get("radius_m", 200)); radius = max(80.0, radius) if plane else min(250.0, max(20.0, radius))
        first = len(items)
        items.append(item(CMD_LOITER_TURNS, p["lat"], p["lon"], alt, p1=254.0, p3=radius))
        items.append(item(CMD_DO_JUMP, p1=float(first), p2=-1.0))
    elif kind in ("hold", "relay", "standby"):
        items.append(item(CMD_LOITER_UNLIM, p["lat"], p["lon"], alt, p3=float(p.get("radius_m", 60)) if plane else 0.0))
    elif kind in ("survey", "perimeter", "search"):
        first = len(items)
        for lon, lat in route_points(kind, p):
            items.append(item(CMD_WAYPOINT, lat, lon, alt))
        items.append(item(CMD_DO_JUMP, p1=float(first), p2=-1.0))
    else:
        raise ValueError(f"no mission for {kind} tasks")
    return items


def on_station(task: dict, lon: float, lat: float, agl: float | None) -> bool:
    """Whether the aircraft is doing its task where it was asked to, at roughly the asked height."""
    p = task["params"]; kind = task["kind"]
    if agl is not None and "agl_m" in p and abs(agl - float(p["agl_m"])) > 30:
        return False
    if "bbox" in p:
        w, s, e, n = p["bbox"]; return w - 0.002 <= lon <= e + 0.002 and s - 0.002 <= lat <= n + 0.002
    if kind == "search":
        return distance(lon, lat, p["lon"], p["lat"]) < float(p.get("size_m", 1200)) * 0.75 + 150
    if "lon" in p:
        return distance(lon, lat, p["lon"], p["lat"]) < float(p.get("radius_m", 60)) + 120
    return True


def pointing(task: dict | None, lon: float, lat: float, agl: float, heading: float) -> dict:
    """Where a camera on this aircraft would point for its task (used when the autopilot reports no gimbal)."""
    if task and task["kind"] in ("orbit", "hold") and "lon" in task["params"]:
        p = task["params"]; d = max(1.0, distance(lon, lat, p["lon"], p["lat"]))
        return {"heading_deg": round(bearing(lon, lat, p["lon"], p["lat"]), 1), "pitch_deg": round(-math.degrees(math.atan2(max(10.0, agl), d)), 1)}
    if task and task["kind"] == "survey":
        return {"heading_deg": round(heading, 1), "pitch_deg": -80.0}
    if task and task["kind"] == "search":
        return {"heading_deg": round(heading, 1), "pitch_deg": -55.0}
    if task and task["kind"] == "perimeter":
        return {"heading_deg": round((heading - 90) % 360, 1), "pitch_deg": -42.0}
    return {"heading_deg": round(heading, 1), "pitch_deg": -35.0}


class Autopilot:
    """One aircraft over MAVLink (PX4, ArduPilot, or ArduPilot SITL). Missions are uploaded with the MAVLink
    mission protocol; arming, takeoff and manual setpoints happen only when explicitly allowed."""

    def __init__(self, terra: Terra, connection: str, spec: dict, allow_commands: bool, allow_arming: bool, allow_manual: bool, video_url: str | None, rate: float):
        from pymavlink import mavutil
        self.mavutil, self.m = mavutil, mavutil.mavlink
        self.terra, self.spec, self.rate, self.video_url = terra, spec, rate, video_url
        self.allow_commands, self.allow_arming, self.allow_manual = allow_commands, allow_arming, allow_manual
        print(f"{spec['id']}: connecting to {connection}…", flush=True)
        self.link = mavutil.mavlink_connection(connection, source_system=254, autoreconnect=True)
        self.link.wait_heartbeat(timeout=60)
        hb = self.link.messages.get("HEARTBEAT")
        self.autopilot = {3: "ArduPilot", 12: "PX4"}.get(getattr(hb, "autopilot", None), "MAVLink")
        self.state: dict = {}; self.seq = 0; self.task: dict | None = None; self.acked: dict[str, str] = {}
        self.home: tuple[float, float, float] | None = None; self.guided = False; self.last_setpoint = 0.0
        self.link.mav.request_data_stream_send(self.link.target_system, self.link.target_component, self.m.MAV_DATA_STREAM_ALL, 4, 1)

    # -- readiness, registration and telemetry
    def wait_ready(self, timeout: float = 180) -> None:
        """Join the fleet only when the autopilot can fly: a 3D GPS fix, a home position and a horizontal position estimate."""
        ts, tc = self.link.target_system, self.link.target_component
        deadline = time.monotonic() + timeout; fix = ekf = False; last = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - last > 3:
                last = time.monotonic()
                self.link.mav.command_long_send(ts, tc, self.m.MAV_CMD_GET_HOME_POSITION, 0, 0, 0, 0, 0, 0, 0, 0)
            msg = self.link.recv_match(type=["GPS_RAW_INT", "EKF_STATUS_REPORT", "HOME_POSITION", "GLOBAL_POSITION_INT", "HEARTBEAT", "SYS_STATUS"], blocking=True, timeout=1)
            if msg is None:
                continue
            kind = msg.get_type()
            if kind == "GPS_RAW_INT":
                fix = msg.fix_type >= 3
            elif kind == "EKF_STATUS_REPORT":
                ekf = bool(msg.flags & 0x10)  # EKF_POS_HORIZ_ABS
            elif kind == "HOME_POSITION":
                self.home = (msg.longitude / 1e7, msg.latitude / 1e7, msg.altitude / 1000)
            if fix and (ekf or self.autopilot != "ArduPilot") and self.home:
                print(f"{self.spec['id']}: ready (GPS 3D fix, position estimate, home set)", flush=True); return
        print(f"{self.spec['id']}: not fully ready after {timeout:.0f} s (gps {fix}, ekf {ekf}, home {bool(self.home)}); joining anyway", flush=True)

    def register(self) -> None:
        caps = {"sensors": self.spec["sensors"], "relay": self.spec["relay"], "protocol": "mavlink", "autopilot": self.autopilot,
                "cruise_mps": self.spec["cruise"], "max_mps": self.spec["cruise"] * 1.6, "endurance_min": self.spec["endurance"],
                "tasks": ["orbit", "survey", "perimeter", "search", "relay", "hold", "standby", "return"]}
        self.terra.call("POST", "/api/fleet/register", {"id": self.spec["id"], "name": self.spec["name"], "origin": self.spec["origin"], "aircraft": self.spec["aircraft"], "capabilities": caps})
        print(f"{self.spec['id']}: registered ({self.autopilot}, {self.spec['origin']}); commands {'on' if self.allow_commands else 'advisory'}", flush=True)

    def read(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            msg = self.link.recv_match(blocking=True, timeout=max(0.01, end - time.monotonic()))
            if msg is None:
                continue
            kind = msg.get_type(); st = self.state
            if kind == "GLOBAL_POSITION_INT":
                st.update(lat=msg.lat / 1e7, lon=msg.lon / 1e7, alt=msg.alt / 1000, rel=msg.relative_alt / 1000,
                          hdg=(msg.hdg / 100) if msg.hdg != 65535 else st.get("hdg"), speed=math.hypot(msg.vx, msg.vy) / 100)
            elif kind == "HOME_POSITION":
                self.home = (msg.longitude / 1e7, msg.latitude / 1e7, msg.altitude / 1000)
            elif kind == "SYS_STATUS" and msg.battery_remaining >= 0:
                st["battery"] = float(msg.battery_remaining)
            elif kind == "TERRAIN_REPORT" and msg.current_height > 0:
                st["agl"] = float(msg.current_height)
            elif kind == "STATUSTEXT" and msg.text.startswith(("PreArm", "Arm")):
                st["arm_reason"] = msg.text.strip()  # the autopilot's own words, passed on when it will not arm
            elif kind == "EXTENDED_SYS_STATE":
                st["on_ground"] = msg.landed_state == self.m.MAV_LANDED_STATE_ON_GROUND
            elif kind == "HEARTBEAT" and msg.get_srcComponent() == 1:
                st["armed"] = bool(msg.base_mode & self.m.MAV_MODE_FLAG_SAFETY_ARMED); st["mode"] = self.mavutil.mode_string_v10(msg)
            elif kind == "GIMBAL_DEVICE_ATTITUDE_STATUS":
                w, x, y, z = msg.q
                st["gimbal"] = {"heading_deg": round(((st.get("hdg") or 0) + math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))) % 360, 1),
                                "pitch_deg": round(max(-90, min(30, math.degrees(math.asin(max(-1, min(1, 2 * (w * y - z * x))))))), 1)}

    def landed(self) -> bool:
        st = self.state
        return bool(st.get("on_ground")) if "on_ground" in st else not st.get("armed", False)

    def telemetry(self) -> dict | None:
        st = self.state
        if "lat" not in st:
            return None
        if self.home is None:
            self.home = (st["lon"], st["lat"], st["alt"] - st.get("rel", 0))
        self.seq += 1
        agl = st.get("agl", max(0.0, st.get("rel", 0.0)))  # terrain report when available, else height above home
        hdg = (st.get("hdg") or 0) % 360
        t = {"id": self.spec["id"], "seq": self.seq, "lon": st["lon"], "lat": st["lat"], "alt_m": round(st["alt"], 1), "agl_m": round(agl, 1),
             "heading_deg": round(hdg, 1), "speed_mps": round(st.get("speed") or 0, 2), "mode": (st.get("mode") or self.autopilot)[:32], "landed": self.landed(),
             "gimbal": st.get("gimbal") or pointing(self.task, st["lon"], st["lat"], agl, hdg)}
        if st.get("battery") is not None:
            t["battery_pct"] = st["battery"]
        if self.spec["origin"] == "sitl":  # simulated radio: estimated from distance to the pad
            d = distance(st["lon"], st["lat"], self.home[0], self.home[1]); t["link_dbm"] = round(-48 - 9 * math.log10(max(30.0, d)), 1)
        if self.video_url:
            t["video_url"] = self.video_url
        return t

    # -- commands
    def command(self, command: int, *params: float) -> bool:
        p = list(params) + [0.0] * (7 - len(params))
        self.link.mav.command_long_send(self.link.target_system, self.link.target_component, command, 0, *p)
        ack = self.link.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
        return bool(ack and ack.command == command and ack.result in (self.m.MAV_RESULT_ACCEPTED, self.m.MAV_RESULT_IN_PROGRESS))

    def set_mode(self, name: str) -> bool:
        mapping = self.link.mode_mapping() or {}
        if name not in mapping:
            return False
        self.link.set_mode(mapping[name])
        for _ in range(20):
            self.read(0.15)
            if (self.state.get("mode") or "").upper() == name:
                return True
        return False

    def upload(self, items: list[dict]) -> str | None:
        """MAVLink mission protocol: count, then answer each request; returns None on success or the reason."""
        ts, tc = self.link.target_system, self.link.target_component
        self.link.mav.mission_count_send(ts, tc, len(items), self.m.MAV_MISSION_TYPE_MISSION)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            msg = self.link.recv_match(type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"], blocking=True, timeout=2)
            if msg is None:
                continue
            if msg.get_type() == "MISSION_ACK":
                return None if msg.type == self.m.MAV_MISSION_ACCEPTED else f"autopilot refused the mission (code {msg.type})"
            it = items[msg.seq]
            self.link.mav.mission_item_int_send(ts, tc, msg.seq, it["frame"], it["command"], 0, 1, it["p1"], it["p2"], it["p3"], it["p4"],
                                                int(it["lat"] * 1e7), int(it["lon"] * 1e7), float(it["alt"]), self.m.MAV_MISSION_TYPE_MISSION)
        return "mission upload timed out"

    def ack(self, status: str, detail: str | None = None) -> None:
        if not self.task or self.acked.get(self.task["id"]) == status:
            return
        self.terra.call("POST", f"/api/fleet/tasks/{self.task['id']}/ack", {"id": self.spec["id"], "status": status, "detail": detail})
        self.acked[self.task["id"]] = status

    def execute(self, task: dict) -> None:
        self.task = task; self.guided = False
        print(f"{self.spec['id']}: task {task['kind']} — {task['label']}", flush=True)
        if not self.allow_commands:
            self.ack("acknowledged", "advisory: shown to the pilot, not sent to the autopilot"); return
        if task["kind"] == "return":
            self.ack("acknowledged"); ok = self.set_mode("RTL")
            self.ack("active" if ok else "rejected", "returning to launch" if ok else "the autopilot did not enter RTL"); return
        airborne = not self.landed()
        try:
            items = mission_items(task, self.home or (self.state["lon"], self.state["lat"], self.state["alt"]), airborne, self.spec["aircraft"])
        except ValueError as exc:
            self.ack("rejected", str(exc)); return
        problem = self.upload(items)
        if problem:
            self.ack("rejected", problem); return
        self.ack("acknowledged", f"mission of {len(items)} items accepted by {self.autopilot}")
        if airborne:
            self.set_mode("AUTO"); self.command(224, 1)  # MAV_CMD_DO_SET_MISSION_CURRENT: start from the first item
        elif self.allow_arming:
            self.set_mode("AUTO")
            armed = False; self.state.pop("arm_reason", None)
            for _ in range(5):  # pre-arm checks can take a few seconds to clear
                if self.command(self.m.MAV_CMD_COMPONENT_ARM_DISARM, 1):
                    armed = True; break
                self.read(2)
            if not armed:
                why = self.state.get("arm_reason")
                print(f"{self.spec['id']}: will not arm{f' ({why})' if why else ''}", flush=True)
                self.ack("rejected", f"vehicle: the autopilot would not arm{f' ({why})' if why else ''}"); return
            if self.spec["aircraft"] != "fixed-wing":
                self.command(self.m.MAV_CMD_MISSION_START, 0, 0)
        else:
            print(f"{self.spec['id']}: mission loaded; arming is left to the pilot", flush=True)

    def manual(self, sp: dict) -> None:
        """A browser setpoint for a simulated aircraft: GUIDED mode, then fly to the point (copters also face its heading)."""
        if not self.allow_manual or time.monotonic() - self.last_setpoint < 0.25:
            return
        self.last_setpoint = time.monotonic()
        if not self.guided:
            self.guided = self.set_mode("GUIDED")
            if self.landed() and self.allow_arming and self.spec["aircraft"] != "fixed-wing":
                self.command(self.m.MAV_CMD_COMPONENT_ARM_DISARM, 1); self.command(self.m.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, max(20.0, sp["agl_m"]))
        mask = 0b0000_1001_1111_1000  # position only, plus yaw
        self.link.mav.set_position_target_global_int_send(0, self.link.target_system, self.link.target_component, self.m.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, mask,
                                                          int(sp["lat"] * 1e7), int(sp["lon"] * 1e7), float(sp["agl_m"]), 0, 0, 0, 0, 0, 0, math.radians(sp["heading_deg"]), 0)

    def run(self) -> None:
        self.wait_ready()
        self.register()
        while True:
            self.read(1 / self.rate)
            t = self.telemetry()
            if not t:
                continue
            reply = self.terra.call("POST", "/api/fleet/telemetry", t) or {}
            if reply.get("register"):
                self.register(); continue
            if reply.get("control"):
                self.manual(reply["control"]); continue
            task = reply.get("task")
            if task and (self.task or {}).get("id") != task["id"]:
                self.execute(task)
            elif task and self.task and task["params"] != self.task["params"] and self.allow_commands and task["kind"] != "return":
                self.execute(task)  # adjusted by the coordinator (for example a new altitude layer)
            elif not task:
                self.task = None
            if self.task and self.acked.get(self.task["id"]) == "acknowledged" and self.allow_commands and self.task["kind"] != "return" \
                    and on_station(self.task, t["lon"], t["lat"], t.get("agl_m")):
                self.ack("active", "on station")
            if self.task and self.task["kind"] == "return" and self.landed() and self.acked.get(self.task["id"]) == "active":
                self.ack("completed", "landed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default=os.getenv("TERRA_SERVER", f"http://127.0.0.1:{os.getenv('TERRA_API_PORT', '8010')}"))
    parser.add_argument("--token", default=None, help="Defaults to TERRA_DRONE_TOKEN (environment or .env)")
    parser.add_argument("--rate", type=float, default=2.0, help="Telemetry messages per second per aircraft")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mavlink", help="MAVLink connection, for example udpin:0.0.0.0:14550, tcp:127.0.0.1:5760 or /dev/ttyUSB0")
    group.add_argument("--virtual", type=int, help="Number of virtual aircraft (up to 5)")
    parser.add_argument("--id", default="uav-1"); parser.add_argument("--name", default="Drone 1")
    parser.add_argument("--aircraft", default="quad", choices=["quad", "fixed-wing", "vtol", "other"])
    parser.add_argument("--origin", default="real", choices=["real", "sitl"], help="sitl: ArduPilot/PX4 software-in-the-loop (simulated airframe)")
    parser.add_argument("--sensors", default="eo", help="Comma-separated: eo, ir, lidar, multispectral, gas")
    parser.add_argument("--relay", action="store_true", help="The aircraft carries a relay radio")
    parser.add_argument("--cruise", type=float, default=10.0); parser.add_argument("--endurance", type=float, default=30.0)
    parser.add_argument("--lat", type=float); parser.add_argument("--lon", type=float)
    parser.add_argument("--allow-commands", action="store_true", help="Upload missions and mode changes to the autopilot (otherwise advisory)")
    parser.add_argument("--allow-arming", action="store_true", help="Arm and take off on a new task (SITL, or with a pilot ready to intervene)")
    parser.add_argument("--allow-manual", action="store_true", help="Accept browser setpoints in GUIDED mode (simulated aircraft only)")
    parser.add_argument("--video-url", help="An http(s) MJPEG or snapshot URL for the drone's camera")
    args = parser.parse_args()
    token = args.token or token_from_env()
    if not token:
        sys.exit("Set TERRA_DRONE_TOKEN (the same value as on the Terra server) or pass --token.")
    if args.allow_manual and args.origin != "sitl":
        sys.exit("--allow-manual is only for simulated aircraft (--origin sitl).")
    terra = Terra(args.server, token)
    try:
        if args.virtual:
            run_virtual(terra, args.virtual, (args.lon, args.lat) if args.lat is not None and args.lon is not None else None, args.rate)
        else:
            try:
                import pymavlink  # noqa: F401
            except ImportError:
                sys.exit("MAVLink needs pymavlink: pip install pymavlink")
            spec = {"id": args.id, "name": args.name, "aircraft": args.aircraft, "origin": args.origin, "relay": args.relay,
                    "sensors": [x.strip() for x in args.sensors.split(",") if x.strip()], "cruise": args.cruise, "endurance": args.endurance}
            Autopilot(terra, args.mavlink, spec, args.allow_commands, args.allow_arming, args.allow_manual, args.video_url, args.rate).run()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
