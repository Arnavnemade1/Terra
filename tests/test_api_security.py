"""Same-origin gateway, static-file, artifact and error boundary tests."""
import importlib
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from terra.store import utc_now

api = importlib.import_module("terra.app")


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))
    with TestClient(api.app, base_url="http://localhost") as instance:
        yield instance


@pytest.mark.parametrize("path", ["/.env", "/.env.local", "/.data/terra.db", "/.git/config", "/api/.env"])
def test_workspace_files_are_never_served(client, path):
    response = client.get(path)
    assert response.status_code == 404


def test_host_header_cannot_rebind_local_api(client):
    response = client.get("/api/connector", headers={"host": "attacker.example"})
    assert response.status_code == 400


def test_foreign_site_cannot_start_investigation(client):
    response = client.post("/api/investigations", headers={"origin": "https://attacker.example"}, json={})
    assert response.status_code == 403


@pytest.mark.parametrize("path", [
    "/api/connector/maps/unknown/0/0/0", "/api/connector/maps/imagery/2/100/0",
    "/api/connector/maps/imagery/99/0/0", "/api/connector/gibs/https://attacker.example/tile",
    "/api/connector/terrain/private", "/api/connector/terrain/tilemap/3/0/0/9999/9999",
])
def test_gateway_has_no_arbitrary_proxy_or_unbounded_tile_address(client, path):
    assert client.get(path).status_code == 404


def test_client_cannot_supply_upstream_credentials(client):
    response = client.get("/api/connector/terrain?token=not-a-real-token")
    assert response.status_code == 422


def test_map_proxy_forwards_only_image_bytes_and_reuses_cache(client, monkeypatch):
    from terra import gateway
    gateway._cache.clear()
    gateway._cache_bytes = 0
    calls = []
    async def fake_request(provider, method, url, **kwargs):
        calls.append((provider, method, url))
        return httpx.Response(200, content=b"test-image", headers={"content-type": "image/jpeg", "set-cookie": "secret=canary"})
    monkeypatch.setattr(gateway, "request", fake_request)
    first = client.get("/api/connector/maps/imagery/1/0/0")
    second = client.get("/api/connector/maps/imagery/1/0/0")
    assert first.content == second.content == b"test-image"
    assert len(calls) == 1
    assert "set-cookie" not in first.headers
    assert second.headers["x-terra-cache"] == "hit"


def test_artifacts_never_expose_manifests_logs_or_symlinks(client, tmp_path):
    now = utc_now()
    api.get_store().save_investigation({"id": "security-test", "created_at": now, "updated_at": now})
    directory = tmp_path / "artifacts" / "security-test"
    directory.mkdir(parents=True)
    secret = tmp_path / ".env"
    secret.write_text("test-canary-secret")
    (directory / "leak.png").symlink_to(secret)
    for name in ("manifest.json", "compute.log", "completed.json", "leak.png"):
        assert client.get(f"/api/investigations/security-test/artifacts/{name}").status_code == 404


def test_followup_failure_does_not_return_upstream_error_details(client, monkeypatch):
    from terra import agent
    monkeypatch.setattr(agent, "available", lambda: True)
    async def current(_):
        return {"status": "running", "result": {"measurements": {"example": 1}}}
    async def failure(*_args):
        raise RuntimeError("test-canary-secret in provider response")
    monkeypatch.setattr(api, "current_investigation", current)
    monkeypatch.setattr(agent, "follow_up", failure)
    response = client.post("/api/investigations/test/ask", json={"question": "What is the result?"})
    assert response.status_code == 502
    assert "test-canary-secret" not in response.text


def test_profile_returns_frontend_points_contract(client, monkeypatch):
    from terra import conditions
    async def heights(points):
        return [10.0] + [None] * (len(points) - 1)
    monkeypatch.setattr(conditions, "elevations", heights)
    response = client.post("/api/profile", json={"vertices": [[0, 0], [0.01, 0]], "samples": 3})
    assert response.status_code == 200
    result = response.json()
    assert len(result["points"]) == 3
    assert set(result["points"][0]) == {"lon", "lat", "distance_km", "elevation_m", "radar"}
    assert result["points"][0]["elevation_m"] == 10
    assert result["points"][1]["elevation_m"] is None
    assert result["status"] == "partial"
    assert "Copernicus" in result["terrain_source"]


def test_profile_rejects_unbounded_request_before_provider_fetch(client):
    response = client.post("/api/profile", json={"vertices": [[0, 0], [10, 0]], "samples": 80})
    assert response.status_code == 422


def test_conditions_rejects_future_acquisitions(client):
    response = client.get("/api/conditions", params={"bbox": "-74.8,-5.55,-74.5,-5.25", "dates": "2099-01-01"})
    assert response.status_code == 422
