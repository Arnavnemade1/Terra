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
    with TestClient(api.app, base_url="http://localhost") as instance:
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
