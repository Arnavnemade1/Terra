"""HTTP surface of area watches. Every route is an operator route (same check as fleet controls): approving a
proposal dispatches aircraft, and a watch starts investigations on its own schedule."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import re
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .config import get_settings
from .fleet.api import operator
from .watch import get_watch, list_watches, save_watch

log = logging.getLogger("terra.watch")
router = APIRouter(prefix="/api/watches", tags=["watches"], dependencies=[Depends(operator)])
_client: dict = {"value": None}


async def _temporal():
    from temporalio.client import Client
    settings = get_settings()
    if _client["value"] is None:
        try:
            _client["value"] = await asyncio.wait_for(Client.connect(settings.temporal_address, namespace=settings.temporal_namespace), timeout=3)
        except Exception as exc:
            raise HTTPException(503, "The durable service (Temporal) is offline; start it and the worker, then retry.") from exc
    return _client["value"]


class WatchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    bbox: list[float] = Field(min_length=4, max_length=4)
    investigation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")
    min_changed_pct: float = Field(default=0.5, ge=0.01, le=50)

    @field_validator("bbox")
    @classmethod
    def area(cls, v: list[float]) -> list[float]:
        w, s, e, n = v
        if not (-180 <= w < e <= 180 and -90 <= s < n <= 90 and e - w <= 2 and n - s <= 2):
            raise ValueError("Watch an area up to 2 degrees across.")
        return v


class Decision(BaseModel):
    action: Literal["approve", "dismiss"]
    proposal: str = Field(min_length=1, max_length=80)


def _id(body: WatchRequest) -> str:
    if body.investigation_id:
        return f"watch-{body.investigation_id}"
    return "watch-" + re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:40] + "-" + uuid4().hex[:6]


@router.get("")
async def watches():
    return {"watches": list_watches(get_settings().database)}


@router.post("", status_code=201)
async def start_watch(body: WatchRequest):
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    identifier = _id(body)
    existing = get_watch(get_settings().database, identifier)
    if existing and existing.get("status") not in ("stopped", "failed"):
        return existing
    client = await _temporal()
    now = datetime.now(timezone.utc).isoformat()
    state = {"id": identifier, "name": body.name, "bbox": body.bbox, "investigation_id": body.investigation_id, "min_changed_pct": body.min_changed_pct,
             "status": "starting", "message": "Starting the watch.", "created_at": now, "updated_at": now, "history": []}
    save_watch(get_settings().database, state)
    await client.start_workflow("AreaWatchWorkflow", {"id": identifier, "name": body.name, "bbox": body.bbox, "investigation_id": body.investigation_id,
                                                      "min_changed_pct": body.min_changed_pct},
                                id=identifier, task_queue=get_settings().task_queue,
                                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
    return state


async def _signal(identifier: str, name: str, arg=None):
    if not get_watch(get_settings().database, identifier):
        raise HTTPException(404, "Unknown watch.")
    client = await _temporal()
    try:
        handle = client.get_workflow_handle(identifier)
        await (handle.signal(name, arg) if arg is not None else handle.signal(name))
    except Exception as exc:
        log.warning("watch signal %s failed: %s", name, exc.__class__.__name__)
        raise HTTPException(409, "The watch is not running.") from exc
    return get_watch(get_settings().database, identifier)


@router.post("/{identifier}/check")
async def check_now(identifier: str):
    return await _signal(identifier, "check_now")


@router.post("/{identifier}/decide")
async def decide(identifier: str, body: Decision):
    watch = get_watch(get_settings().database, identifier) or {}
    if (watch.get("proposal") or {}).get("id") != body.proposal:
        raise HTTPException(409, "That proposal is no longer open.")
    return await _signal(identifier, "decide", body.model_dump())


@router.post("/{identifier}/stop")
async def stop(identifier: str):
    return await _signal(identifier, "stop")
