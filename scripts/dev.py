"""Start the whole TERRA stack for local development with one command.

Order: Temporal dev server -> scientific worker -> FastAPI -> Vite.
Uses the `temporal` CLI when it is installed; otherwise the Temporal Python SDK
downloads and runs the same dev server binary (scripts/temporal_dev.py).
Ctrl+C stops every process.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data"
VENV_PY = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY if VENV_PY.exists() else Path(sys.executable))
TEMPORAL_PORT = int(os.getenv("TERRA_TEMPORAL_PORT", "7233"))
API_PORT_PREFERRED = int(os.getenv("TERRA_API_PORT", "8000"))

COLORS = {"temporal": "35", "worker": "33", "api": "36", "web": "32", "fleet": "34"}
procs: list[tuple[str, subprocess.Popen]] = []
commands: dict[str, list[str]] = {}
restarts: dict[str, list[float]] = {}
stopping = False


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    env.setdefault("TEMPORAL_ADDRESS", f"127.0.0.1:{TEMPORAL_PORT}")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "backend"), env.get("PYTHONPATH", "")]))
    env["PYTHONUNBUFFERED"] = "1"
    return env


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def fail(message: str) -> None:
    print(f"[dev] {message}")
    shutdown(code=1)


def free_port(preferred: int) -> int:
    for port in [preferred, *range(8010, 8100)]:
        if not port_open(port):
            return port
    fail("No free port for the API between 8010 and 8099.")
    return preferred


def wait_for(port: int, name: str, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return
        for proc_name, proc in procs:
            if proc.poll() is not None:
                fail(f"{proc_name} exited with code {proc.returncode} while waiting for {name}.")
        time.sleep(0.3)
    fail(f"{name} did not open port {port} within {timeout:.0f}s.")


def start(name: str, cmd: list[str], env: dict[str, str]) -> None:
    commands[name] = cmd
    color = COLORS.get(name, "0")
    prefix = f"\033[{color}m[{name:>8}]\033[0m "
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True)
    procs[:] = [(n, p) for n, p in procs if n != name]
    procs.append((name, proc))

    import threading

    def pump() -> None:
        assert proc.stdout
        for line in proc.stdout:
            sys.stdout.write(prefix + line)
            sys.stdout.flush()

    threading.Thread(target=pump, daemon=True).start()


def shutdown(*_: object, code: int = 0) -> None:
    global stopping
    stopping = True
    for _, proc in reversed(procs):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.time() + 8
    for _, proc in procs:
        try:
            proc.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    sys.exit(code)


def main() -> None:
    DATA.mkdir(exist_ok=True)
    env = load_env()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if port_open(TEMPORAL_PORT):
        print(f"[dev] Temporal already listening on {TEMPORAL_PORT}; reusing it.")
    elif shutil.which("temporal"):
        start("temporal", ["temporal", "server", "start-dev", "--port", str(TEMPORAL_PORT), "--ui-port", "8233",
                           "--db-filename", str(DATA / "temporal.db"), "--log-level", "warn"], env)
    else:
        start("temporal", [PYTHON, str(ROOT / "scripts" / "temporal_dev.py")], env)
    wait_for(TEMPORAL_PORT, "Temporal", timeout=180)

    start("worker", [PYTHON, "-m", "terra.worker"], env)
    api_port = free_port(API_PORT_PREFERRED)
    if api_port != API_PORT_PREFERRED:
        print(f"[dev] Port {API_PORT_PREFERRED} is busy; serving the API on {api_port}.")
    env["TERRA_API_PORT"] = str(api_port)
    start("api", [PYTHON, "-m", "uvicorn", "terra.app:app", "--host", "127.0.0.1", "--port", str(api_port),
                  "--reload", "--reload-dir", str(ROOT / "backend")], env)
    wait_for(api_port, "API")
    start("web", ["npx", "vite", "--host", "127.0.0.1"], env)
    # The development fleet connects through the same protocol and database as real drones. ArduPilot SITL (the real
    # flight firmware on simulated airframes) when it is built (python scripts/sitl.py setup); otherwise the lightweight
    # virtual fleet. TERRA_FLEET=sitl|virtual|off chooses; TERRA_VIRTUAL_FLEET sets how many aircraft (default 5).
    count = env.get("TERRA_VIRTUAL_FLEET", "5")
    sitl_bin = Path(env.get("TERRA_SITL_HOME", str(Path.home() / ".terra" / "sitl"))) / "ardupilot" / "build" / "sitl" / "bin"
    kind = env.get("TERRA_FLEET") or ("sitl" if (sitl_bin / "arducopter").exists() and (sitl_bin / "arduplane").exists() else "virtual")
    if not env.get("TERRA_DRONE_TOKEN"):
        print("[dev] TERRA_DRONE_TOKEN is not set: drone connections and the development fleet are off.")
    elif kind == "sitl" and count not in ("", "0"):
        start("fleet", [PYTHON, str(ROOT / "scripts" / "sitl.py"), "fleet", "--count", count], env)
    elif kind == "virtual" and count not in ("", "0"):
        start("fleet", [PYTHON, str(ROOT / "scripts" / "drone_bridge.py"), "--virtual", count], env)
    print(f"\n[dev] TERRA → http://127.0.0.1:5173   API docs → http://127.0.0.1:{api_port}/api/docs   Temporal UI → http://127.0.0.1:8233\n")

    # Supervise: restart a process that dies unexpectedly; give up only if it keeps crashing.
    while not stopping:
        for name, proc in list(procs):
            if proc.poll() is None or stopping:
                continue
            recent = [t for t in restarts.get(name, []) if time.time() - t < 120]
            if len(recent) >= 5:
                print(f"[dev] {name} keeps exiting (code {proc.returncode}); stopping the stack.")
                shutdown(code=1)
            restarts[name] = recent + [time.time()]
            print(f"[dev] {name} exited with code {proc.returncode}; restarting it.")
            time.sleep(1.5)
            start(name, commands[name], env)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
