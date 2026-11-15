import asyncio
import importlib
import json
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from terra.runtime import runtime_environment
from terra.runtime_health import HEARTBEAT, worker_ready, write_heartbeat

api = importlib.import_module('terra.app')


def test_runtime_requires_temporal_and_assigns_a_unique_boot_identity():
    with pytest.raises(ValueError, match='TEMPORAL_ADDRESS'):
        runtime_environment({})
    source = {'TEMPORAL_ADDRESS': 'temporal.internal:7233', 'PORT': '8080', 'RAILWAY_PUBLIC_DOMAIN': 'terra.example'}
    first, second = runtime_environment(source), runtime_environment(source)
    assert first['TERRA_RUNTIME_ID'] != second['TERRA_RUNTIME_ID']
    assert 'TERRA_RUNTIME_ID' not in source
    assert 'healthcheck.railway.app' in first['TERRA_ALLOWED_HOSTS'].split(',')
    assert 'terra.example' in first['TERRA_ALLOWED_HOSTS'].split(',')
    assert first['TERRA_MAX_ACTIVITIES'] == '3'


def test_worker_heartbeat_expires_and_cannot_survive_a_different_container_boot(monkeypatch, tmp_path):
    monkeypatch.setenv('TERRA_RUNTIME_ID', 'first-boot')
    assert not worker_ready(tmp_path)
    write_heartbeat(tmp_path)
    assert worker_ready(tmp_path)
    monkeypatch.setenv('TERRA_RUNTIME_ID', 'second-boot')
    assert not worker_ready(tmp_path)
    write_heartbeat(tmp_path)
    path = tmp_path / HEARTBEAT
    old = json.loads(path.read_text()); old['at'] = time.time() - 60; path.write_text(json.dumps(old))
    assert not worker_ready(tmp_path)
    path.write_text('unfinished data')
    assert not worker_ready(tmp_path)


def test_deployment_is_not_ready_until_both_worker_and_temporal_are_healthy(monkeypatch, tmp_path):
    monkeypatch.setenv('TERRA_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TERRA_TILE_WARM', '0')
    monkeypatch.setenv('TERRA_RUNTIME_ID', 'test-deploy')
    async def system_info(_):
        return object()
    async def connected():
        return SimpleNamespace(workflow_service=SimpleNamespace(get_system_info=system_info))
    monkeypatch.setattr(api, 'temporal_client', connected)
    with TestClient(api.app, base_url='http://localhost') as client:
        assert client.get('/api/ready').status_code == 503
        write_heartbeat(tmp_path)
        response = client.get('/api/ready')
        assert response.status_code == 200 and response.json()['worker']
        async def disconnected():
            raise ConnectionError('private connection details')
        monkeypatch.setattr(api, 'temporal_client', disconnected)
        response = client.get('/api/ready')
        assert response.status_code == 503
        assert 'private connection details' not in response.text
