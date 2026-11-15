"""Supervise the API and worker together so both use the same SQLite/artifact volume.

Temporal is a separate, private service backed by PostgreSQL. No development server,
frontend build or desktop tooling starts here. An optional lightweight virtual
fleet uses the same API and agents as connected ground stations.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
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
    kind = env.get('TERRA_FLEET', 'off')
    if kind not in ('off', 'virtual'):
        raise ValueError('The hosted runtime supports TERRA_FLEET=virtual or off. Run SITL on its ground station.')
    if kind == 'virtual':
        count = int(env.get('TERRA_VIRTUAL_FLEET', '5'))
        if not 1 <= count <= 5 or not env.get('TERRA_DRONE_TOKEN'):
            raise ValueError('A virtual fleet needs TERRA_DRONE_TOKEN and between one and five aircraft.')
        env['TERRA_VIRTUAL_FLEET'] = str(count)
        env['TERRA_SERVER'] = f'http://127.0.0.1:{port}'
    hosts = [v.strip() for v in env.get('TERRA_ALLOWED_HOSTS', '').split(',') if v.strip()]
    hosts.append('healthcheck.railway.app')
    if env.get('RAILWAY_PUBLIC_DOMAIN'):
        hosts.append(env['RAILWAY_PUBLIC_DOMAIN'])
    env['TERRA_ALLOWED_HOSTS'] = ','.join(dict.fromkeys(hosts))
    return env


def runtime_commands(env: dict[str, str]) -> dict[str, list[str]]:
    commands = {
        'api': [sys.executable, '-m', 'uvicorn', 'terra.app:app', '--host', '0.0.0.0', '--port', env['PORT'], '--timeout-graceful-shutdown', '20'],
        'worker': [sys.executable, '-m', 'terra.worker'],
    }
    if env.get('TERRA_FLEET') == 'virtual':
        bridge = Path(__file__).resolve().parents[2] / 'scripts' / 'drone_bridge.py'
        commands['fleet'] = [sys.executable, str(bridge), '--virtual', env['TERRA_VIRTUAL_FLEET'], '--rate', '2']
    return commands


def child_environment(name: str, env: dict[str, str]) -> dict[str, str]:
    if name != 'fleet':
        return env
    # The simulator calls the local API; it has no reason to receive provider keys.
    return {k: v for k, v in env.items() if k in ('PATH', 'PYTHONPATH', 'PYTHONUNBUFFERED', 'TERRA_SERVER', 'TERRA_DRONE_TOKEN')}


def main() -> int:
    try:
        env = runtime_environment(dict(os.environ))
    except ValueError as exc:
        log.error('%s', exc)
        return 1
    commands = runtime_commands(env)
    children: dict[str, subprocess.Popen] = {}
    stopping = False
    failures = {name: 0 for name in commands if name != 'api'}
    restart_at = {name: 0.0 for name in failures}
    started = {name: 0.0 for name in failures}

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
            for name in failures:
                child = children.get(name)
                if child is not None and child.poll() is not None:
                    failures[name] = 1 if time.monotonic() - started[name] > 60 else failures[name] + 1
                    if failures[name] >= 8:
                        log.error('%s repeatedly exited; restarting the container.', name)
                        return 1
                    restart_at[name] = time.monotonic() + min(30, 2 ** failures[name])
                    children.pop(name)
                    log.warning('%s exited; retrying after its dependency becomes available.', name)
                if name not in children and time.monotonic() >= restart_at[name]:
                    children[name] = subprocess.Popen(commands[name], env=child_environment(name, env), start_new_session=True)
                    started[name] = time.monotonic()
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
