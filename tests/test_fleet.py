"""Fleet coordination: the protocol, the central database, the auction, airspace separation and the agents' rounds."""
import importlib
import time

from fastapi.testclient import TestClient
import pytest

from terra.fleet import activities, planner
from terra.fleet.db import FleetDB

api = importlib.import_module("terra.app")
TOKEN = "fleet-test-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}
AREA = {"name": "Etna", "bbox": [14.85, 37.6, 15.15, 37.85], "targets": [
    {"id": "patch-1", "kind": "change", "label": "change patch 1", "lon": 15.06, "lat": 37.75, "radius_m": 900, "priority": 0.9, "needs": ["ir"]},
    {"id": "summit", "kind": "summit", "label": "Mount Etna", "lon": 14.995, "lat": 37.751, "radius_m": 600, "priority": 0.6}]}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TERRA_DRONE_TOKEN", TOKEN)

    async def no_temporal(reason):
        return False
    monkeypatch.setattr("terra.fleet.api.ensure_coordinator", no_temporal)
    # The workspace: a same-origin browser request from this machine.
    with TestClient(api.app, base_url="http://localhost", headers={"sec-fetch-site": "same-origin"}) as instance:
        yield instance


def _register(client, ident, origin="virtual", aircraft="quad", sensors=("eo", "ir"), relay=False):
    caps = {"sensors": list(sensors), "relay": relay, "cruise_mps": 10, "max_mps": 16, "endurance_min": 40}
    assert client.post("/api/fleet/register", json={"id": ident, "name": ident, "origin": origin, "aircraft": aircraft, "capabilities": caps}, headers=AUTH).status_code == 201


def _fly(client, ident, seq, lon=15.0, lat=37.62, agl=100, battery=90, **extra):
    return client.post("/api/fleet/telemetry", json={"id": ident, "seq": seq, "lon": lon, "lat": lat, "alt_m": agl, "agl_m": agl,
                                                     "heading_deg": 10, "speed_mps": 8, "battery_pct": battery, "link_dbm": -60, **extra}, headers=AUTH)


# ---------------------------------------------------------------- protocol
def test_connections_need_the_token(client, monkeypatch):
    assert client.post("/api/fleet/register", json={"id": "a", "name": "A"}).status_code == 401
    monkeypatch.delenv("TERRA_DRONE_TOKEN")
    assert client.post("/api/fleet/register", json={"id": "a", "name": "A"}, headers=AUTH).status_code == 403


def test_telemetry_requires_registration_and_ignores_old_sequence_numbers(client):
    assert _fly(client, "d1", 1).status_code == 409
    _register(client, "d1")
    assert _fly(client, "d1", 5).json()["accepted"] is True
    assert _fly(client, "d1", 5).json()["accepted"] is False  # duplicate
    assert _fly(client, "d1", 3).json()["accepted"] is False  # late
    _register(client, "d1")  # a restarted ground station starts a new session and counts from 1 again
    assert _fly(client, "d1", 1).json()["accepted"] is True
    state = client.get("/api/fleet").json()
    [drone] = state["drones"]
    assert drone["link_state"] == "online" and drone["state"]["seq"] == 1 and len(drone["track"]) == 2
    assert TOKEN not in str(state)


@pytest.mark.parametrize("patch", [{"id": "../x"}, {"lat": 91}, {"video_url": "javascript:alert(1)"}, {"seq": -1}])
def test_bad_telemetry_is_rejected(client, patch):
    _register(client, "d1")
    assert client.post("/api/fleet/telemetry", json={"id": "d1", "seq": 1, "lon": 15, "lat": 37.7, "alt_m": 10, **patch}, headers=AUTH).status_code == 422


def test_task_lifecycle_moves_forward_only(client):
    client.post("/api/fleet/area", json=AREA)
    _register(client, "d1"); _fly(client, "d1", 1)
    task = client.post("/api/fleet/tasks", json={"drone_id": "d1", "kind": "hold", "lon": 15.0, "lat": 37.7}).json()["task"]
    assert _fly(client, "d1", 2).json()["task"]["id"] == task["id"]
    ack = lambda status: client.post(f"/api/fleet/tasks/{task['id']}/ack", json={"id": "d1", "status": status}, headers=AUTH).json()["task"]["status"]  # noqa: E731
    assert ack("active") == "active"
    assert ack("acknowledged") == "active"  # cannot move backwards
    assert ack("completed") == "completed"
    assert _fly(client, "d1", 3).json()["task"] is None


def test_real_aircraft_are_never_flown_from_the_browser(client):
    _register(client, "real-1", origin="real"); _fly(client, "real-1", 1)
    assert client.post("/api/fleet/drones/real-1/control", json={"lon": 15, "lat": 37.7, "agl_m": 100, "heading_deg": 0}).status_code == 403
    _register(client, "v-1"); _fly(client, "v-1", 1)
    assert client.post("/api/fleet/drones/v-1/control", json={"lon": 15, "lat": 37.7, "agl_m": 100, "heading_deg": 0}).status_code == 200
    assert _fly(client, "v-1", 2).json()["control"]["agl_m"] == 100


def test_foreign_sites_cannot_task_aircraft(client):
    _register(client, "d1"); _fly(client, "d1", 1)
    response = client.post("/api/fleet/tasks", json={"drone_id": "d1", "kind": "return"}, headers={"origin": "https://attacker.example"})
    assert response.status_code == 403


def test_operations_export_as_operational_intents(client):
    client.post("/api/fleet/area", json=AREA)
    _register(client, "d1"); _fly(client, "d1", 1)
    activities.run_coordination_round({"round": 0})
    intents = client.get("/api/fleet/operations").json()["operational_intents"]
    assert intents and intents[0]["details"]["volumes"][0]["volume"]["altitude_lower"]["reference"] == "AGL"


# ---------------------------------------------------------------- planner
def _drone(ident, lon=15.0, lat=37.62, battery=90, aircraft="quad", sensors=("eo", "ir"), relay=False, agl=100, origin="virtual"):
    return {"id": ident, "name": ident, "origin": origin, "aircraft": aircraft, "link_state": "online", "age_s": 0.5,
            "capabilities": {"sensors": list(sensors), "relay": relay, "cruise_mps": 10, "endurance_min": 40},
            "state": {"lon": lon, "lat": lat, "battery_pct": battery, "agl_m": agl, "link_dbm": -60}}


AREA_STATE = {"bbox": AREA["bbox"], "launch": [15.0, 37.725]}
TARGETS = [{**t, "needs": t.get("needs", []), "last_observed": None} for t in AREA["targets"]]


def test_auction_gives_each_job_to_the_cheapest_capable_aircraft():
    near = _drone("near", lon=15.05, lat=37.74); far = _drone("far", lon=14.9, lat=37.62); blind = _drone("blind", lon=15.06, lat=37.75, sensors=("eo",))
    d = planner.plan_round(AREA_STATE, TARGETS, [near, far, blind], [], [], set(), time.time())
    awarded = {t["job_key"]: t["drone_id"] for t in d["tasks"] if t["job_key"]}
    assert awarded["orbit:patch-1"] == "near"  # the IR-capable aircraft closest to the patch
    assert "blind" not in [awarded.get("orbit:patch-1")]


def test_low_battery_returns_and_the_job_is_handed_over():
    tired = _drone("tired", lon=15.06, lat=37.75, battery=18); fresh = _drone("fresh", lon=15.0, lat=37.64, battery=95)
    holding = [{"id": "t1", "drone_id": "tired", "job_key": "orbit:patch-1", "kind": "orbit", "label": "Orbit change patch 1", "params": {"lon": 15.06, "lat": 37.75, "agl_m": 150},
                "status": "active", "created_by": "coordinator", "created_at": time.time() - 100, "hold_until": None}]
    d = planner.plan_round(AREA_STATE, TARGETS, [tired, fresh], holding, [], set(), time.time())
    kinds = {t["drone_id"]: t["kind"] for t in d["tasks"]}
    assert kinds["tired"] == "return"
    assert any(t["drone_id"] == "fresh" and t["job_key"] == "orbit:patch-1" for t in d["tasks"])
    assert any(m.get("kind") == "handoff" for m in d["messages"])


def test_overlapping_airspace_gets_separate_altitude_layers():
    plans = [{"drone_id": "a", "job_key": "j1", "kind": "orbit", "params": {"lon": 15.0, "lat": 37.7, "radius_m": 300, "agl_m": 150}, "priority": 0.9, "aircraft": "quad"},
             {"drone_id": "b", "job_key": "j2", "kind": "orbit", "params": {"lon": 15.002, "lat": 37.701, "radius_m": 300, "agl_m": 150}, "priority": 0.8, "aircraft": "quad"},
             {"drone_id": "c", "job_key": "j3", "kind": "orbit", "params": {"lon": 15.3, "lat": 37.9, "radius_m": 300, "agl_m": 150}, "priority": 0.7, "aircraft": "quad"}]
    placed = {p["drone_id"]: p for p in planner.deconflict(plans)}
    assert abs(placed["a"]["agl_m"] - placed["b"]["agl_m"]) >= planner.SEPARATION_M
    assert placed["c"]["agl_m"] == 150 and not placed["c"]["moved"]  # far away: keeps its height


def test_people_and_manual_flight_take_precedence():
    a = _drone("a", lon=15.06, lat=37.75)
    pinned = [{"id": "t1", "drone_id": "a", "job_key": None, "kind": "hold", "label": "Hold", "params": {"lon": 15.0, "lat": 37.7, "agl_m": 100},
               "status": "active", "created_by": "operator", "created_at": time.time(), "hold_until": time.time() + 600}]
    d = planner.plan_round(AREA_STATE, TARGETS, [a], pinned, [], set(), time.time())
    assert not [t for t in d["tasks"] if t["drone_id"] == "a"]
    d = planner.plan_round(AREA_STATE, TARGETS, [a], [], [], {"a"}, time.time())
    assert not [t for t in d["tasks"] if t["drone_id"] == "a"]


def test_unacknowledged_real_tasks_are_reopened():
    real = _drone("r", origin="real"); other = _drone("v", lon=15.05, lat=37.74)
    stale = [{"id": "t9", "drone_id": "r", "job_key": "orbit:patch-1", "kind": "orbit", "label": "Orbit change patch 1", "params": {"lon": 15.06, "lat": 37.75, "agl_m": 150},
              "status": "assigned", "created_by": "coordinator", "created_at": time.time() - 60, "hold_until": None}]
    d = planner.plan_round(AREA_STATE, TARGETS, [real, other], stale, [], set(), time.time())
    assert "t9" in d["released"] and any(m["kind"] == "unacknowledged" for m in d["messages"])
    assert any(t["drone_id"] == "v" and t["job_key"] == "orbit:patch-1" for t in d["tasks"])


def test_agents_report_health_and_traffic():
    launch = [15.0, 37.725]
    low = _drone("low", lon=15.1, lat=37.8, battery=12)
    found = {f["kind"] for f in planner.assess(low, None, None, [low], launch)}
    assert "battery" in found
    a, b = _drone("a", lon=15.05, lat=37.76, agl=120), _drone("b", lon=15.0502, lat=37.7601, agl=125)
    assert "proximity" in {f["kind"] for f in planner.assess(a, None, None, [a, b], launch)}
    pad_a, pad_b = _drone("pa", lon=15.0, lat=37.7251, agl=40), _drone("pb", lon=15.0002, lat=37.7252, agl=45)
    assert "proximity" not in {f["kind"] for f in planner.assess(pad_a, None, None, [pad_a, pad_b], launch)}  # sequenced departures
    lost = {**_drone("x"), "link_state": "lost", "age_s": 90}
    assert planner.assess(lost, None, None, [lost], launch)[0]["urgent"]


# ---------------------------------------------------------------- rounds against the central database
def test_rounds_coordinate_through_the_database(client, tmp_path):
    client.post("/api/fleet/area", json=AREA)
    for i, (lon, lat) in enumerate([(15.05, 37.74), (15.0, 37.75), (14.95, 37.65)]):
        _register(client, f"d{i}", relay=(i == 2)); _fly(client, f"d{i}", 1, lon=lon, lat=lat)
    for i in range(3):  # agents bid from their own state
        assert activities.run_drone_agent_round({"drone_id": f"d{i}", "round": 0})["retire"] is False
    result = activities.run_coordination_round({"round": 1, "workflow_id": "test"})
    assert result["summary"]["online"] == 3 and result["summary"]["assigned"] >= 2
    db = FleetDB(tmp_path / "terra.db")
    tasks = db.tasks()
    assert {t["drone_id"] for t in tasks} == {"d0", "d1", "d2"}
    assert len({o["drone_id"] for o in db.operations()}) >= 2
    texts = " ".join(m["body"].get("text", "") for m in db.messages())
    assert "Awarded at auction" in texts
    # Nothing new happens on a repeat round with the same state: no thrashing, no repeated messages.
    before = len(db.messages(500)); activities.run_coordination_round({"round": 2}); after = len(db.messages(500))
    assert after == before and {t["id"] for t in db.tasks()} == {t["id"] for t in tasks}


def test_scarce_capabilities_are_kept_for_the_jobs_that_need_them():
    # One relay-equipped aircraft and an urgent relay job: it must go to the relay, not to an orbit others can fly.
    far = _drone("far", lon=15.12, lat=37.84)  # beyond direct radio range of the launch site
    relay = _drone("relay", lon=15.0, lat=37.73, relay=True, sensors=("eo",))
    quad = _drone("quad", lon=15.06, lat=37.75)
    d = planner.plan_round(AREA_STATE, TARGETS, [far, relay, quad], [], [], set(), time.time())
    awarded = {t["drone_id"]: t["job_key"] for t in d["tasks"] if t["job_key"]}
    assert awarded.get("relay") == "relay"
    assert awarded.get("quad", "").startswith("orbit:")


def test_refused_jobs_and_unsupported_task_kinds_are_not_offered_again():
    mav = _drone("mav"); mav["capabilities"]["tasks"] = ["orbit", "relay", "hold", "standby", "return"]
    d = planner.plan_round(AREA_STATE, TARGETS, [mav], [], [], set(), time.time())
    assert not any(t["kind"] in ("survey", "perimeter") for t in d["tasks"] if t["drone_id"] == "mav")
    first = next(t for t in d["tasks"] if t["drone_id"] == "mav")
    again = planner.plan_round(AREA_STATE, TARGETS, [mav], [], [], set(), time.time(), {("mav", first["job_key"])})
    assert all(t["job_key"] != first["job_key"] for t in again["tasks"] if t["drone_id"] == "mav")


def test_aircraft_far_from_the_operation_are_left_alone():
    elsewhere = _drone("elsewhere", lon=-75.5, lat=3.7, battery=86)  # another continent
    d = planner.plan_round(AREA_STATE, TARGETS, [elsewhere], [], [], set(), time.time())
    assert not [t for t in d["tasks"] if t["drone_id"] == "elsewhere"]
    assert d["away"] == ["elsewhere"] and any(m["kind"] == "out-of-area" for m in d["messages"])


# ---------------------------------------------------------------- mission profiles, search, notes, hand-offs, enrollment
def test_search_profile_plans_expanding_square_searches(client, tmp_path):
    assert client.post("/api/fleet/area", json={**AREA, "profile": "search"}).json()["targets"] == 4
    targets = FleetDB(tmp_path / "terra.db").targets()
    assert {t["kind"] for t in targets} == {"search"} and len(targets) == 4
    jobs = planner.build_jobs(AREA_STATE, targets, [])
    search = [j for j in jobs if j["kind"] == "search"]
    assert len(search) == 4 and all(j["params"]["size_m"] >= 400 for j in search)
    route = planner.search_points(search[0]["params"])
    start = route[0]
    assert start == (search[0]["params"]["lon"], search[0]["params"]["lat"]) and len(route) > 8
    # Each leg is a whole number of lanes and the square grows outwards.
    reach = [planner.distance(*start, *p) for p in route]
    assert reach[-1] > reach[2] > 0


def test_thermal_profile_sends_ir_aircraft_to_the_warmest_ground(client, monkeypatch, tmp_path):
    import base64
    from io import BytesIO
    import numpy as np
    from PIL import Image
    pixels = np.full((40, 40, 2), (60, 255), dtype=np.uint8); pixels[5, 30] = (255, 255); pixels[34, 4] = (220, 255)
    buffer = BytesIO(); Image.fromarray(pixels, "LA").save(buffer, "PNG")
    scene = {"available": True, "png": base64.b64encode(buffer.getvalue()).decode(), "minC": 10.0, "maxC": 60.0, "bounds": AREA["bbox"], "date": "2026-10-30"}

    async def fake_thermal(bbox):
        return scene
    monkeypatch.setattr("terra.thermal.thermal", fake_thermal)
    client.post("/api/fleet/area", json={**AREA, "profile": "thermal"})
    hot = [t for t in FleetDB(tmp_path / "terra.db").targets() if t["kind"] == "hotspot"]
    assert len(hot) >= 2 and all(t["needs"] == ["ir"] for t in hot)
    warmest = max(hot, key=lambda t: t["priority"])
    assert "≥60 °C" in warmest["label"] and warmest["lon"] > 15.0 and warmest["lat"] > 37.8  # at the top of the scale
    assert any(t["label"].endswith(" 53 °C") for t in hot)  # below the top: the reading itself


def test_agents_write_field_notes_and_relieve_the_aircraft_they_replace(client, monkeypatch, tmp_path):
    import numpy as np
    client.post("/api/fleet/area", json=AREA)
    _register(client, "d0"); _fly(client, "d0", 1, lon=15.05, lat=37.74, agl=120, gimbal={"heading_deg": 90, "pitch_deg": -60})
    data = {"thermal": {"c": np.full((20, 20), 48.0), "bounds": AREA["bbox"], "date": "2026-10-30", "platform": "Landsat 9", "p95": 41.0},
            "weather": {"temperature_2m": 21.0, "wind_speed_10m": 14.0, "wind_direction_10m": 250}}
    monkeypatch.setattr("terra.fleet.observe.area_data", lambda bbox, investigation: data)
    monkeypatch.setattr(activities, "_elevation", lambda lon, lat: 1480.0)
    db = FleetDB(tmp_path / "terra.db")
    # A person's orbit (no auction job behind it) must not trip the agent's bookkeeping.
    task = db.create_task("d0", "orbit", "Orbit change patch 1", {"lon": 15.06, "lat": 37.75, "radius_m": 200, "agl_m": 120, "relieves": "d9"},
                          "operator", "test", 0.9, None, None)
    assert client.post(f"/api/fleet/tasks/{task['id']}/ack", json={"id": "d0", "status": "active"}, headers=AUTH).status_code == 200
    assert activities.run_drone_agent_round({"drone_id": "d0", "round": 0})["note"] == "warm"
    activities.run_drone_agent_round({"drone_id": "d0", "round": 1})  # within two minutes: no second note
    state = client.get("/api/fleet").json()
    [note] = state["notes"]
    assert note["drone_id"] == "d0" and note["data"]["surface_c"] == 48.0 and note["data"]["elevation_m"] == 1480
    assert "Landsat 9 surface temperature, 2026-10-30" in note["text"] and "wind 14 km/h from the W" in note["text"]
    assert note["lon"] > 15.05  # the camera looks east, so the noted ground is east of the aircraft
    kinds = {m["kind"]: m for m in state["messages"]}
    assert kinds["relieved"]["recipient"] == "agent:d9" and "relieved" in kinds["relieved"]["body"]["text"]
    assert "observation" in kinds


def test_enrolled_aircraft_use_their_own_revocable_token(client, tmp_path):
    reply = client.post("/api/fleet/enroll", json={"id": "uav-7", "name": "Ridge Seven", "sensors": ["eo", "ir"], "connection": "udpin:0.0.0.0:14550"})
    assert reply.status_code == 201
    token = reply.json()["token"]
    assert token.startswith("terra_") and token in reply.json()["command"] and "--mavlink udpin:0.0.0.0:14550" in reply.json()["command"]
    own = {"authorization": f"Bearer {token}"}
    assert client.post("/api/fleet/register", json={"id": "uav-7", "name": "Ridge Seven"}, headers=own).status_code == 201
    assert client.post("/api/fleet/register", json={"id": "uav-8", "name": "Someone else"}, headers=own).status_code == 403
    listed = client.get("/api/fleet/enrollments").json()["enrollments"]
    assert listed[0]["drone_id"] == "uav-7" and token not in str(listed)
    assert token not in (tmp_path / "terra.db").read_bytes().decode("latin-1")  # only its hash is kept
    assert client.post("/api/fleet/enrollments/uav-7/revoke").json()["revoked"] == 1
    assert client.post("/api/fleet/register", json={"id": "uav-7", "name": "Ridge Seven"}, headers=own).status_code == 401
    assert client.post("/api/fleet/enroll", json={"id": "x", "name": "x", "connection": "udp:1; rm -rf /"}).status_code == 422


def test_bridge_flies_every_task_kind_as_an_ardupilot_mission():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("drone_bridge", Path(__file__).parents[1] / "scripts" / "drone_bridge.py")
    bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
    home = (15.0, 37.725, 1200.0)
    for kind, params in [("orbit", {"lon": 15.06, "lat": 37.75, "radius_m": 900, "agl_m": 150}), ("hold", {"lon": 15.0, "lat": 37.73, "agl_m": 100}),
                         ("survey", {"bbox": [15.0, 37.7, 15.02, 37.72], "agl_m": 120}), ("search", {"lon": 15.02, "lat": 37.71, "size_m": 800, "agl_m": 90})]:
        items = bridge.mission_items({"kind": kind, "params": params}, home, airborne=False, aircraft="quad")
        assert items[0]["frame"] == bridge.FRAME_GLOBAL and items[1]["command"] == bridge.CMD_TAKEOFF
        if kind == "orbit":
            assert items[3]["p3"] == 250  # a quad's orbit radius is capped
        if kind in ("orbit", "survey", "search"):
            assert items[-1]["command"] == bridge.CMD_DO_JUMP and items[-1]["p2"] == -1
    airborne = bridge.mission_items({"kind": "orbit", "params": {"lon": 15.06, "lat": 37.75, "agl_m": 150}}, home, airborne=True, aircraft="fixed-wing")
    assert bridge.CMD_TAKEOFF not in [i["command"] for i in airborne] and airborne[2]["p3"] >= 80
    with pytest.raises(ValueError):
        bridge.mission_items({"kind": "dance", "params": {}}, home, False, "quad")


# ---------------------------------------------------------------- operator routes: the workspace or the token, never a guess
OPERATOR_ROUTES = [("GET", "/api/fleet", None), ("GET", "/api/fleet/operations", None), ("POST", "/api/fleet/area", AREA),
                   ("POST", "/api/fleet/tasks", {"drone_id": "d1", "kind": "hold", "lon": 15.0, "lat": 37.7}),
                   ("POST", "/api/fleet/drones/d1/control", {"lon": 15.0, "lat": 37.7, "agl_m": 100, "heading_deg": 0}),
                   ("POST", "/api/fleet/drones/d1/release", None), ("GET", "/api/fleet/enrollments", None),
                   ("POST", "/api/fleet/enroll", {"id": "x1", "name": "x1"}), ("POST", "/api/fleet/enrollments/x1/revoke", None)]


def test_operator_routes_need_the_workspace_or_the_token(client):
    _register(client, "d1"); _fly(client, "d1", 1)
    aircraft_token = client.post("/api/fleet/enroll", json={"id": "d1", "name": "d1"}).json()["token"]
    bare = TestClient(api.app, base_url="http://localhost")  # curl, a script, or anything that is not the workspace
    for method, path, body in OPERATOR_ROUTES:
        call = lambda c, **kw: c.request(method, path, json=body, **kw)  # noqa: E731
        assert call(bare).status_code == 403, path
        assert call(bare, headers={"authorization": "Bearer wrong"}).status_code == 401, path
        assert call(bare, headers={"authorization": f"Bearer {aircraft_token}"}).status_code == 401, path  # an aircraft's token is not an operator's
        assert call(bare, headers={"sec-fetch-site": "same-origin", "origin": "https://evil.example"}).status_code == 403, path
        assert call(bare, headers=AUTH).status_code < 400, path
        assert call(client).status_code < 400, path


def test_other_machines_need_the_token_even_with_browser_headers(client, monkeypatch):
    monkeypatch.setattr("terra.fleet.api.LOCAL", {"127.0.0.1"})  # the test transport now counts as another machine
    assert client.get("/api/fleet").status_code == 403
    assert client.get("/api/fleet", headers=AUTH).status_code == 200


def test_cloud_proxy_can_run_only_a_simulation_and_never_issue_aircraft_credentials(client, monkeypatch):
    monkeypatch.setattr("terra.fleet.api.LOCAL", {"127.0.0.1"})
    monkeypatch.setenv("TERRA_FLEET", "virtual")
    monkeypatch.setenv("TERRA_WORKSPACE_TOKEN", "test-workspace-capability")
    headers = {"x-terra-workspace-token": "test-workspace-capability"}
    assert client.get("/api/fleet").status_code == 403
    assert client.get("/api/fleet", headers={"x-terra-workspace-token": "wrong"}).status_code == 403
    response = client.get("/api/fleet", headers=headers)
    assert response.status_code == 200
    assert response.json()["operator_access"] is False
    assert response.json()["simulation"]["managed"] is True
    assert "test-workspace-capability" not in response.text
    assert client.post("/api/fleet/area", json=AREA, headers=headers).status_code == 200
    assert client.post("/api/fleet/enroll", json={"id": "x", "name": "X"}, headers=headers).status_code == 403
    assert client.get("/api/fleet/enrollments", headers=headers).status_code == 403
    assert client.get("/api/fleet", headers={**headers, "origin": "https://attacker.example"}).status_code == 403
    assert client.post("/api/fleet/register", json={"id": "real-1", "name": "Real", "origin": "real"}, headers=AUTH).status_code == 403
    db = FleetDB(api.get_settings().database)
    db.register({"id": "existing-real", "name": "Real", "origin": "real", "aircraft": "quad", "capabilities": {}})
    assert client.get("/api/fleet", headers=headers).status_code == 403
    assert client.get("/api/fleet", headers=AUTH).status_code == 200


def test_operator_tasks_are_checked(client):
    _register(client, "d1"); _fly(client, "d1", 1)
    task = lambda **kw: client.post("/api/fleet/tasks", json={"drone_id": "d1", **kw})  # noqa: E731
    assert task(kind="survey", bbox=[1e9, 0, 1e9 + 0.5, 0.5]).status_code == 422
    assert task(kind="survey", bbox=[15.0, 37.7, 15.1, 37.8]).status_code == 201
    assert task(kind="search", lon=15.0, lat=37.7, size_m=900).json()["task"]["params"]["size_m"] == 900
    assert client.post("/api/fleet/drones/nobody/release").status_code == 404
    _register(client, "blank")  # registered, never reported a position, and no monitoring area yet
    assert client.post("/api/fleet/tasks", json={"drone_id": "blank", "kind": "return"}).status_code == 422


def test_old_telemetry_is_pruned_whatever_the_sequence_numbers(client, tmp_path, monkeypatch):
    from terra.fleet import db as fleet_db_module
    _register(client, "d1")
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(fleet_db_module, "now", lambda: clock["t"])
    monkeypatch.setitem(fleet_db_module._pruned, "at", 0.0)
    for seq in (3, 7):  # odd sequence numbers never hit a "every 200th message" rule
        _fly(client, "d1", seq); clock["t"] += fleet_db_module.TELEMETRY_KEEP_S + 120
    _fly(client, "d1", 11)
    assert len(FleetDB(tmp_path / "terra.db").track("d1", 10**9)) == 1


def test_the_workspace_behind_the_dev_proxy_is_accepted(client):
    # Vite's proxy rewrites Host to the API (127.0.0.1:8010) while the browser sends the page's own Origin.
    proxied = TestClient(api.app, base_url="http://127.0.0.1:8010", headers={"sec-fetch-site": "same-origin"})
    for page in ("http://127.0.0.1:5173", "http://localhost:5173"):
        assert proxied.get("/api/fleet", headers={"origin": page}).status_code == 200, page
        assert proxied.post("/api/fleet/area", json=AREA, headers={"origin": page}).status_code == 200, page
    assert proxied.get("/api/fleet", headers={"origin": "http://evil.example:5173"}).status_code == 403


# ---------------------------------------------------------------- orchestration: fleet commands, cadence, supervision, deployments
def test_fleet_commands_hold_recall_and_resume(client, tmp_path):
    client.post("/api/fleet/area", json=AREA)
    for i, (lon, lat) in enumerate([(15.05, 37.74), (15.0, 37.75)]):
        _register(client, f"d{i}"); _fly(client, f"d{i}", 1, lon=lon, lat=lat, agl=120)
    _register(client, "pad"); _fly(client, "pad", 1, landed=True, agl=0)  # on the ground: not held, not recalled
    db = FleetDB(tmp_path / "terra.db")
    activities.run_coordination_round({"round": 1})
    assert client.post("/api/fleet/command", json={"action": "hold"}).json()["mode"]["mode"] == "hold"
    result = activities.run_coordination_round({"round": 2})
    held = {t["drone_id"]: t for t in db.tasks()}
    assert {d for d, t in held.items() if t["kind"] == "hold" and t["created_by"] == "fleet-command"} == {"d0", "d1"}
    assert held["d0"]["params"]["lon"] == 15.05 and sorted(result["wake"]) == ["d0", "d1"] and result["next_s"] == 2.0
    activities.run_coordination_round({"round": 3})  # holding again changes nothing
    assert {t["id"] for t in db.tasks()} == {t["id"] for t in held.values()}
    client.post("/api/fleet/command", json={"action": "recall"})
    activities.run_coordination_round({"round": 4})
    assert {t["drone_id"] for t in db.tasks() if t["kind"] == "return"} == {"d0", "d1"}
    client.post("/api/fleet/command", json={"action": "resume"})
    activities.run_coordination_round({"round": 5})
    assert not [t for t in db.tasks() if t["created_by"] == "fleet-command"]
    assert any(m["body"].get("text", "").startswith("The fleet resumed") for m in db.messages())
    assert client.get("/api/fleet").json()["mode"]["mode"] == "normal"
    assert client.post("/api/fleet/command", json={"action": "dance"}).status_code == 422


def test_agents_and_coordinator_pace_themselves():
    busy = {"link_state": "online", "state": {"landed": False}}
    assert activities.cadence_for_agent(busy, {"status": "assigned"}, False) == 2.0
    assert activities.cadence_for_agent(busy, {"status": "active"}, False) == 3.0
    assert activities.cadence_for_agent({"link_state": "online", "state": {"landed": True}}, None, False) == 10.0
    assert activities.cadence_for_agent({"link_state": "lost", "state": {}}, None, False) == 20.0
    assert activities.cadence_for_agent({"link_state": "lost", "state": {}}, None, True) == 2.0  # urgent wins
    assert activities.cadence_for_coordinator(True, 0, 3, "normal") == 2.0
    assert activities.cadence_for_coordinator(False, 0, 3, "normal") == 6.0
    assert activities.cadence_for_coordinator(False, 0, 0, "normal") == 15.0
    assert activities.AGENT_STALE_S >= 2 * 20  # never restarts an agent that is merely pacing itself


def test_the_coordinator_supervises_agents(client, tmp_path):
    client.post("/api/fleet/area", json=AREA)
    _register(client, "d0"); _fly(client, "d0", 1)
    db = FleetDB(tmp_path / "terra.db")
    db.heartbeat("agent:gone", "drone", "drone-agent-gone", "lost", {})  # an aircraft that left the fleet
    result = activities.run_coordination_round({"round": 1})
    assert result["need_agents"] == ["d0"]  # no agent yet: started
    assert "agent:gone" not in {a["id"] for a in db.agents()}
    activities.run_drone_agent_round({"drone_id": "d0", "round": 0})
    assert activities.run_coordination_round({"round": 2})["need_agents"] == []


def test_the_fleet_deploys_anywhere_with_its_own_launch_site(client, tmp_path):
    box = [-122.3, 46.1, -122.1, 46.25]
    reply = client.post("/api/fleet/area", json={"name": "Deployment at Mount St. Helens", "bbox": box, "launch": [-122.18, 46.2]})
    assert reply.status_code == 200 and reply.json()["area"]["launch"] == [-122.18, 46.2]
    assert client.post("/api/fleet/area", json={"name": "Too far", "bbox": box, "launch": [-120.0, 46.2]}).status_code == 422
    centre = client.post("/api/fleet/area", json={"name": "Centre", "bbox": box}).json()["area"]["launch"]
    assert centre == [pytest.approx(-122.2), pytest.approx(46.175)]


# ---------------------------------------------------------------- radar to drones and back
def _investigation_with_patches():
    from terra.signatures import signature
    from terra.store import utc_now
    spots = [{"rank": 1, "direction": "decrease", "pixels": 400, "area_km2": 0.9, "mean_change_db": -4.4, "centre": [37.75, 15.06],
              "bounds": [15.05, 37.745, 15.07, 37.755], "by_polarization": {"HH": {"mean_change_db": -0.6, "standardized_change": -0.8}, "HV": {"mean_change_db": -4.2, "standardized_change": -5.1}}},
             {"rank": 2, "direction": "decrease", "pixels": 120, "area_km2": 0.3, "mean_change_db": -6.0, "centre": [37.70, 15.10],
              "bounds": [15.095, 37.697, 15.105, 37.703], "by_polarization": {"HH": {"mean_change_db": -6.0, "standardized_change": -7.0}, "HV": {"mean_change_db": -5.5, "standardized_change": -6.0}}}]
    for s in spots:
        s["signature"] = signature(s["by_polarization"], s["direction"])
    now = utc_now()
    api.get_store().save_investigation({"id": "inv-etna", "title": "Mount Etna · Backscatter change", "bbox": [14.9, 37.6, 15.2, 37.85], "created_at": now, "updated_at": now,
                                        "result": {"measurements": {"change": {"hotspots": spots}}}})


def test_dispatch_sends_the_fleet_to_verify_each_patch(client, tmp_path):
    _investigation_with_patches()
    _register(client, "d0", sensors=("eo", "ir")); _fly(client, "d0", 1)
    reply = client.post("/api/fleet/dispatch", json={"investigation_id": "inv-etna"})
    assert reply.status_code == 200
    targets = {t["id"]: t for t in FleetDB(tmp_path / "terra.db").targets()}
    assert set(targets) == {"patch-1", "patch-2"}
    assert targets["patch-1"]["brief"]["hypothesis"] == "vegetation_loss" and "vegetation" in targets["patch-1"]["label"]
    assert targets["patch-2"]["brief"]["hypothesis"] == "water" and targets["patch-2"]["needs"] == ["ir"]  # the fleet has IR, and it helps here
    assert targets["patch-1"]["priority"] > targets["patch-2"]["priority"]  # strong evidence and the larger patch first
    area = reply.json()["area"]
    assert area["investigation_id"] == "inv-etna" and area["bbox"][0] < 15.05 and area["bbox"][2] > 15.105
    assert client.post("/api/fleet/dispatch", json={"investigation_id": "nope"}).status_code == 404


def test_agents_judge_what_they_see_against_the_patch(client, monkeypatch, tmp_path):
    import numpy as np
    _investigation_with_patches()
    client.post("/api/fleet/dispatch", json={"investigation_id": "inv-etna"})
    _register(client, "d0"); _fly(client, "d0", 1, lon=15.06, lat=37.75, agl=120, gimbal={"heading_deg": 0, "pitch_deg": -89})
    codes = np.full((10, 10), 10, dtype=np.uint8)  # ESA WorldCover 10 = tree cover (before the change)
    data = {"thermal": {"c": np.full((20, 20), 31.0), "bounds": [14.9, 37.6, 15.2, 37.85], "date": "2026-10-30", "platform": "Landsat 9", "p95": 40.0, "median": 25.0},
            "cover": {"codes": codes, "bounds": [14.9, 37.6, 15.2, 37.85], "names": {10: "Tree cover"}}}
    monkeypatch.setattr("terra.fleet.observe.area_data", lambda bbox, investigation: data)
    monkeypatch.setattr(activities, "_elevation", lambda lon, lat: 1500.0)
    db = FleetDB(tmp_path / "terra.db")
    task = db.create_task("d0", "orbit", "Orbit Patch 1", {"lon": 15.06, "lat": 37.75, "radius_m": 200, "agl_m": 120}, "coordinator", "test", 0.9, "orbit:patch-1", None)
    client.post(f"/api/fleet/tasks/{task['id']}/ack", json={"id": "d0", "status": "active"}, headers=AUTH)
    activities.run_drone_agent_round({"drone_id": "d0", "round": 0})
    [note] = db.notes()
    assert note["data"]["verdict"] == "supports" and note["kind"] == "verify" and "Check of patch 1" in note["text"]
    assert any(m["kind"] == "verification" and m["body"]["verdict"] == "supports" for m in db.messages())
    ev = client.get("/api/fleet/evidence/inv-etna").json()
    one = next(p for p in ev["patches"] if p["target_id"] == "patch-1")
    assert one["observations"] == 1 and one["counts"]["supports"] == 1 and one["overall"] == "supported" and one["aircraft"] == ["d0"]
    two = next(p for p in ev["patches"] if p["target_id"] == "patch-2")
    assert two["overall"] == "not yet observed" and ev["totals"]["supports"] == 1


def test_field_evidence_is_kept_with_the_investigation_and_its_report(client, monkeypatch, tmp_path):
    import json
    import numpy as np
    _investigation_with_patches()
    (api.get_settings().artifacts / "inv-etna").mkdir(parents=True, exist_ok=True)
    client.post("/api/fleet/dispatch", json={"investigation_id": "inv-etna"})
    assert client.get("/api/fleet/evidence/inv-etna").json()["recorded_at"] is None  # nothing observed yet: nothing kept
    _register(client, "d0", origin="sitl"); _fly(client, "d0", 1, lon=15.06, lat=37.75, agl=120, gimbal={"heading_deg": 0, "pitch_deg": -89})
    data = {"cover": {"codes": np.full((10, 10), 10, dtype=np.uint8), "bounds": [14.9, 37.6, 15.2, 37.85], "names": {10: "Tree cover"}}}
    monkeypatch.setattr("terra.fleet.observe.area_data", lambda bbox, investigation: data)
    monkeypatch.setattr(activities, "_elevation", lambda lon, lat: 1500.0)
    db = FleetDB(tmp_path / "terra.db")
    task = db.create_task("d0", "orbit", "Orbit Patch 1", {"lon": 15.06, "lat": 37.75, "radius_m": 200, "agl_m": 120}, "coordinator", "test", 0.9, "orbit:patch-1", None)
    client.post(f"/api/fleet/tasks/{task['id']}/ack", json={"id": "d0", "status": "active"}, headers=AUTH)
    activities.run_drone_agent_round({"drone_id": "d0", "round": 0})
    # The workspace ties an aircraft to the patch it verifies through its task's job key.
    assert next(d for d in client.get("/api/fleet").json()["drones"] if d["id"] == "d0")["task"]["job_key"] == "orbit:patch-1"

    ev = client.get("/api/fleet/evidence/inv-etna").json()
    assert ev["aircraft"] == [{"id": "d0", "name": "d0", "origin": "sitl"}] and ev["artifact"] == "field-verification.geojson"
    one = next(p for p in ev["patches"] if p["target_id"] == "patch-1")
    assert "judged" not in one and len(one["points"]) == 1 and one["points"][0][2] == one["latest"][0]["verdict"]
    # Kept with the investigation: the report shows it after the fleet moves on, and the investigation API returns it.
    kept = api.get_store().investigation("inv-etna")["field_verification"]
    assert kept["observations"] == 1 and kept["aircraft"][0]["origin"] == "sitl" and kept["patches"][0]["target_id"] == "patch-1"

    async def offline():
        raise RuntimeError("no Temporal in tests")
    monkeypatch.setattr(api, "temporal_client", offline)
    assert client.get("/api/investigations/inv-etna").json()["field_verification"]["fingerprint"] == kept["fingerprint"]
    # Unchanged evidence is not rewritten.
    assert client.get("/api/fleet/evidence/inv-etna").json()["recorded_at"] == kept["recorded_at"]
    # A GIS file of the patches and every judged observation, served with the investigation's other outputs.
    reply = client.get("/api/investigations/inv-etna/artifacts/field-verification.geojson")
    assert reply.status_code == 200
    features = json.loads(reply.content)["features"]
    patch = next(f for f in features if f["properties"]["kind"] == "patch" and f["properties"]["patch"] == 1)
    point = next(f for f in features if f["properties"]["kind"] == "observation")
    assert patch["geometry"]["type"] == "Polygon" and patch["properties"]["field_verdict"] == one["overall"]
    assert point["geometry"]["coordinates"] == [pytest.approx(15.06, abs=0.01), pytest.approx(37.75, abs=0.01)] and point["properties"]["aircraft_origin"] == "sitl"


def test_hosted_area_watches_share_the_simulation_capability_and_fail_closed(client, monkeypatch):
    monkeypatch.setattr("terra.fleet.api.LOCAL", {"127.0.0.1"})
    monkeypatch.setenv("TERRA_FLEET", "virtual")
    monkeypatch.setenv("TERRA_WORKSPACE_TOKEN", "test-watch-capability")
    headers = {"x-terra-workspace-token": "test-watch-capability"}
    assert client.get("/api/watches", headers=headers).status_code == 200
    assert client.post("/api/watches", json={}, headers=headers).status_code == 422
    for action in ("check", "stop"):
        assert client.post(f"/api/watches/watch-missing/{action}", headers=headers).status_code == 404
    assert client.post("/api/watches/watch-missing/decide", json={"proposal":"missing","action":"dismiss"}, headers=headers).status_code == 409
    assert client.get("/api/watches", headers={"x-terra-workspace-token":"wrong"}).status_code == 403
    assert client.get("/api/watches", headers={**headers,"origin":"https://foreign.example"}).status_code == 403
    db = FleetDB(api.get_settings().database)
    db.register({"id":"real-aircraft","name":"Real","origin":"real","aircraft":"quad","capabilities":{}})
    assert client.get("/api/watches", headers=headers).status_code == 403
