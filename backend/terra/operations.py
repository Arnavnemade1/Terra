"""Bounded, read-only Temporal visibility. Never returns workflow inputs or failure payloads."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import time

from fastapi import APIRouter
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

from .config import get_settings
from .runtime_health import worker_ready

router = APIRouter(prefix="/api/agents", tags=["agents"])
KINDS = {"InvestigationWorkflow": "investigation", "AreaWatchWorkflow": "watch",
         "FleetCoordinatorWorkflow": "coordinator", "DroneAgentWorkflow": "aircraft"}
ACTIVITIES = {
    "plan_investigation": "Planning and archive search", "discover_inputs": "Source discovery",
    "prepare_inputs": "Reading original radar products", "compute_science": "Radar computation",
    "verify_science": "Evidence verification", "publish_science": "Publishing evidence",
    "narrate_findings": "Writing the interpretation", "enrich_science": "Optical imagery and context",
    "coordination_round": "Fleet coordination", "drone_agent_round": "Aircraft agent round",
    "watch_next_pass": "Predicting the next pass", "watch_find_new": "Checking for new acquisitions",
    "watch_reference": "Selecting a baseline", "watch_save": "Saving watch state",
    "watch_assess": "Assessing measured change", "watch_dispatch": "Preparing approved dispatch",
}


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _age(value, now: float) -> float | None:
    if value is None:
        return None
    try:
        stamp = value.timestamp() if isinstance(value, datetime) else datetime.fromisoformat(str(value)).timestamp()
        return round(max(0, now - stamp), 1)
    except (ValueError, TypeError):
        return None


def read_models() -> dict[str, dict]:
    """Only public read-model fields are used; no aircraft tokens or raw results."""
    from .store import Store
    from .fleet.api import fleet_db
    from .watch import list_watches
    settings = get_settings()
    records: dict[str, dict] = {}
    for item in Store(settings.database).investigations(200):
        records[item["id"]] = {"label": item.get("title") or "Radar investigation", "stage": item.get("stage"),
            "model_status": item.get("status"), "progress": item.get("progress"), "updated_at": item.get("updated_at"),
            "events": [{k: e.get(k) for k in ("stage", "status", "at")} for e in item.get("events", [])[-8:]]}
    for item in list_watches(settings.database):
        records[item["id"]] = {"label": item.get("name") or "Area watch", "stage": item.get("status"),
            "model_status": item.get("status"), "updated_at": item.get("updated_at"),
            "next_at": item.get("next_pass_at"), "events": [{k: e.get(k) for k in ("status", "at")} for e in item.get("history", [])[-8:]]}
    for item in fleet_db().agents():
        summary = item.get("summary") or {}
        records[item["workflow_id"]] = {"label": "Fleet coordinator" if item["kind"] == "coordinator" else item["id"].replace("agent:", ""),
            "model_status": item["status"], "heartbeat_age_s": item["age_s"], "next_s": summary.get("next_s"),
            "round": summary.get("round"), "stage": item["status"], "events": [],
            "task": summary.get("task"), "eligible_jobs": summary.get("feasible_bids"), "offered_jobs": summary.get("bids")}
    return records


def workflow_view(execution, model: dict, connected: bool, worker_available: bool, now: float) -> dict:
    status = execution.status.name.lower() if execution.status else "unknown"
    kind = KINDS[execution.workflow_type]
    stage = model.get("stage") or ("starting" if status == "running" else status)
    heartbeat_age = model.get("heartbeat_age_s")
    stale_after = max(25, (model.get("next_s") or 0) * 3 + 5)
    if status == "running":
        if not connected or not worker_available:
            availability = "unavailable"
        elif heartbeat_age is not None and heartbeat_age > stale_after:
            availability = "stale"
        elif kind == "aircraft" and model.get("model_status") in ("offline", "lost"):
            availability = "unavailable"
        elif stage in ("blocked", "waiting_approval", "proposal") or model.get("model_status") == "blocked":
            availability = "blocked"
        elif kind == "aircraft" and stage == "online" and "task" in model and not model["task"]:
            availability = "ready"
        elif stage.startswith("waiting") or stage in ("watching", "idle", "standby"):
            availability = "waiting"
        else:
            availability = "working"
    else:
        availability = "finished" if status in ("completed", "continued_as_new", "canceled", "cancelled", "terminated") else "failed"
    return {"id": execution.id, "run_id": execution.run_id, "kind": kind, "workflow_type": execution.workflow_type,
            "label": model.get("label") or ("Fleet coordinator" if kind == "coordinator" else execution.id),
            "status": status, "availability": availability, "stage": stage, "progress": model.get("progress"),
            "started_at": _iso(execution.start_time), "closed_at": _iso(execution.close_time),
            "age_s": _age(execution.start_time, now), "heartbeat_age_s": heartbeat_age,
            "updated_at": model.get("updated_at"), "next_at": model.get("next_at"), "next_s": model.get("next_s"),
            "round": model.get("round"), "task": model.get("task"), "eligible_jobs": model.get("eligible_jobs"), "offered_jobs": model.get("offered_jobs"), "events": model.get("events", []), "activities": [], "details_live": False}


async def queue_pollers(client, queue_type: int) -> int | None:
    try:
        response = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
            namespace=client.namespace, task_queue=TaskQueue(name=get_settings().task_queue), task_queue_type=queue_type),
            timeout=timedelta(seconds=2))
        now = time.time()
        # Temporal retains old pollers; only recent polls indicate availability.
        return sum(0 <= now - p.last_access_time.ToDatetime(tzinfo=timezone.utc).timestamp() < 60 for p in response.pollers)
    except Exception:
        return None


async def pending_work(client, row: dict, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        try:
            description = await client.get_workflow_handle(row["id"], run_id=row["run_id"]).describe(rpc_timeout=timedelta(seconds=1))
            pending = description.raw_description.pending_activities
            row["activities"] = [{"name": a.activity_type.name, "label": ACTIVITIES.get(a.activity_type.name, a.activity_type.name),
                "state": "running" if a.state == 2 else "queued", "attempt": a.attempt,
                "heartbeat_age_s": round(max(0, time.time() - a.last_heartbeat_time.ToDatetime(tzinfo=timezone.utc).timestamp()), 1) if a.HasField("last_heartbeat_time") else None}
                for a in pending[:12]]
            row["details_live"] = True
            if row["activities"] and row["availability"] == "waiting":
                row["availability"] = "working"
        except Exception:
            pass  # Listing remains useful during a continue-as-new race or a slow describe.


class AgentMonitor:
    def __init__(self):
        self.cached: dict | None = None
        self.at = 0.0
        self.pending: asyncio.Task | None = None

    async def snapshot(self) -> dict:
        if self.cached is not None and time.monotonic() - self.at < 4:
            return self.cached
        if self.pending is None or self.pending.done():
            self.pending = asyncio.create_task(self.refresh())
        # One disconnected browser must not cancel the shared visibility request.
        return await asyncio.shield(self.pending)

    async def refresh(self) -> dict:
        from .app import temporal_client
        settings = get_settings()
        models = await asyncio.to_thread(read_models)
        now = time.time()
        connected = False
        workflow_pollers = activity_pollers = None
        executions = []
        limited = False
        try:
            client = await temporal_client()
            queue = settings.task_queue.replace("'", "''")
            async def listing(active: bool, limit: int):
                condition = "= 'Running'" if active else "!= 'Running'"
                return [e async for e in client.list_workflows(f"TaskQueue = '{queue}' AND ExecutionStatus {condition}",
                    limit=limit, page_size=limit, rpc_timeout=timedelta(seconds=2)) if e.workflow_type in KINDS]
            active, recent, workflow_pollers, activity_pollers = await asyncio.wait_for(asyncio.gather(
                listing(True, 200), listing(False, 30), queue_pollers(client, TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
                queue_pollers(client, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)), timeout=3)
            executions = active + recent
            limited = len(active) >= 200
            connected = True
        except Exception:
            client = None
        heartbeat = await asyncio.to_thread(worker_ready, settings.data_dir)
        available = bool(connected and workflow_pollers and activity_pollers)
        rows = [workflow_view(e, models.get(e.id, {}), connected, available, now) for e in executions]
        if client is not None:
            semaphore = asyncio.Semaphore(4)
            pending_rows = [r for r in rows if r["status"] == "running"][:24]
            tasks = [asyncio.create_task(pending_work(client, r, semaphore)) for r in pending_rows]
            if tasks:
                _, unfinished = await asyncio.wait(tasks, timeout=1.5)
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        # Keep the last known registry visible on outages, explicitly marked unavailable.
        if not connected and self.cached:
            rows = [{**r, "availability": "unavailable" if r["status"] == "running" else r["availability"],
                     "details_live": False} for r in self.cached["agents"]]
        rows.sort(key=lambda r: (r["status"] != "running", {"coordinator": 0, "aircraft": 1, "watch": 2, "investigation": 3}[r["kind"]], r["id"]))
        value = {"observed_at": self.cached["observed_at"] if not connected and self.cached else datetime.now(timezone.utc).isoformat(), "refresh_seconds": 5,
            "temporal": {"connected": connected, "queue": settings.task_queue, "worker_available": available,
                         "workflow_pollers": workflow_pollers, "activity_pollers": activity_pollers, "worker_heartbeat": heartbeat},
            "agents": rows, "limited": limited, "stale": not connected,
            "summary": {"total": len(rows), "running": sum(r["status"] == "running" for r in rows),
                "working": sum(r["availability"] == "working" for r in rows), "ready": sum(r["availability"] == "ready" for r in rows), "waiting": sum(r["availability"] == "waiting" for r in rows),
                "attention": sum(r["availability"] in ("blocked", "failed", "stale", "unavailable") for r in rows)}}
        self.cached, self.at = value, time.monotonic()
        return value


monitor = AgentMonitor()


@router.get("")
async def agents():
    return await monitor.snapshot()
