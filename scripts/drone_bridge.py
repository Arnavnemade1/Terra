#!/usr/bin/env python3
"""Ground-station bridge: connect drones to Terra's fleet coordinator through the central database.

Real aircraft over MAVLink (PX4 or ArduPilot; `pip install pymavlink`):
  TERRA_DRONE_TOKEN=... python scripts/drone_bridge.py --mavlink udpin:0.0.0.0:14550 --id uav-1 --name "Ridge One"

Terra's virtual fleet (five aircraft that fly assigned tasks with simple, realistic kinematics, so the whole
system can be exercised without hardware; they speak exactly the protocol real aircraft use):
  TERRA_DRONE_TOKEN=... python scripts/drone_bridge.py --virtual 5

Protocol (all over HTTPS with the bearer token):
  1. POST /api/fleet/register     identity and capabilities (sensors, relay radio, speed, endurance, autopilot)
  2. POST /api/fleet/telemetry    twice a second, with an increasing sequence number; the reply carries the task
  3. POST /api/fleet/tasks/{id}/ack   acknowledged when received, active on station, completed / failed / rejected

Tasks are advisory for real aircraft. With MAVLink, orbit (MAV_CMD_DO_ORBIT), hold and relay
(MAV_CMD_DO_REPOSITION) and return (MAV_CMD_NAV_RETURN_TO_LAUNCH) are forwarded to the autopilot only with
--allow-commands, and only while the vehicle is in a mode that accepts them; the pilot in command can always
override. Survey and perimeter tasks are rejected with a note to plan them in the ground-control software.
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
        elif kind in ("survey", "perimeter"):
            route = self._route(kind, p); want_agl = p.get("agl_m", 150)
            self.waypoint %= len(route); target = route[self.waypoint]
            if distance(self.lon, self.lat, *target) < max(30, self.speed * 2.5):
                self.waypoint = (self.waypoint + 1) % len(route); target = route[self.waypoint]
            want_speed = p.get("speed_mps", cruise)
            # On station once at working height and flying the route (not while climbing out or transiting).
            on_station = abs(self.agl - want_agl) < 25 and (self.waypoint > 0 or distance(self.lon, self.lat, *target) < 400)
            gimbal = {"heading_deg": self.heading, "pitch_deg": -80.0} if kind == "survey" else {"heading_deg": (self.heading - 90) % 360, "pitch_deg": -42.0}
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
            if new_area and distance(*new_area["launch"], *v.launch) > 30_000:
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


# ---------------------------------------------------------------- MAVLink
def run_mavlink(terra: Terra, connection: str, ident: str, name: str, aircraft: str, allow_commands: bool, video_url: str | None, rate: float) -> None:
    try:
        from pymavlink import mavutil
    except ImportError:
        sys.exit("MAVLink needs pymavlink: pip install pymavlink")
    link = mavutil.mavlink_connection(connection, source_system=254)
    print(f"Waiting for a heartbeat on {connection}…", flush=True); link.wait_heartbeat(timeout=30)
    heartbeat = link.messages.get("HEARTBEAT")
    autopilot = {3: "ArduPilot", 12: "PX4"}.get(getattr(heartbeat, "autopilot", None), "MAVLink")
    terra.call("POST", "/api/fleet/register", {"id": ident, "name": name, "origin": "real", "aircraft": aircraft,
                                               "capabilities": {"sensors": ["eo"], "protocol": "mavlink", "autopilot": autopilot, "cruise_mps": 10, "max_mps": 16, "endurance_min": 30,
                                                                # Forwarded commands cover these; with advisory tasks the pilot can fly any.
                                                                "tasks": ["orbit", "relay", "hold", "standby", "return"] if allow_commands else
                                                                         ["orbit", "survey", "perimeter", "relay", "hold", "standby", "return"]}})
    print(f"Connected to system {link.target_system} ({autopilot}). Forwarding tasks: {'yes' if allow_commands else 'no (advisory only)'}", flush=True)
    m = mavutil.mavlink
    state: dict = {"battery": None, "agl": None, "gimbal": None, "armed": False, "landed": None}
    seq, last_post, current = 0, 0.0, None
    while True:
        msg = link.recv_match(blocking=True, timeout=1)
        if msg is not None:
            kind = msg.get_type()
            if kind == "GLOBAL_POSITION_INT":
                state.update(lat=msg.lat / 1e7, lon=msg.lon / 1e7, alt=msg.alt / 1000, rel=msg.relative_alt / 1000,
                             hdg=(msg.hdg / 100) if msg.hdg != 65535 else None, speed=math.hypot(msg.vx, msg.vy) / 100)
            elif kind == "SYS_STATUS" and msg.battery_remaining >= 0:
                state["battery"] = float(msg.battery_remaining)
            elif kind == "TERRAIN_REPORT":
                state["agl"] = float(msg.current_height)
            elif kind == "EXTENDED_SYS_STATE":
                state["landed"] = msg.landed_state == m.MAV_LANDED_STATE_ON_GROUND
            elif kind == "HEARTBEAT":
                state["armed"] = bool(msg.base_mode & m.MAV_MODE_FLAG_SAFETY_ARMED); state["mode"] = mavutil.mode_string_v10(msg)
            elif kind == "GIMBAL_DEVICE_ATTITUDE_STATUS":
                w, x, y, z = msg.q
                pitch = math.degrees(math.asin(max(-1, min(1, 2 * (w * y - z * x))))); yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
                state["gimbal"] = {"heading_deg": round(((state.get("hdg") or 0) + yaw) % 360, 1), "pitch_deg": round(max(-90, min(30, pitch)), 1)}
        if "lat" not in state or time.monotonic() - last_post < 1 / rate:
            continue
        last_post = time.monotonic(); seq += 1
        telemetry = {"id": ident, "seq": seq, "lon": state["lon"], "lat": state["lat"], "alt_m": state["alt"], "heading_deg": round((state.get("hdg") or 0) % 360, 1),
                     "speed_mps": round(state.get("speed") or 0, 2), "mode": state.get("mode", "MAVLink")[:32], "landed": bool(state.get("landed"))}
        for key, field in (("agl", "agl_m"), ("battery", "battery_pct"), ("gimbal", "gimbal")):
            if state.get(key) is not None:
                telemetry[field] = state[key]
        if video_url:
            telemetry["video_url"] = video_url
        reply = terra.call("POST", "/api/fleet/telemetry", telemetry) or {}
        task = reply.get("task")
        if not task or task["id"] == current:
            continue
        current = task["id"]; print(f"Task {task['id']}: {task['kind']} {task['label']}", flush=True)
        ack = lambda status, detail=None: terra.call("POST", f"/api/fleet/tasks/{task['id']}/ack", {"id": ident, "status": status, "detail": detail})  # noqa: E731
        if not allow_commands:
            ack("acknowledged", "advisory: shown to the pilot, not sent to the autopilot"); continue
        p = task["params"]; alt = float(p.get("agl_m", 100))
        if task["kind"] == "orbit":
            link.mav.command_int_send(link.target_system, link.target_component, m.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, m.MAV_CMD_DO_ORBIT, 0, 0,
                                      float(p.get("radius_m", 250)), float("nan"), 0, 0, int(p["lat"] * 1e7), int(p["lon"] * 1e7), alt)
        elif task["kind"] in ("hold", "relay", "standby"):
            link.mav.command_int_send(link.target_system, link.target_component, m.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, m.MAV_CMD_DO_REPOSITION, 0, 0,
                                      -1, 1, 0, float("nan"), int(p["lat"] * 1e7), int(p["lon"] * 1e7), alt)
        elif task["kind"] == "return":
            link.mav.command_long_send(link.target_system, link.target_component, m.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0, 0, 0, 0, 0, 0, 0, 0)
        else:
            ack("rejected", "survey and perimeter routes are planned in the ground-control software"); continue
        reply_ack = link.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
        if reply_ack and reply_ack.result == m.MAV_RESULT_ACCEPTED:
            ack("active", "accepted by the autopilot")
        else:
            ack("rejected", f"autopilot answered {getattr(reply_ack, 'result', 'nothing')}")


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
    parser.add_argument("--lat", type=float); parser.add_argument("--lon", type=float)
    parser.add_argument("--allow-commands", action="store_true", help="Forward orbit, hold, relay and return tasks to the autopilot")
    parser.add_argument("--video-url", help="An http(s) MJPEG or snapshot URL for the drone's camera")
    args = parser.parse_args()
    token = args.token or token_from_env()
    if not token:
        sys.exit("Set TERRA_DRONE_TOKEN (the same value as on the Terra server) or pass --token.")
    terra = Terra(args.server, token)
    try:
        if args.virtual:
            run_virtual(terra, args.virtual, (args.lon, args.lat) if args.lat is not None and args.lon is not None else None, args.rate)
        else:
            run_mavlink(terra, args.mavlink, args.id, args.name, args.aircraft, args.allow_commands, args.video_url, args.rate)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
