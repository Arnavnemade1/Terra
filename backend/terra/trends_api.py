"""HTTP surface of trend analyses: start one, read its snapshot and result, fetch the two layer files.

A trend analysis reads original GCOV products and runs many tile agents, so the POST starts a durable workflow
and returns its identifier. The GET routes read the workflow snapshot and, once publishing finished, the
result document written by the publish activity.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .catalog import validate_bbox
from .config import get_settings

router = APIRouter(prefix="/api/trends", tags=["trends"])
IDENTIFIER = re.compile(r"^trend-[0-9a-f]{12}$")
LAYER = re.compile(r"^trend(-significant)?\.(png|tif)$")
MEDIA = {".png": "image/png", ".tif": "image/tiff"}
MAX_SPAN_DEGREES = 0.5
_client: dict = {"value": None}


async def _temporal():
    """The Temporal client, connected the same way the watch routes connect theirs."""
    from temporalio.client import Client
    settings = get_settings()
    if _client["value"] is None:
        try:
            _client["value"] = await asyncio.wait_for(
                Client.connect(settings.temporal_address, namespace=settings.temporal_namespace), timeout=3)
        except Exception as exc:
            raise HTTPException(503, "The durable service (Temporal) is offline; start it and the worker, then retry.") from exc
    return _client["value"]


class TrendRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    bbox: list[float] = Field(min_length=4, max_length=4)
    max_dates: int = Field(default=10, ge=4, le=16)

    @field_validator("bbox")
    @classmethod
    def area(cls, value: list[float]) -> list[float]:
        bbox = validate_bbox(value)
        west, south, east, north = bbox
        if not (west < east and east - west <= MAX_SPAN_DEGREES and north - south <= MAX_SPAN_DEGREES):
            raise ValueError("A trend analysis covers a WGS84 area up to 0.5 degrees across.")
        return bbox


def _identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(str(value)):
        raise HTTPException(422, "Invalid trend analysis identifier.")
    return str(value)


def _analysis(identifier: str) -> Path:
    return get_settings().data_dir / "trends" / identifier


async def _snapshot(identifier: str) -> dict:
    client = await _temporal()
    try:
        return await client.get_workflow_handle(identifier).query("snapshot")
    except Exception as exc:
        raise HTTPException(404, "Unknown trend analysis.") from exc


@router.post("")
async def start_trend(body: TrendRequest):
    from uuid import uuid4
    identifier = "trend-" + uuid4().hex[:12]
    client = await _temporal()
    await client.start_workflow(
        "TrendWorkflow",
        {"id": identifier, "bbox": body.bbox, "name": body.name, "max_dates": body.max_dates},
        id=identifier, task_queue=get_settings().task_queue,
    )
    return {"id": identifier}


@router.get("/{identifier}")
async def trend_status(identifier: str):
    identifier = _identifier(identifier)
    directory = _analysis(identifier)
    result = None
    result_path = directory / "result.json"
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
        except (OSError, ValueError):
            result = None
    try:
        state = await _snapshot(identifier)
    except HTTPException as exc:
        if result is not None:
            return {"id": identifier, "status": "done", "result": result}
        if directory.is_dir():
            raise exc
        raise HTTPException(404, "Unknown trend analysis.") from exc
    return {**state, "id": identifier, "result": result}


@router.get("/{identifier}/files/{name}")
async def trend_layer(identifier: str, name: str):
    identifier = _identifier(identifier)
    if not LAYER.fullmatch(name):
        raise HTTPException(404, "Unknown trend layer.")
    directory = _analysis(identifier)
    candidate = directory / name
    if not candidate.is_file() or not candidate.resolve().is_relative_to(directory.resolve()):
        raise HTTPException(404, "That layer has not been written for this analysis.")
    return FileResponse(candidate, media_type=MEDIA[Path(name).suffix.lower()], filename=name)
