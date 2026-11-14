#!/usr/bin/env python3
"""ArduPilot SITL for Terra: the real ArduCopter and ArduPlane flight firmware, flying simulated airframes.

  python scripts/sitl.py setup              # once: fetch and build ArduCopter + ArduPlane (gentle on the machine)
  python scripts/sitl.py fleet --count 5    # run a fleet at the monitoring area's launch site, one bridge each
  python scripts/sitl.py add --id D-06 --name Merlin --sensors eo,ir   # one more aircraft (the workspace's Add a drone)
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
import math
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
# Physics steps per second. ArduPilot's default 1200 Hz costs about 16 % of a core per vehicle; 400 Hz matches the
# copter's 400 Hz main loop (and is far above the plane's 50 Hz) and costs roughly a third of that.
SIM_RATE_HZ = int(os.getenv("TERRA_SITL_RATE", "400"))

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
BATT_CAPACITY 12000
SIM_BATT_CAP_AH 12
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


def _prepare(args) -> tuple[str, tuple[float, float, float]]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from drone_bridge import token_from_env
    token = args.token or token_from_env()
    if not token:
        sys.exit("Set TERRA_DRONE_TOKEN (the server's token, or an aircraft token from the workspace's Add a drone).")
    missing = [b for b in ("arducopter", "arduplane") if not (BIN / b).exists()]
    if missing:
        sys.exit(f"Not built yet: {', '.join(missing)}. Run: python scripts/sitl.py setup")
    RUN.mkdir(parents=True, exist_ok=True)
    return token, ((args.lon, args.lat, args.alt) if args.lat is not None else launch_site(token, wait=True))


def _port_free(port: int) -> bool:
    import socket
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def launch(spec: dict, instance: int, slot: int, site: tuple[float, float, float], token: str, procs: list[subprocess.Popen]) -> None:
    """One ArduPilot process on its own MAVLink port, and the bridge that connects it to Terra."""
    lon, lat, alt = site; plane = spec["aircraft"] == "fixed-wing"
    workdir = RUN / spec["id"]; workdir.mkdir(exist_ok=True)
    (workdir / "terra.parm").write_text(PLANE_PARAMS if plane else COPTER_PARAMS)
    # The airframe's stock parameters (their location differs between ArduPilot versions; ArduPlane also carries its
    # model parameters built in), then Terra's.
    stock = [SRC / "Tools/autotest/default_params" / ("plane.parm" if plane else "copter.parm"), SRC / "Tools/autotest/models" / ("plane.parm" if plane else "copter.parm")]
    defaults = ",".join([str(p) for p in stock if p.exists()][:1] + [str(workdir / "terra.parm")])
    # Spread the vehicles 15 m apart on the pad, facing north.
    hlon = lon + slot * 15 / (111_320 * max(0.2, math.cos(math.radians(lat))))
    sim = [str(BIN / ("arduplane" if plane else "arducopter")), "--model", "plane" if plane else "quad", "--home", f"{lat:.7f},{hlon:.7f},{alt:.1f},0",
           "--instance", str(instance), "--defaults", defaults, "-w", "--speedup", "1", "--rate", str(SIM_RATE_HZ)]
    procs.append(subprocess.Popen(sim, cwd=workdir, stdout=open(workdir / "sitl.log", "w"), stderr=subprocess.STDOUT))
    time.sleep(1.5)
    bridge = [sys.executable, str(ROOT / "scripts" / "drone_bridge.py"), "--mavlink", f"tcp:127.0.0.1:{5760 + 10 * instance}", "--id", spec["id"], "--name", spec["name"],
              "--aircraft", spec["aircraft"], "--origin", "sitl", "--sensors", spec["sensors"], "--cruise", str(spec["cruise"]), "--endurance", str(spec["endurance"]),
              "--allow-commands", "--allow-arming", "--allow-manual"] + (["--relay"] if spec["relay"] else [])
    procs.append(subprocess.Popen(bridge, cwd=ROOT, env={**os.environ, "TERRA_DRONE_TOKEN": token}))


def _km(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return math.hypot((a[0] - b[0]) * 111.32 * math.cos(math.radians(a[1])), (a[1] - b[1]) * 110.574)


def _halt(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def supervise(procs: list[subprocess.Popen], token: str, site: tuple[float, float, float], start, follow: bool) -> None:
    """Keep the vehicles and bridges running. With `follow`, when the workspace moves the monitoring area more than
    40 km (beyond where the coordinator tasks aircraft), the vehicles are relaunched at the new launch site, as a
    ground crew would drive the fleet there."""
    def stop(*_):
        _halt(procs); sys.exit(0)
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    print("Running. Ctrl-C stops every vehicle and bridge started here.", flush=True)
    checked = time.monotonic()
    while True:
        time.sleep(2)
        if follow and time.monotonic() - checked > 10:
            checked = time.monotonic()
            try:
                area = terra("/api/fleet/area", token).get("area")
            except Exception:
                area = None
            if area and _km(site, tuple(area["launch"])) > 40:
                print(f"The monitoring area moved to {area['name']} ({_km(site, tuple(area['launch'])):.0f} km away); relaunching the vehicles there.", flush=True)
                _halt(procs); procs.clear()
                site = launch_site(token, wait=True); start(site)
                continue
        for p in procs:
            if p.poll() is not None:
                print(f"A process exited ({' '.join(p.args[:2])} code {p.returncode}); stopping.", flush=True)
                stop()


def fleet(args) -> None:
    token, site = _prepare(args)
    procs: list[subprocess.Popen] = []

    def start(at: tuple[float, float, float]) -> None:
        print(f"Launching {args.count} ArduPilot SITL vehicles at {at[1]:.5f}, {at[0]:.5f}, {at[2]:.0f} m", flush=True)
        for i, spec in enumerate(FLEET[:args.count]):
            launch(spec, i, i - 2, at, token, procs)
    start(site)
    supervise(procs, token, site, start, follow=args.lat is None)


def add(args) -> None:
    """One more SITL aircraft, on the first free instance from 10 upwards (the fleet uses 0–4)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.id):
        sys.exit("Use an id of letters, digits, - and _ (up to 32).")
    token, site = _prepare(args)
    instance = next(i for i in range(10, 60) if _port_free(5760 + 10 * i))
    spec = {"id": args.id, "name": args.name or args.id, "aircraft": args.aircraft, "sensors": args.sensors, "relay": args.relay,
            "cruise": 22 if args.aircraft == "fixed-wing" else 10, "endurance": 90 if args.aircraft == "fixed-wing" else 30}
    procs: list[subprocess.Popen] = []

    def start(at: tuple[float, float, float]) -> None:
        print(f"Launching {spec['id']} ({spec['aircraft']}, ArduPilot SITL instance {instance}) at {at[1]:.5f}, {at[0]:.5f}", flush=True)
        launch(spec, instance, 3 + instance % 6, at, token, procs)
    start(site)
    supervise(procs, token, site, start, follow=args.lat is None)


def status(_args) -> None:
    built = sorted(p.name for p in BIN.glob("ardu*")) if BIN.exists() else []
    print(json.dumps({"home": str(HOME), "source": (SRC / ".git").exists(), "built": built, "free_mb": round(free_mb() or 0)}, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--targets", nargs="+", default=["copter", "plane"])
    f = sub.add_parser("fleet"); f.add_argument("--count", type=int, default=5); f.add_argument("--token")
    f.add_argument("--lat", type=float); f.add_argument("--lon", type=float); f.add_argument("--alt", type=float, default=0.0)
    a = sub.add_parser("add"); a.add_argument("--id", required=True); a.add_argument("--name"); a.add_argument("--token")
    a.add_argument("--aircraft", default="quad", choices=["quad", "fixed-wing"]); a.add_argument("--sensors", default="eo,ir"); a.add_argument("--relay", action="store_true")
    a.add_argument("--lat", type=float); a.add_argument("--lon", type=float); a.add_argument("--alt", type=float, default=0.0)
    sub.add_parser("status")
    args = parser.parse_args()
    {"setup": setup, "fleet": fleet, "add": add, "status": status}[args.cmd](args)


if __name__ == "__main__":
    main()
