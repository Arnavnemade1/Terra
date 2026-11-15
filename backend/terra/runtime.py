"""Supervise the API and worker together so both use the same SQLite/artifact volume.

Temporal is a separate, private service backed by PostgreSQL. No development server,
frontend build, simulated aircraft or desktop tooling starts in this runtime.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from uuid import uuid4

log = logging.getLogger(__name__)


def runtime_environment(source: dict[str, str]) -> dict[str, str]:
    env = source.copy()
    if not env.get('TEMPORAL_ADDRESS'):
        raise ValueError('Set TEMPORAL_ADDRESS to the private Temporal service before starting Terra.')
    port = int(env.get('PORT', '8000'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535.')
    env['PORT'] = str(port)
    env['TERRA_RUNTIME_ID'] = uuid4().hex
    env.setdefault('TERRA_DATA_DIR', '/data')
    env.setdefault('PYTHONUNBUFFERED', '1')
    env.setdefault('TERRA_MAX_ACTIVITIES', '3')
    env.setdefault('TERRA_MAX_WORKFLOW_TASKS', '4')
    env.setdefault('TERRA_TILE_WARM', '0')
    hosts = [v.strip() for v in env.get('TERRA_ALLOWED_HOSTS', '').split(',') if v.strip()]
    hosts.append('healthcheck.railway.app')
    if env.get('RAILWAY_PUBLIC_DOMAIN'):
        hosts.append(env['RAILWAY_PUBLIC_DOMAIN'])
    env['TERRA_ALLOWED_HOSTS'] = ','.join(dict.fromkeys(hosts))
    return env


def main() -> int:
    try:
        env = runtime_environment(dict(os.environ))
    except ValueError as exc:
        log.error('%s', exc)
        return 1
    commands = {
        'api': [sys.executable, '-m', 'uvicorn', 'terra.app:app', '--host', '0.0.0.0', '--port', env['PORT'], '--timeout-graceful-shutdown', '20'],
        'worker': [sys.executable, '-m', 'terra.worker'],
    }
    children: dict[str, subprocess.Popen] = {}
    stopping = False
    failures = 0
    restart_at = 0.0
    worker_started = 0.0

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children['api'] = subprocess.Popen(commands['api'], env=env, start_new_session=True)
        while not stopping:
            if children['api'].poll() is not None:
                log.error('API exited; restarting the container.')
                return children['api'].returncode or 1
            worker = children.get('worker')
            if worker is not None and worker.poll() is not None:
                failures = 1 if time.monotonic() - worker_started > 60 else failures + 1
                if failures >= 8:
                    log.error('Worker repeatedly exited; restarting the container.')
                    return 1
                restart_at = time.monotonic() + min(30, 2 ** failures)
                children.pop('worker')
                log.warning('Worker exited; retrying after its dependency becomes available.')
            if 'worker' not in children and time.monotonic() >= restart_at:
                children['worker'] = subprocess.Popen(commands['worker'], env=env, start_new_session=True)
                worker_started = time.monotonic()
            time.sleep(0.5)
    finally:
        for child in children.values():
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 25
        for child in children.values():
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
