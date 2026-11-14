"""Google Map Tiles gateway: key handling, URI rewriting, path policy and cache rules.

Upstream responses mirror the real API shapes probed on 2026-11-14: root.json is
`application/json` with absolute child URIs (`/v1/3dtiles/datasets/<id>/files/<f>.json|glb?session=<s>`)
and `Cache-Control: private, max-age=0, must-revalidate`; tiles are `model/gltf-binary`
with `private, max-age=14400`. Credentials are invented canaries.
"""
import importlib
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from terra import connector, google_tiles

api = importlib.import_module("terra.app")
CANARY = "test-google-key-canary-not-real"
ROOT = {"asset": {"version": "1.0"}, "geometricError": 1e100, "root": {"children": [
    {"content": {"uri": "/v1/3dtiles/datasets/CgIYAQ/files/AJVsH2abc.json?session=CJjs2NODzvfd"}},
    {"content": {"uri": "/v1/3dtiles/datasets/CgIYAQ/files/AJVsH2def.glb?session=CJjs2NODzvfd"}}]}}


@pytest.fixture
def upstream(monkeypatch, tmp_path):
    """Route connector traffic to a fake tile.googleapis.com and record every request."""
    seen: list[httpx.Request] = []
    behaviour = {"status": 200}

    def handler(request: httpx.Request):
        seen.append(request)
        if behaviour["status"] != 200:
            return httpx.Response(behaviour["status"], json={"error": {"message": f"key {CANARY} denied"}})
        if request.headers.get("if-none-match") == "CO_H4tcG":
            return httpx.Response(304, headers={"etag": "CO_H4tcG", "cache-control": "private, max-age=0, must-revalidate"})
        if request.url.path.endswith(".glb"):
            return httpx.Response(200, content=b"glTF\x02\x00\x00\x00", headers={"content-type": "model/gltf-binary",
                                  "cache-control": "private, max-age=14400, must-revalidate, no-transform", "etag": "CO_H4tcG"})
        return httpx.Response(200, json=ROOT, headers={"cache-control": "private, max-age=0, must-revalidate, no-transform", "etag": "CO_H4tcG"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(connector, "_async_client", lambda: client)
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_MAPS_TILES_KEY", CANARY)
    google_tiles._status.update(checked=0.0, value=None)
    return seen, behaviour


@pytest.fixture
def client():
    with TestClient(api.app, base_url="http://localhost") as instance:
        yield instance


def test_root_rewrites_child_uris_through_the_gateway(upstream, client):
    seen, _ = upstream
    response = client.get("/api/connector/google3d/root.json")
    assert response.status_code == 200
    body = response.json()
    uris = [c["content"]["uri"] for c in body["root"]["children"]]
    assert uris == ["/api/connector/google3d/datasets/CgIYAQ/files/AJVsH2abc.json?session=CJjs2NODzvfd",
                    "/api/connector/google3d/datasets/CgIYAQ/files/AJVsH2def.glb?session=CJjs2NODzvfd"]
    assert str(seen[0].url) == "https://tile.googleapis.com/v1/3dtiles/root.json"


def test_key_travels_only_in_the_upstream_header(upstream, client):
    seen, _ = upstream
    responses = [client.get("/api/connector/google3d/root.json"),
                 client.get("/api/connector/google3d/datasets/CgIYAQ/files/AJVsH2def.glb?session=CJjs2NODzvfd")]
    for request in seen:
        assert request.headers["x-goog-api-key"] == CANARY
        assert CANARY not in str(request.url)
    for response in responses:
        assert CANARY not in response.text if response.headers["content-type"].startswith("application/json") else CANARY.encode() not in response.content
        assert all(CANARY not in v for v in response.headers.values())
    assert CANARY not in json.dumps(client.get("/api/connector").json())
    assert CANARY not in client.get("/api/health").text


def test_tiles_keep_googles_private_cache_policy(upstream, client):
    tile = client.get("/api/connector/google3d/datasets/CgIYAQ/files/AJVsH2def.glb?session=CJjs2NODzvfd")
    assert tile.status_code == 200 and tile.content.startswith(b"glTF")
    assert tile.headers["content-type"] == "model/gltf-binary"
    assert tile.headers["cache-control"].startswith("private")
    assert tile.headers["etag"] == "CO_H4tcG"


def test_nothing_is_kept_server_side(upstream, client):
    """Google marks tiles private, so the server must ask upstream every time instead of serving a stored copy."""
    seen, _ = upstream
    for _ in range(3):
        client.get("/api/connector/google3d/datasets/CgIYAQ/files/AJVsH2def.glb?session=CJjs2NODzvfd")
    assert len(seen) == 3


def test_revalidation_returns_not_modified(upstream, client):
    response = client.get("/api/connector/google3d/root.json", headers={"if-none-match": "CO_H4tcG"})
    assert response.status_code == 304
    assert response.headers["etag"] == "CO_H4tcG"


@pytest.mark.parametrize("path", [
    "/api/connector/google3d/root.json?key=attacker",
    "/api/connector/google3d/datasets/CgIYAQ/files/a.glb?session=s&key=attacker",
    "/api/connector/google3d/datasets/CgIYAQ/files/a.glb",
    "/api/connector/google3d/datasets/CgIYAQ/files/a.glb?session=bad%20value",
    "/api/connector/google3d/datasets/CgIYAQ/files/a.png?session=s",
    "/api/connector/google3d/datasets/../../v1/createSession?session=s",
    "/api/connector/google3d/v1/2dtiles/0/0/0?session=s",
    "/api/connector/google3d/datasets/CgIYAQ/files/https:%2F%2Fattacker.example%2Fx.glb?session=s",
])
def test_only_documented_tileset_requests_are_forwarded(upstream, client, path):
    seen, _ = upstream
    assert client.get(path).status_code in (404, 422)
    assert seen == []


@pytest.mark.parametrize("status,expected", [(403, 502), (401, 502), (429, 429), (404, 404)])
def test_upstream_refusals_are_explained_without_leaking(upstream, client, status, expected):
    _, behaviour = upstream
    behaviour["status"] = status
    response = client.get("/api/connector/google3d/root.json")
    assert response.status_code == expected
    assert CANARY not in response.text


def test_status_reports_availability_and_probes_once(upstream, client):
    seen, behaviour = upstream
    assert client.get("/api/connector/google3d/status").json() == {"configured": True, "available": True, "reason": None}
    client.get("/api/connector/google3d/status")
    assert len(seen) == 1  # cached for ten minutes
    google_tiles._status.update(checked=0.0, value=None)
    behaviour["status"] = 403
    state = client.get("/api/connector/google3d/status").json()
    assert state["available"] is False and "Map Tiles API" in state["reason"]


def test_without_a_key_nothing_is_requested(upstream, client, monkeypatch):
    seen, _ = upstream
    for name in google_tiles.KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(google_tiles, "get_settings", lambda: type("S", (), {"root": __import__("pathlib").Path("/nonexistent")})())
    assert client.get("/api/connector/google3d/status").json()["configured"] is False
    assert client.get("/api/connector/google3d/root.json").status_code == 503
    assert client.get("/api/health").json()["capabilities"]["photorealistic_3d"] is False
    assert seen == []


def test_key_is_read_from_env_file(monkeypatch, tmp_path):
    for name in google_tiles.KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(f"OTHER=1\nVITE_GOOGLE_MAPS_KEY='{CANARY}'\n")
    monkeypatch.setattr(google_tiles, "get_settings", lambda: type("S", (), {"root": tmp_path})())
    assert google_tiles.api_key() == CANARY


def test_google_key_header_is_refused_for_other_providers():
    with pytest.raises(connector.ConnectorError, match="credentials are not permitted"):
        connector._headers("imagery", {"X-Goog-Api-Key": CANARY})


async def test_google_key_is_stripped_on_cross_host_redirect():
    response = httpx.Response(302, headers={"location": "https://tile.googleapis.com/elsewhere"})
    _, headers = connector._redirect("google", "https://tile.googleapis.com/v1/3dtiles/root.json", response, {"X-Goog-Api-Key": CANARY})
    assert headers["X-Goog-Api-Key"] == CANARY  # same host keeps it
    with pytest.raises(connector.ConnectorError):
        connector._redirect("google", "https://tile.googleapis.com/v1/3dtiles/root.json",
                            httpx.Response(302, headers={"location": "https://attacker.example/x"}), {"X-Goog-Api-Key": CANARY})
