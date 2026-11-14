"""Verify publication precedes optional work without timing-sensitive sleeps."""
import asyncio
from datetime import datetime, timezone

import pytest

from terra.workflows import InvestigationWorkflow


@pytest.mark.asyncio
async def test_computed_result_published_before_enrichment_finishes(monkeypatch):
    from terra import workflows
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(workflows.workflow, "now", lambda: now)
    monkeypatch.setattr(workflows.workflow, "patched", lambda _id: True)
    instance = InvestigationWorkflow()
    instance.started_at = now
    instance.state = {"timings": {}}
    enrichment_started = asyncio.Event()
    release_enrichment = asyncio.Event()
    published = []
    weather = {"status": "available", "acquisitions": [{"date": "2026-10-01", "rain_72h_mm": 12.5}]}

    async def step(name, payload, timeout=120):
        if name == "publish_science":
            snapshot = {"narrative_status": payload["narrative_status"], "enrichment_status": payload["enrichment_status"]}
            if "surface_conditions" in payload:
                snapshot["surface_conditions"] = payload["surface_conditions"]
            published.append(snapshot)
            return snapshot
        if name == "narrate_findings":
            return {"narrative_status": "ready", "narrative": {"answer": "Measured evidence"}, "surface_conditions": weather}
        if name == "enrich_science":
            enrichment_started.set()
            await release_enrichment.wait()
            return {"enrichment_status": "ready", "enrichment": {"optical_evidence": []}}
        raise AssertionError(name)

    checkpoints = []

    async def checkpoint(*args):
        checkpoints.append(args[:2])

    instance.step = step
    instance.checkpoint = checkpoint
    task = asyncio.create_task(instance.publish_incrementally({"id": "test"}, {}))
    await asyncio.wait_for(enrichment_started.wait(), 1)
    assert published[0] == {"narrative_status": "pending", "enrichment_status": "pending", "timings": {"first_result_seconds": 0.0}}
    assert instance.state["answer_ready"] is True
    for _ in range(20):  # let the narrator finish while enrichment is still held
        await asyncio.sleep(0)
    # Complete as soon as the answer is written; optical imagery is still attaching.
    assert ("complete", "completed") in checkpoints
    assert not task.done()
    release_enrichment.set()
    await task
    assert published[-1]["narrative_status"] == "ready"
    assert published[-1]["enrichment_status"] == "ready"
    assert published[-1]["surface_conditions"] == weather


@pytest.mark.asyncio
async def test_optional_failure_keeps_computed_result(monkeypatch):
    from terra import workflows
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(workflows.workflow, "now", lambda: now)
    monkeypatch.setattr(workflows.workflow, "patched", lambda _id: True)
    instance = InvestigationWorkflow()
    instance.started_at = now
    instance.state = {"timings": {}}
    async def step(name, payload, timeout=120):
        if name == "publish_science":
            return {"measurements": {"valid_pixels": 4}, "enrichment_status": payload["enrichment_status"]}
        raise RuntimeError("optional provider unavailable")
    async def checkpoint(*args):
        pass
    instance.step, instance.checkpoint = step, checkpoint
    await instance.publish_incrementally({"id": "test"}, {})
    assert instance.state["result"]["measurements"]["valid_pixels"] == 4
    assert instance.state["result"]["enrichment_status"] == "unavailable"
