"""Area watch: NISAR swath prediction, comparable acquisitions, significance, the API, and the durable loop."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from terra.watch import assess_change, comparable, distance_km, next_nisar_pass, pick_new_acquisition, pick_pair, sub_point

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


def test_pass_in_the_baselines_direction_matches_the_archive():
    """Etna's acquisitions on track 51 (descending) start at 17:55:02 UTC every 12 days (2026-10-15, 10-27, ...). The
    next descending pass predicted from the orbit must land on that repeat: 2026-11-20, within a couple of minutes.
    The undirected prediction (04:08, ascending) could never be compared with a descending baseline."""
    start = datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc)
    down = next_nisar_pass(L1, L2, 37.75, 15.025, start, direction="DESCENDING")
    repeat = datetime(2026, 10, 15, 17, 55, 2, tzinfo=timezone.utc) + 3 * (datetime(2026, 10, 27, tzinfo=timezone.utc) - datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert down is not None and abs((down - repeat).total_seconds()) < 120
    up = next_nisar_pass(L1, L2, 37.75, 15.025, start, direction="ascending")
    assert up is not None and up.strftime("%Y-%m-%d %H:%M") == "2026-11-20 04:08"


def test_next_pass_found_for_any_land_area_within_13_days():
    start = datetime(2026, 11, 15, tzinfo=timezone.utc)
    for lat, lon in [(19.4, -155.25), (-4.5, -74.5), (64.0, -22.4), (35.4, 138.7)]:
        at = next_nisar_pass(L1, L2, lat, lon, start)
        assert at is not None and (at - start).days <= 13


REF = {"id": "ref", "product": "GCOV", "date": "2026-10-15T17:55:02Z", "track": 51, "frame": 69, "direction": "DESCENDING", "maturity": "PROVISIONAL", "product_version": "P05023"}


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


def test_pairs_respect_the_processing_release():
    """Etna, live: the 2026-10-27 product is VALIDATED (P05036) while the baseline is PROVISIONAL (P05023); the
    science refuses that pair, so the watch must not propose it, and must say why."""
    validated = {**REF, "id": "oct27", "date": "2026-10-27T17:55:02Z", "maturity": "VALIDATED", "product_version": "P05036"}
    partner, new, reason = pick_pair([REF, validated], REF)
    assert new is None and "P05036" in reason and "P05023" in reason
    # Once an earlier acquisition from the same release exists after the baseline, the pair is that one and the new one.
    reprocessed = {**validated, "id": "oct15-validated", "date": "2026-10-15T17:55:03Z"}
    partner, new, _ = pick_pair([REF, validated, reprocessed], REF)
    assert (partner["id"], new["id"]) == ("oct15-validated", "oct27")
    # A same-release newer pass pairs with the baseline itself.
    nov8 = {**REF, "id": "nov8", "date": "2026-11-08T17:55:02Z"}
    assert [x["id"] for x in pick_pair([REF, validated, nov8], REF)[:2]] == ["ref", "nov8"]
    # Pairs that failed before are skipped.
    assert pick_pair([REF, nov8], REF, {"nov8"})[1] is None


def test_comparable_is_what_the_science_accepts():
    dual = {**REF, "polarizations": ["HH", "HV"], "frequency": "A"}
    assert comparable(dual, {**dual, "id": "b", "maturity": "provisional"})              # case is not a difference
    assert comparable(dual, {**dual, "id": "b", "polarizations": ["HH"]})                 # a shared co-pol channel is enough
    assert not comparable(dual, {**dual, "id": "b", "polarizations": ["VV", "VH"]})       # nothing in common
    assert not comparable(dual, {**dual, "id": "b", "frequency": "B"})
    assert not comparable(dual, {**dual, "id": "b", "frame": 70})
    assert comparable(dual, {**dual, "id": "b", "frame": None})                           # frame unknown: the science checks the grid
    assert not comparable(dual, {**dual, "id": "b", "product_version": None})


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


def _activities(calls: list, saved: list, assessment: dict):
    from temporalio import activity
    new = {**REF, "id": "new", "date": "2026-11-08T17:55:02Z"}

    @activity.defn(name="watch_save")
    async def save(state: dict) -> None: saved.append({k: state.get(k) for k in ("status", "message", "proposal", "dispatched", "reference", "skipped")})
    @activity.defn(name="watch_reference")
    async def reference(_: dict) -> dict: return REF
    @activity.defn(name="watch_next_pass")
    async def next_pass(p: dict) -> dict: calls.append(f"pass:{p.get('direction')}"); return {"at": "2026-11-20T17:55:00+00:00", "direction": p.get("direction")}
    @activity.defn(name="watch_find_new")
    async def find_new(p: dict) -> dict:
        calls.append("find")
        if p["reference"]["id"] == "ref" and "new" not in (p.get("skip") or []):
            return {"partner": REF, "new": new, "reason": ""}
        return {"partner": None, "new": None, "reason": "No newer acquisition with the same viewing geometry yet."}
    @activity.defn(name="watch_register_investigation")
    async def register(p: dict) -> dict: calls.append("register"); return {"id": "inv-1", "observation_ids": [p["reference"]["id"], p["new"]["id"]]}
    @activity.defn(name="watch_assess")
    async def assess(_: dict) -> dict: return assessment
    @activity.defn(name="watch_dispatch")
    async def dispatch(p: dict) -> dict: calls.append("dispatch:" + p["investigation_id"]); return {"targets": 6, "coordinator": True}
    return [save, reference, next_pass, find_new, register, assess, dispatch]


async def _run_watch(assessment: dict, script):
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker
    from temporalio.testing import WorkflowEnvironment
    from terra.watch_workflows import AreaWatchWorkflow

    saved: list[dict] = []
    CALLS.clear()
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_dev_server(), ui=False) as env:
        queue = "watch-test-" + uuid.uuid4().hex[:6]
        async with Worker(env.client, task_queue=queue, workflows=[AreaWatchWorkflow, FakeInvestigation], workflow_runner=UnsandboxedWorkflowRunner(),
                          activities=_activities(CALLS, saved, assessment)):
            handle = await env.client.start_workflow(AreaWatchWorkflow.run, {"id": "watch-etna", "name": "Mount Etna", "bbox": [14.95, 37.7, 15.1, 37.8]},
                                                     id="watch-etna-" + uuid.uuid4().hex[:6], task_queue=queue)

            async def until(pred, what):
                state = {}
                for _ in range(200):
                    state = await handle.query(AreaWatchWorkflow.snapshot)
                    if pred(state):
                        return state
                    await asyncio.sleep(0.05)
                raise AssertionError(f"timed out waiting for {what}: {state.get('status')}")
            await script(handle, until, AreaWatchWorkflow)
            await handle.signal(AreaWatchWorkflow.stop)
            return await handle.result(), saved


@pytest.mark.asyncio
@pytest.mark.skipif(_dev_server() is None, reason="no local Temporal dev server binary (.data/temporal-bin)")
async def test_watch_loop_check_investigate_propose_approve_dispatch():
    async def script(handle, until, W):
        await until(lambda s: s.get("status") == "waiting_pass", "the predicted pass")
        await handle.signal(W.check_now)  # don't wait for 2026-11-20
        state = await until(lambda s: s.get("status") == "proposal", "a proposal")
        assert state["proposal"]["id"] == "inv-1" and state["reference"]["id"] == "new"
        assert not any(c.startswith("dispatch") for c in CALLS)  # never dispatches without the operator
        await handle.signal(W.decide, {"action": "approve", "proposal": "inv-1"})
        await until(lambda s: s.get("dispatched"), "the dispatch")
        await until(lambda s: s.get("status") == "waiting_pass", "the next pass")
    final, saved = await _run_watch({"significant": True, "status": "completed", "summary": "1.2% changed", "patches": 6}, script)
    assert CALLS[:4] == ["pass:DESCENDING", "find", "register", "investigate:ref,new"] and "dispatch:inv-1" in CALLS
    assert final["status"] == "stopped" and final["cycles"] == 1
    assert [s["status"] for s in saved][:3] == ["watching", "waiting_pass", "checking"]


@pytest.mark.asyncio
@pytest.mark.skipif(_dev_server() is None, reason="no local Temporal dev server binary (.data/temporal-bin)")
async def test_a_refused_comparison_keeps_the_baseline_and_is_not_retried():
    """Live, Etna: the science refused 2026-10-15 vs 10-27 (maturity differs) and the watch reported "no significant
    change" and moved its baseline. A failed comparison must say so, keep the baseline, and skip that pass next time."""
    async def script(handle, until, W):
        await until(lambda s: s.get("status") == "waiting_pass", "the predicted pass")
        await handle.signal(W.check_now)
        state = await until(lambda s: s.get("cycles") == 1 and s.get("status") == "waiting_pass", "the refused comparison")
        assert state["reference"]["id"] == "ref" and state["skipped"] == ["new"] and not state.get("proposal")
        await handle.signal(W.check_now)
        await until(lambda s: s.get("status") == "waiting_data", "the next search")
    final, saved = await _run_watch({"significant": False, "status": "failed", "summary": "Cannot compare acquisitions: maturity differs."}, script)
    said = [s["message"] for s in saved]
    assert any(m.startswith("Could not compare 2026-10-15 with 2026-11-08: Cannot compare acquisitions") for m in said)
    assert not any("No significant change" in m for m in said)
    assert CALLS.count("register") == 1 and not any(c.startswith("dispatch") for c in CALLS)
    assert final["reference"]["id"] == "ref"
