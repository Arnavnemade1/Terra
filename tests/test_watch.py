"""Area watch: NISAR swath prediction, comparable acquisitions, significance, the API, and the durable loop."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from terra.watch import assess_change, distance_km, next_nisar_pass, pick_new_acquisition, sub_point

# NISAR (NORAD 65053) elements of 2026-11-15, fixed so the prediction is reproducible.
L1 = "1 65053U 25163A   26319.24014874  .00000229  00000+0  82365-4 0  9997"
L2 = "2 65053  98.4059 144.7287 0001264  90.3576 269.7759 14.42504413 68166"


def test_next_pass_matches_the_workspace_and_lies_in_the_left_swath():
    from sgp4.api import Satrec
    start = datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)
    at = next_nisar_pass(L1, L2, 37.75, 15.025, start)  # Mount Etna
    assert at is not None
    # The workspace (satellite.js, same elements) predicts 2026-11-20 04:08 UTC at 576 km from the track.
    assert at.strftime("%Y-%m-%d %H:%M") == "2026-11-20 04:08"
    lat, lon = sub_point(Satrec.twoline2rv(L1, L2), at)
    assert 420 <= distance_km(37.75, 15.025, lat, lon) <= 680


def test_next_pass_found_for_any_land_area_within_13_days():
    start = datetime(2026, 11, 15, tzinfo=timezone.utc)
    for lat, lon in [(19.4, -155.25), (-4.5, -74.5), (64.0, -22.4), (35.4, 138.7)]:
        at = next_nisar_pass(L1, L2, lat, lon, start)
        assert at is not None and (at - start).days <= 13


REF = {"id": "ref", "product": "GCOV", "date": "2026-10-15T17:55:02Z", "track": 51, "frame": 69, "direction": "DESCENDING"}


def test_new_acquisition_must_share_the_viewing_geometry_and_be_newer():
    obs = [
        {**REF, "id": "older", "date": "2026-10-03T17:55:00Z"},
        {**REF, "id": "other-track", "track": 52, "date": "2026-11-01T00:00:00Z"},
        {**REF, "id": "ascending", "direction": "ASCENDING", "date": "2026-11-02T00:00:00Z"},
        {**REF, "id": "next", "date": "2026-10-27T17:55:02Z"},
        {**REF, "id": "newest", "date": "2026-11-08T17:55:02Z"},
        {**REF, "id": "not-gcov", "product": "GSLC", "date": "2026-11-09T00:00:00Z"},
    ]
    assert pick_new_acquisition(obs, REF)["id"] == "newest"
    assert pick_new_acquisition([o for o in obs if o["id"] in ("older", "other-track")], REF) is None
    assert pick_new_acquisition(obs, None) is None


def _investigation(pct, spots, status="completed"):
    return {"status": status, "result": {"measurements": {
        "change": {"threshold_exceedance_pct": pct, "hotspots": spots},
        "observations": [{"date": "2026-10-15T17:55:02Z"}, {"date": "2026-06-17T17:55:06Z"}]}}}


def test_significance_by_share_of_area_or_patch_size():
    big = [{"area_km2": 0.59, "direction": "increase", "mean_change_db": 5.9}]
    small = [{"area_km2": 0.02, "direction": "decrease", "mean_change_db": -3.4}]
    a = assess_change(_investigation(1.1, big))
    assert a["significant"] and a["patches"] == 1 and "between 2026-06-17 and 2026-10-15" in a["summary"]
    assert assess_change(_investigation(0.2, big))["significant"]           # one large patch is enough
    assert not assess_change(_investigation(0.2, small))["significant"]     # small and rare: not worth a flight
    assert not assess_change(_investigation(None, [], status="failed"))["significant"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("TERRA_DATA_DIR", str(tmp_path))
    from terra import config
    config.get_settings.cache_clear() if hasattr(config.get_settings, "cache_clear") else None
    from terra.app import app
    return TestClient(app, base_url="http://127.0.0.1")


SAME = {"sec-fetch-site": "same-origin"}


def test_watch_routes_are_operator_only_and_validated(client):
    assert client.get("/api/watches", headers={"sec-fetch-site": "cross-site", "origin": "https://example.com"}).status_code == 403
    assert client.get("/api/watches", headers=SAME).status_code == 200
    assert client.post("/api/watches", json={"name": "Too big", "bbox": [0, 0, 5, 5]}, headers=SAME).status_code == 422
    assert client.post("/api/watches", json={"name": "Bad id", "bbox": [0, 0, 1, 1], "investigation_id": "../x"}, headers=SAME).status_code == 422


def test_a_closed_proposal_cannot_be_approved(client):
    from terra.config import get_settings
    from terra.watch import save_watch
    save_watch(get_settings().database, {"id": "watch-x", "name": "X", "bbox": [0, 0, 1, 1], "status": "watching", "proposal": None,
                                         "updated_at": "2026-11-15T00:00:00+00:00"})
    r = client.post("/api/watches/watch-x/decide", json={"action": "approve", "proposal": "inv-1"}, headers=SAME)
    assert r.status_code == 409
    assert client.post("/api/watches/nope/check", headers=SAME).status_code == 404


# ------------------------------------------------------------------ the durable loop, end to end on a local Temporal
from temporalio import workflow  # noqa: E402

CALLS: list[str] = []


@workflow.defn(name="InvestigationWorkflow")
class FakeInvestigation:
    """Stands in for the science: records which acquisitions the watch asked to compare."""
    @workflow.run
    async def run(self, request: dict) -> dict:
        CALLS.append("investigate:" + ",".join(request["observation_ids"]))
        return {"status": "completed"}

def _dev_server() -> str | None:
    root = Path(__file__).resolve().parents[1] / ".data" / "temporal-bin"
    found = sorted(root.glob("temporal*")) if root.exists() else []
    return str(found[0]) if found else None


@pytest.mark.asyncio
@pytest.mark.skipif(_dev_server() is None, reason="no local Temporal dev server binary (.data/temporal-bin)")
async def test_watch_loop_check_investigate_propose_approve_dispatch():
    from temporalio import activity
    from temporalio.worker import UnsandboxedWorkflowRunner
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from terra.watch_workflows import AreaWatchWorkflow

    saved: list[dict] = []
    new = {**REF, "id": "new", "date": "2026-11-08T17:55:02Z"}

    @activity.defn(name="watch_save")
    async def save(state: dict) -> None: saved.append({k: state.get(k) for k in ("status", "proposal", "dispatched", "reference")})
    @activity.defn(name="watch_reference")
    async def reference(_: dict) -> dict: return REF
    @activity.defn(name="watch_next_pass")
    async def next_pass(_: dict) -> dict: return {"at": "2026-11-20T04:08:00+00:00"}
    @activity.defn(name="watch_find_new")
    async def find_new(p: dict) -> dict | None: calls.append("find"); return new if p["reference"]["id"] == "ref" else None
    @activity.defn(name="watch_register_investigation")
    async def register(p: dict) -> dict: calls.append("register"); return {"id": "inv-1", "observation_ids": [p["reference"]["id"], p["new"]["id"]]}
    @activity.defn(name="watch_assess")
    async def assess(_: dict) -> dict: return {"significant": True, "summary": "1.2% changed", "patches": 6}
    @activity.defn(name="watch_dispatch")
    async def dispatch(p: dict) -> dict: calls.append("dispatch:" + p["investigation_id"]); return {"targets": 6, "coordinator": True}

    CALLS.clear(); calls = CALLS
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_dev_server(), ui=False) as env:
        queue = "watch-test-" + uuid.uuid4().hex[:6]
        async with Worker(env.client, task_queue=queue, workflows=[AreaWatchWorkflow, FakeInvestigation], workflow_runner=UnsandboxedWorkflowRunner(),
                          activities=[save, reference, next_pass, find_new, register, assess, dispatch]):
            handle = await env.client.start_workflow(AreaWatchWorkflow.run, {"id": "watch-etna", "name": "Mount Etna", "bbox": [14.95, 37.7, 15.1, 37.8]},
                                                     id="watch-etna-" + uuid.uuid4().hex[:6], task_queue=queue)
            async def until(pred, what):
                for _ in range(200):
                    state = await handle.query(AreaWatchWorkflow.snapshot)
                    if pred(state):
                        return state
                    await asyncio.sleep(0.05)
                raise AssertionError(f"timed out waiting for {what}: {state.get('status')}")
            await until(lambda s: s.get("status") == "waiting_pass", "the predicted pass")
            await handle.signal(AreaWatchWorkflow.check_now)  # don't wait for 2026-11-20
            state = await until(lambda s: s.get("status") == "proposal", "a proposal")
            assert state["proposal"]["id"] == "inv-1" and state["reference"]["id"] == "new"
            assert "dispatch:inv-1" not in calls  # never dispatches without the operator
            await handle.signal(AreaWatchWorkflow.decide, {"action": "approve", "proposal": "inv-1"})
            state = await until(lambda s: s.get("dispatched"), "the dispatch")
            assert calls[:3] == ["find", "register", "investigate:ref,new"] and "dispatch:inv-1" in calls
            await until(lambda s: s.get("status") == "waiting_pass", "the next pass")
            await handle.signal(AreaWatchWorkflow.stop)
            final = await handle.result()
            assert final["status"] == "stopped" and final["cycles"] == 1
            assert [s["status"] for s in saved][:3] == ["watching", "waiting_pass", "checking"]
