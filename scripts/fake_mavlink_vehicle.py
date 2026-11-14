#!/usr/bin/env python3
"""A minimal MAVLink vehicle for testing the ground-station bridge without hardware or a full SITL.

It identifies as a PX4 multirotor, streams HEARTBEAT, GLOBAL_POSITION_INT, SYS_STATUS and EXTENDED_SYS_STATE,
accepts MAV_CMD_DO_ORBIT, MAV_CMD_DO_REPOSITION and MAV_CMD_NAV_RETURN_TO_LAUNCH with COMMAND_ACK, and flies toward
the commanded point. It is a protocol test double, not a flight model; use PX4 or ArduPilot SITL for that.

  python scripts/fake_mavlink_vehicle.py --lat 37.745 --lon 15.0       # sends to udp 127.0.0.1:14550
  python scripts/drone_bridge.py --mavlink udpin:0.0.0.0:14550 --id mav-1 --name "Test vehicle" --allow-commands
"""
import argparse
import math
import time

from pymavlink import mavutil


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", default="udpout:127.0.0.1:14550")
    parser.add_argument("--lat", type=float, default=37.745); parser.add_argument("--lon", type=float, default=15.0)
    parser.add_argument("--seconds", type=float, default=0, help="Stop after this long (0: run until interrupted)")
    args = parser.parse_args()
    link = mavutil.mavlink_connection(args.to, source_system=1, source_component=1)
    m = mavutil.mavlink
    lat, lon, rel, battery, heading = args.lat, args.lon, 80.0, 87.0, 0.0
    target = (lat, lon, rel); radius = 0.0; started = last = time.monotonic(); beat = 0.0
    while not args.seconds or time.monotonic() - started < args.seconds:
        now = time.monotonic(); dt = now - last; last = now
        # Fly toward the target (or around it when orbiting) at 10 m/s.
        tlat, tlon, talt = target
        dn = (tlat - lat) * 111_000; de = (tlon - lon) * 111_000 * math.cos(math.radians(lat)); d = math.hypot(dn, de)
        if radius and abs(d - radius) < 30:
            heading = (math.degrees(math.atan2(-de, -dn)) + 90) % 360  # tangent, clockwise
        elif d > 2:
            heading = math.degrees(math.atan2(de, dn)) % 360
        step = min(10 * dt, max(0.0, d - radius) if not radius or abs(d - radius) >= 30 else 10 * dt)
        lat += math.cos(math.radians(heading)) * step / 111_000; lon += math.sin(math.radians(heading)) * step / (111_000 * math.cos(math.radians(lat)))
        rel += max(-3 * dt, min(3 * dt, talt - rel)); battery = max(0.0, battery - dt * 0.03)
        if now - beat > 1:
            beat = now
            link.mav.heartbeat_send(m.MAV_TYPE_QUADROTOR, m.MAV_AUTOPILOT_PX4, m.MAV_MODE_FLAG_SAFETY_ARMED | m.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, 0, m.MAV_STATE_ACTIVE)
            link.mav.sys_status_send(0, 0, 0, 500, 15800, -1, int(battery), 0, 0, 0, 0, 0, 0)
            link.mav.extended_sys_state_send(m.MAV_VTOL_STATE_UNDEFINED, m.MAV_LANDED_STATE_IN_AIR)
        link.mav.global_position_int_send(int(now * 1000) & 0xFFFFFFFF, int(lat * 1e7), int(lon * 1e7), int((rel + 1500) * 1000), int(rel * 1000),
                                          int(math.cos(math.radians(heading)) * 1000), int(math.sin(math.radians(heading)) * 1000), 0, int(heading * 100))
        msg = link.recv_match(type=["COMMAND_INT", "COMMAND_LONG"], blocking=False)
        if msg:
            if msg.command in (m.MAV_CMD_DO_ORBIT, m.MAV_CMD_DO_REPOSITION):
                target = (msg.x / 1e7, msg.y / 1e7, msg.z); radius = abs(msg.param1) if msg.command == m.MAV_CMD_DO_ORBIT else 0.0
                result = m.MAV_RESULT_ACCEPTED
            elif msg.command == m.MAV_CMD_NAV_RETURN_TO_LAUNCH:
                target = (args.lat, args.lon, 30.0); radius = 0.0; result = m.MAV_RESULT_ACCEPTED
            else:
                result = m.MAV_RESULT_UNSUPPORTED
            link.mav.command_ack_send(msg.command, result)
            print(f"command {msg.command} → {'accepted' if result == m.MAV_RESULT_ACCEPTED else 'unsupported'}", flush=True)
        time.sleep(0.1)


if __name__ == "__main__":
    main()
