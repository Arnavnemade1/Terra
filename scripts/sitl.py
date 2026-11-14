#!/usr/bin/env python3
"""ArduPilot SITL for Terra: the real ArduCopter and ArduPlane flight firmware, flying simulated airframes.

  python scripts/sitl.py setup              # once: fetch and build ArduCopter + ArduPlane (gentle on the machine)
  python scripts/sitl.py fleet --count 5    # run a fleet at the monitoring area's launch site, one bridge each
  python scripts/sitl.py status

Each vehicle is a separate ArduPilot process (its own EEPROM, parameters and MAVLink port 5760 + 10 × instance);
each is connected to Terra by scripts/drone_bridge.py exactly as a real aircraft's ground station would be, so the
fleet coordinator, missions, acknowledgements and telemetry are exercised end to end with real autopilot logic.

The build runs at low priority (nice 15) with four compiler jobs (TERRA_SITL_JOBS), and is paused automatically whenever free
memory falls below 1.5 GB, so it never starves the rest of the computer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.getenv("TERRA_SITL_HOME", Path.home() / ".terra" / "sitl"))
SRC = HOME / "ardupilot"
VENV = HOME / "venv"
BIN = SRC / "build" / "sitl" / "bin"
RUN = HOME / "run"
JOBS = int(os.getenv("TERRA_SITL_JOBS", "4"))  # four compiler jobs; the memory guard below is the hard limit
MIN_FREE_MB = 1536

# The fleet: one fixed-wing mapper and four multirotors, as in the virtual fleet.
FLEET = [
    {"id": "D-01", "name": "Kestrel", "aircraft": "fixed-wing", "sensors": "eo,multispectral", "relay": False, "cruise": 22, "endurance": 95},
    {"id": "D-02", "name": "Harrier", "aircraft": "quad", "sensors": "eo,ir", "relay": False, "cruise": 10, "endurance": 30},
    {"id": "D-03", "name": "Swift", "aircraft": "quad", "sensors": "eo,ir", "relay": False, "cruise": 11, "endurance": 30},
    {"id": "D-04", "name": "Osprey", "aircraft": "quad", "sensors": "eo,lidar", "relay": False, "cruise": 10, "endurance": 32},
    {"id": "D-05", "name": "Wren", "aircraft": "quad", "sensors": "eo", "relay": True, "cruise": 8, "endurance": 35},
]
COPTER_PARAMS = """# Terra overrides for simulated multirotors
WPNAV_SPEED 1000
WPNAV_SPEED_UP 300
WPNAV_SPEED_DN 200
WPNAV_ACCEL 300
LOIT_SPEED 1000
RTL_ALT 6000
RTL_SPEED 1200
AUTO_OPTIONS 3
ARMING_CHECK 0
FENCE_ENABLE 0
BATT_CAPACITY 5000
SIM_BATT_CAP_AH 5
"""
PLANE_PARAMS = """# Terra overrides for the simulated fixed-wing
AIRSPEED_CRUISE 22
TRIM_ARSPD_CM 2200
WP_LOITER_RAD 160
RTL_RADIUS 160
RTL_AUTOLAND 0
TKOFF_ALT 60
ARMING_CHECK 0
FENCE_ENABLE 0
"""


# ---------------------------------------------------------------- resource guard
def free_mb() -> float | None:
    """Memory that can be used without swapping (free + inactive + speculative pages on macOS; MemAvailable on Linux)."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", out).group(1))
            pages = sum(int(re.search(rf"{k}:\s+(\d+)", out).group(1)) for k in ("Pages free", "Pages inactive", "Pages speculative"))
            return pages * page / 1e6
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except Exception:
        return None
    return None


def guarded(cmd: list[str], cwd: Path, env: dict, log) -> int:
    """Run a build step at low priority; pause it (SIGSTOP) while free memory is low, resume when it recovers."""
    proc = subprocess.Popen(["nice", "-n", "15", *cmd], cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    paused = False

    def watch() -> None:
        nonlocal paused
        while proc.poll() is None:
            mb = free_mb()
            if mb is not None and not paused and mb < MIN_FREE_MB:
                os.killpg(proc.pid, signal.SIGSTOP); paused = True
                print(f"  paused the build: only {mb:.0f} MB free", flush=True)
            elif mb is not None and paused and mb > MIN_FREE_MB * 1.5:
                os.killpg(proc.pid, signal.SIGCONT); paused = False
                print(f"  resumed the build ({mb:.0f} MB free)", flush=True)
            time.sleep(2)

    threading.Thread(target=watch, daemon=True).start()
    try:
        return proc.wait()
    except KeyboardInterrupt:
        os.killpg(proc.pid, signal.SIGTERM); raise


def setup(args) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    log_path = HOME / "build.log"
    with open(log_path, "a") as log:
        if not (SRC / ".git").exists():
            print("Cloning ArduPilot (shallow, with submodules)…", flush=True)
            subprocess.run(["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules", "-j", "4",
                            "https://github.com/ArduPilot/ardupilot.git", str(SRC)], check=True, stdout=log, stderr=subprocess.STDOUT)
        if not (VENV / "bin" / "python").exists():
            print("Creating the build tools environment…", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        print("Installing build tools (empy, future, pexpect, dronecan, pymavlink)…", flush=True)
        subprocess.run([str(VENV / "bin" / "pip"), "install", "-q", "empy==3.3.4", "future", "pexpect", "dronecan", "pymavlink", "lxml", "setuptools"],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
        env = {**os.environ, "PATH": f"{VENV / 'bin'}:{os.environ['PATH']}"}
        py = str(VENV / "bin" / "python")
        print(f"Configuring for SITL (log: {log_path})…", flush=True)
        if guarded([py, "./waf", "configure", "--board", "sitl"], SRC, env, log) != 0:
            sys.exit(f"Configure failed; see {log_path}")
        for target in args.targets:
            print(f"Building {target} with {JOBS} jobs at low priority…", flush=True)
            started = time.time()
            if guarded([py, "./waf", target, "-j", str(JOBS)], SRC, env, log) != 0:
                sys.exit(f"Building {target} failed; see {log_path}")
            print(f"  {target} built in {time.time() - started:.0f} s", flush=True)
    print(f"Done: {', '.join(sorted(p.name for p in BIN.glob('ardu*')))} in {BIN}")


# ---------------------------------------------------------------- fleet
def terra(path: str, token: str, body: dict | None = None) -> dict:
    server = os.getenv("TERRA_SERVER", f"http://127.0.0.1:{os.getenv('TERRA_API_PORT', '8010')}")
    request = urllib.request.Request(server + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def launch_site(token: str, wait: bool) -> tuple[float, float, float]:
    while True:
        try:
            area = terra("/api/fleet/area", token).get("area")
        except Exception as exc:
            area = None; print(f"Terra is not reachable yet ({exc.__class__.__name__}); retrying…", flush=True)
        if area:
            lon, lat = area["launch"]
            try:  # ground height at the launch site, from Terra's elevation service
                profile = terra("/api/profile", token, {"vertices": [[lon, lat], [lon + 0.0002, lat]], "samples": 2})
                alt = float(profile["points"][0]["elevation_m"] or 0)
            except Exception:
                alt = 0.0
            return lon, lat, alt
        if not wait:
            sys.exit("No monitoring area yet: open the workspace (Drones) and choose one, or pass --lat/--lon.")
        print("Waiting for a monitoring area to be set in the workspace…", flush=True); time.sleep(5)


def fleet(args) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from drone_bridge import token_from_env
    token = args.token or token_from_env()
    if not token:
        sys.exit("Set TERRA_DRONE_TOKEN (the same value as on the Terra server).")
    missing = [b for b in ("arducopter", "arduplane") if not (BIN / b).exists()]
    if missing:
        sys.exit(f"Not built yet: {', '.join(missing)}. Run: python scripts/sitl.py setup")
    lon, lat, alt = (args.lon, args.lat, args.alt) if args.lat is not None else launch_site(token, wait=True)
    RUN.mkdir(parents=True, exist_ok=True)
    procs: list[subprocess.Popen] = []

    def stop(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()
        sys.exit(0)
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    print(f"Launching {args.count} ArduPilot SITL vehicles at {lat:.5f}, {lon:.5f}, {alt:.0f} m", flush=True)
    for i, spec in enumerate(FLEET[:args.count]):
        plane = spec["aircraft"] == "fixed-wing"
        workdir = RUN / spec["id"]; workdir.mkdir(exist_ok=True)
        (workdir / "terra.parm").write_text(PLANE_PARAMS if plane else COPTER_PARAMS)
        defaults = f"{SRC / 'Tools/autotest/default_params' / ('plane.parm' if plane else 'copter.parm')},{workdir / 'terra.parm'}"
        # Spread the vehicles 15 m apart on the pad, facing north.
        hlat, hlon = lat, lon + (i - 2) * 15 / (111_320 * max(0.2, __import__('math').cos(__import__('math').radians(lat))))
        sim = [str(BIN / ("arduplane" if plane else "arducopter")), "--model", "plane" if plane else "quad", "--home", f"{hlat:.7f},{hlon:.7f},{alt:.1f},0",
               "--instance", str(i), "--defaults", defaults, "-w", "--speedup", "1"]
        procs.append(subprocess.Popen(sim, cwd=workdir, stdout=open(workdir / "sitl.log", "w"), stderr=subprocess.STDOUT))
        time.sleep(1.5)
        bridge = [sys.executable, str(ROOT / "scripts" / "drone_bridge.py"), "--mavlink", f"tcp:127.0.0.1:{5760 + 10 * i}", "--id", spec["id"], "--name", spec["name"],
                  "--aircraft", spec["aircraft"], "--origin", "sitl", "--sensors", spec["sensors"], "--cruise", str(spec["cruise"]), "--endurance", str(spec["endurance"]),
                  "--allow-commands", "--allow-arming", "--allow-manual"] + (["--relay"] if spec["relay"] else [])
        procs.append(subprocess.Popen(bridge, cwd=ROOT, env={**os.environ, "TERRA_DRONE_TOKEN": token}))
    print("Fleet running. Ctrl-C stops every vehicle and bridge.", flush=True)
    while True:
        time.sleep(2)
        for p in procs:
            if p.poll() is not None:
                print(f"A process exited ({' '.join(p.args[:2])} code {p.returncode}); stopping the fleet.", flush=True)
                stop()


def status(_args) -> None:
    built = sorted(p.name for p in BIN.glob("ardu*")) if BIN.exists() else []
    print(json.dumps({"home": str(HOME), "source": (SRC / ".git").exists(), "built": built, "free_mb": round(free_mb() or 0)}, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--targets", nargs="+", default=["copter", "plane"])
    f = sub.add_parser("fleet"); f.add_argument("--count", type=int, default=5); f.add_argument("--token")
    f.add_argument("--lat", type=float); f.add_argument("--lon", type=float); f.add_argument("--alt", type=float, default=0.0)
    sub.add_parser("status")
    args = parser.parse_args()
    {"setup": setup, "fleet": fleet, "status": status}[args.cmd](args)


if __name__ == "__main__":
    main()
