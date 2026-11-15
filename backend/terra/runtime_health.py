"""Readiness shared by the API and worker on their persistent volume."""
import json
import os
from pathlib import Path
import time

HEARTBEAT = '.worker-heartbeat.json'
MAX_AGE_SECONDS = 20


def write_heartbeat(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f'{HEARTBEAT}.{os.getpid()}.tmp'
    temporary.write_text(json.dumps({'at': time.time(), 'runtime_id': os.getenv('TERRA_RUNTIME_ID', '')}))
    temporary.replace(directory / HEARTBEAT)


def worker_ready(directory: Path) -> bool:
    try:
        data = json.loads((directory / HEARTBEAT).read_text())
        age = time.time() - float(data['at'])
        return 0 <= age <= MAX_AGE_SECONDS and data['runtime_id'] == os.getenv('TERRA_RUNTIME_ID', '')
    except (OSError, ValueError, TypeError, KeyError):
        return False
