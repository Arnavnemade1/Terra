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
    if status != "running":
        # A continued-as-new workflow reuses its ID. Live heartbeat/task data belongs to its new run.
        model = {"label": model.get("label")}
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
            "temporal": {"connected": connected, "namespace": settings.temporal_namespace, "queue": settings.task_queue, "worker_available": available,
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


# Temporal's public visibility surface: typed filters, bounded pagination and payload-free history.
import base64
from collections import OrderedDict
from fastapi import HTTPException, Path, Query
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.workflowservice.v1 import GetWorkflowExecutionHistoryReverseRequest

STATUS_VALUES = {'Running', 'Completed', 'Failed', 'Canceled', 'Terminated', 'ContinuedAsNew', 'TimedOut'}
_history_cache: OrderedDict = OrderedDict()
_history_slots = asyncio.Semaphore(2)


def visibility_query(status: str = '', workflow_type: str = '', identifier: str = '') -> str:
    quote = lambda value: value.replace("'", "''")
    clauses = [f"TaskQueue = '{quote(get_settings().task_queue)}'", "WorkflowType IN (" + ', '.join(f"'{name}'" for name in KINDS) + ')']
    if status:
        if status not in STATUS_VALUES:
            raise HTTPException(422, 'Unknown execution status.')
        clauses.append(f"ExecutionStatus = '{status}'")
    if workflow_type:
        if workflow_type not in KINDS:
            raise HTTPException(422, 'Unknown workflow type.')
        clauses.append(f"WorkflowType = '{workflow_type}'")
    if identifier:
        clauses.append(f"WorkflowId STARTS_WITH '{quote(identifier)}'")
    return ' AND '.join(clauses)


async def status_counts(client, query: str | None = None) -> dict | None:
    try:
        result = await client.count_workflows((query or visibility_query()) + ' GROUP BY ExecutionStatus', rpc_timeout=timedelta(seconds=2))
        return {str(g.group_values[0]): g.count for g in result.groups if g.group_values}
    except Exception:
        return None


@router.get('/executions')
async def execution_page(status: str = '', workflow_type: str = '', identifier: str = Query('', max_length=200),
                         page_size: int = Query(20, ge=1, le=100), page_token: str = Query('', max_length=8192)):
    from .app import temporal_client
    query = visibility_query(status, workflow_type, identifier)
    try:
        token = base64.b64decode(page_token, altchars=b'-_', validate=True) if page_token else None
    except (ValueError, TypeError):
        raise HTTPException(422, 'Invalid page token.') from None
    try:
        client = await temporal_client()
        iterator = client.list_workflows(query, page_size=page_size, next_page_token=token, rpc_timeout=timedelta(seconds=2))
        async def page():
            await iterator.fetch_next_page()
            return iterator.current_page or []
        executions, counts, models = await asyncio.wait_for(asyncio.gather(page(), status_counts(client, query), asyncio.to_thread(read_models)), 4)
        available = bool(monitor.cached and monitor.cached['temporal']['worker_available'])
        rows = [workflow_view(e, models.get(e.id, {}), True, available, time.time()) for e in executions if e.workflow_type in KINDS]
        return {'executions': rows, 'next_page_token': base64.urlsafe_b64encode(iterator.next_page_token).decode() if iterator.next_page_token else None,
                'counts': counts, 'namespace': client.namespace, 'queue': get_settings().task_queue}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Temporal execution visibility is temporarily unavailable. Retry shortly.') from None


def history_event(event) -> dict:
    """Allowlist metadata only. Never serialize protobuf input, result, failure, memo or signal payloads."""
    name = EventType.Name(event.event_type).removeprefix('EVENT_TYPE_')
    view = {'id': event.event_id, 'type': ''.join(word.title() for word in name.split('_')),
            'at': event.event_time.ToDatetime(tzinfo=timezone.utc).isoformat()}
    attributes = event.WhichOneof('attributes')
    if attributes:
        data = getattr(event, attributes)
        for field in ('activity_id', 'scheduled_event_id', 'started_event_id', 'initiated_event_id', 'timer_id', 'attempt'):
            if field in data.DESCRIPTOR.fields_by_name:
                value = getattr(data, field)
                if value:
                    view[field] = value
        if 'activity_type' in data.DESCRIPTOR.fields_by_name:
            view['activity_type'] = data.activity_type.name
        if 'workflow_type' in data.DESCRIPTOR.fields_by_name:
            view['workflow_type'] = data.workflow_type.name
        if 'task_queue' in data.DESCRIPTOR.fields_by_name:
            view['task_queue'] = data.task_queue.name
    return view


@router.get('/{identifier}/history')
async def execution_history(identifier: str = Path(..., min_length=1, max_length=200, pattern=r'^[A-Za-z0-9_.:-]+$'),
                            run_id: str = Query(..., min_length=1, max_length=64, pattern=r'^[A-Za-z0-9-]+$')):
    from .app import temporal_client
    key = (get_settings().temporal_namespace, get_settings().task_queue, identifier, run_id)
    cached = _history_cache.get(key)
    if cached and time.monotonic() - cached[0] < 3:
        return cached[1]
    async with _history_slots:
        try:
            client = await temporal_client()
            description = await client.get_workflow_handle(identifier, run_id=run_id).describe(rpc_timeout=timedelta(seconds=2))
            raw = description.raw_description
            if description.workflow_type not in KINDS or raw.execution_config.task_queue.name != get_settings().task_queue:
                raise HTTPException(404, 'This execution is not in the Terra task queue.')
            response = await client.workflow_service.get_workflow_execution_history_reverse(GetWorkflowExecutionHistoryReverseRequest(
                namespace=client.namespace, execution=WorkflowExecution(workflow_id=identifier, run_id=run_id), maximum_page_size=200), timeout=timedelta(seconds=2))
            events = sorted((history_event(e) for e in response.history.events), key=lambda e: e['id'])[-200:]
            models = await asyncio.to_thread(read_models)
            available = bool(monitor.cached and monitor.cached['temporal']['worker_available'])
            execution = workflow_view(description, models.get(identifier, {}), True, available, time.time())
            await pending_work(client, execution, asyncio.Semaphore(1))
            children = [{'id': c.workflow_id, 'run_id': c.run_id, 'workflow_type': c.workflow_type_name} for c in raw.pending_children if c.workflow_type_name in KINDS]
            info = raw.workflow_execution_info
            parent = {'id': info.parent_execution.workflow_id, 'run_id': info.parent_execution.run_id} if info.HasField('parent_execution') else None
            value = {'execution': execution, 'events': events, 'limited': bool(response.next_page_token) or len(response.history.events) > 200, 'namespace': client.namespace,
                     'queue': get_settings().task_queue, 'parent': parent, 'children': children, 'source': 'temporal'}
            _history_cache[key] = (time.monotonic(), value)
            _history_cache.move_to_end(key)
            while len(_history_cache) > 16:
                _history_cache.popitem(last=False)
            return value
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, 'This execution history could not be loaded. Retry shortly.') from None
