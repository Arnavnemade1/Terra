"""One HTTP boundary for the TERRA globe and scientific investigations."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4, uuid5, NAMESPACE_URL

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from .config import get_settings
from .store import Store, utc_now
from .activities import available_inputs, safe_job_directory
from .workflows import InvestigationWorkflow
from . import connector
from .gateway import router as connector_router
from .google_tiles import api_key as google_tiles_key, router as google_tiles_router
from .fleet.api import router as fleet_router

log = logging.getLogger(__name__)


def validate_bbox(values: list[float], bounded: bool = False) -> list[float]:
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("Use four finite coordinates: west,south,east,north.")
    west, south, east, north = values
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Coordinates must form a non-empty WGS84 bounding box. Split antimeridian-crossing regions into two investigations.")
    if bounded and (east - west > 30 or north - south > 30):
        raise ValueError("Choose a regional analysis area spanning at most 30 degrees in each direction.")
    return values


class InvestigationRequest(BaseModel):
    title: str = Field(default="Regional backscatter investigation", min_length=1, max_length=160)
    question: str = Field(default="Inspect calibrated radar backscatter and its limitations.", max_length=3000)
    bbox: list[float] = Field(min_length=4, max_length=4)
    observation_ids: list[str] = Field(default_factory=list, max_length=2)
    mode: Literal["backscatter", "change"] = "backscatter"
    auto: bool = False

    @field_validator("bbox")
    @classmethod
    def region_is_valid(cls, value: list[float]) -> list[float]:
        return validate_bbox(value, bounded=True)

    @field_validator("observation_ids")
    @classmethod
    def identifiers_are_valid(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not item or len(item) > 512 for item in value):
            raise ValueError("Select distinct catalog observations.")
        return value

    @model_validator(mode="after")
    def check_mode(self):
        if self.auto:
            if not self.question.strip():
                raise ValueError("Ask a question so TERRA can plan the investigation.")
            return self
        if self.mode == "change" and len(self.observation_ids) != 2:
            raise ValueError("Change analysis requires two compatible observations.")
        if self.mode == "backscatter" and len(self.observation_ids) != 1:
            raise ValueError("Backscatter inspection requires one observation.")
        return self


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_directories()
    app.state.store = Store(settings.database)
    app.state.temporal = None
    # Map tiles around the fleet are fetched ahead of time into the shared tile cache (see fleet/warm.py).
    from .fleet.warm import Warmer, enabled as warming_enabled
    app.state.warmer = Warmer()
    warming = asyncio.create_task(app.state.warmer.run()) if warming_enabled() else None
    try:
        yield
    finally:
        if warming:
            warming.cancel()
        await connector.close()


app = FastAPI(title="TERRA Investigation API", version="0.1.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.include_router(google_tiles_router)
app.include_router(connector_router)
app.include_router(fleet_router)
from .operations import router as operations_router  # noqa: E402
app.include_router(operations_router)
from .watch_api import router as watch_router  # noqa: E402  (after the fleet router: it reuses the operator check)
app.include_router(watch_router)
# *.localhost always resolves to this machine; the workspace spreads map tiles over a few such names so the browser
# opens more than six connections at once (it allows six per host over HTTP/1.1).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "*.localhost", "127.0.0.1", "[::1]", *filter(None, os.getenv("TERRA_ALLOWED_HOSTS", "").split(","))])
allowed_origins = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:3000"]
allowed_origins.extend(filter(None, os.getenv("TERRA_ALLOWED_ORIGINS", "").split(",")))
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Idempotency-Key"])


@app.middleware("http")
async def protect_mutations(request: Request, call_next):
    # No workspace files are ever part of the public API surface.
    if any(part.startswith(".") for part in request.url.path.split("/") if part):
        return JSONResponse({"detail": "Not found."}, status_code=404)
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin not in allowed_origins and origin != expected:
            return JSONResponse({"detail": "Origin is not allowed to start or control investigations."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def get_store() -> Store:
    if not hasattr(app.state, "store"):
        app.state.store = Store(get_settings().database)
    return app.state.store


async def temporal_client() -> Client:
    if getattr(app.state, "temporal", None) is not None:
        return app.state.temporal
    settings = get_settings()
    try:
        app.state.temporal = await asyncio.wait_for(
            Client.connect(settings.temporal_address, namespace=settings.temporal_namespace), timeout=2.5)
        return app.state.temporal
    except Exception as exc:
        raise HTTPException(503, "Durable investigation service is offline. Start the Temporal server and scientific worker, then retry.") from exc


def local_observations() -> list[dict]:
    """Register only inspected local products, never fixture/synthetic observations."""
    from .science import inspect_gcov
    observations = []
    for path in available_inputs():
        try:
            if path.suffix.lower() in {".tif", ".tiff"}:
                sidecar = Path(str(path) + ".provenance.json")
                if not sidecar.is_file() or sidecar.is_symlink():
                    continue
                meta = json.loads(sidecar.read_text())
                if meta.get("synthetic") or meta.get("product") != "GCOV":
                    continue
                import rasterio
                from rasterio.warp import transform_bounds
                with rasterio.open(path) as raster:
                    bounds = list(transform_bounds(raster.crs, "EPSG:4326", *raster.bounds))
                meta.update(bounds=bounds, id=path.stem,
                            polarizations=[meta.get("polarization", "HHHH")])
            else:
                meta = inspect_gcov(path)
            observation = {
                "id": path.stem, "title": meta.get("source_granule") or path.stem,
                "date": meta.get("date"), "product": meta.get("product", "GCOV"),
                "maturity": meta.get("maturity", "UNKNOWN"), "polarizations": meta.get("polarizations", []),
                "track": meta.get("track"), "frame": meta.get("frame"), "direction": meta.get("direction"),
                "bbox": meta.get("bounds"), "browse_url": None, "download_url": meta.get("source_url"),
                "size_mb": round(path.stat().st_size / 1024**2, 2), "collection": "Local scientific input",
                "local_available": True, "derivative": bool(meta.get("derivative", False)),
                "source_notes": meta.get("source_notes"), "filename": path.name,
            }
            observations.append(observation)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    get_store().save_observations(observations)
    return observations


@app.get("/api/health")
async def health():
    settings = get_settings()
    connected = False
    try:
        client = await temporal_client()
        await asyncio.wait_for(client.workflow_service.get_system_info(
            __import__("temporalio.api.workflowservice.v1", fromlist=["GetSystemInfoRequest"]).GetSystemInfoRequest()), timeout=2)
        connected = True
    except Exception:
        app.state.temporal = None
    return {"status": "ready" if connected else "degraded", "version": "0.1.0",
            "temporal": {"connected": connected, "address": settings.temporal_address},
            "capabilities": {"catalog": True, "analysis": connected, "language_agent": __import__("terra.agent", fromlist=["available"]).available(),
                             "language_model": __import__("terra.agent", fromlist=["model_name"]).model_name(),
                             "original_products": bool(__import__("terra.acquire", fromlist=["earthdata_token"]).earthdata_token()),
                             "isolated_execution": settings.compute_mode == "docker", "compute_mode": settings.compute_mode,
                             "photorealistic_3d": bool(google_tiles_key())},
            "inputs_count": len(available_inputs()), "connector": connector.diagnostics()}


@app.get('/api/ready')
async def ready():
    from .runtime_health import worker_ready
    if not worker_ready(get_settings().data_dir):
        return JSONResponse({'status': 'starting', 'worker': False}, status_code=503)
    try:
        client = await temporal_client()
        from temporalio.api.workflowservice.v1 import GetSystemInfoRequest
        await asyncio.wait_for(client.workflow_service.get_system_info(GetSystemInfoRequest()), timeout=2)
    except Exception:
        app.state.temporal = None
        return JSONResponse({'status': 'starting', 'worker': True, 'temporal': False}, status_code=503)
    return {'status': 'ready', 'worker': True, 'temporal': True}


@app.get("/api/inputs")
async def inputs():
    observations = await asyncio.to_thread(local_observations)
    return {"inputs": observations, "observations": observations,
            "directory": ".data/inputs", "note": "Only original GCOV products or derivatives with provenance metadata are eligible."}


@app.get("/api/observations")
async def observations(bbox: str = Query(...), limit: int = Query(24, ge=1, le=100)):
    try:
        region = validate_bbox([float(value) for value in bbox.split(",")])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        from .catalog import search_observations
        items = await search_observations(region, limit=limit)
        get_store().save_observations(items)
    except Exception as exc:
        raise HTTPException(502, "The NASA/ASF catalog could not be reached. Try again; no observations have been invented or substituted.") from exc
    return {"observations": items, "source": "NASA CMR / ASF", "fetched_at": utc_now(), "bbox": region}


@app.get("/api/feeds/{name}")
async def live_feed(name: str):
    from .feeds import FEEDS, FeedError
    loader = FEEDS.get(name)
    if loader is None:
        raise HTTPException(404, "Unknown feed.")
    try:
        return await loader()
    except FeedError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/geocode")
async def geocode_place(q: str = Query(..., min_length=2, max_length=120)):
    from .feeds import geocode
    try:
        return await geocode(q)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "Place search is unavailable. Coordinates (lat, lon) still work.") from exc


async def current_investigation(identifier: str) -> dict:
    snapshot = get_store().investigation(identifier)
    if snapshot is None:
        raise HTTPException(404, "Investigation not found.")
    try:
        client = await temporal_client()
        state = await asyncio.wait_for(client.get_workflow_handle(identifier).query(InvestigationWorkflow.state_snapshot), timeout=3)
        if state:
            # Report enrichment may archive optical context after a completed
            # workflow. Temporal remains authoritative for execution state and
            # computed science; SQLite contributes only these later artifacts.
            stored_result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else None
            if state.get("status") == "completed" and stored_result and stored_result.get("optical_evidence"):
                live_result = state.get("result") or {}
                state = {**state, "result": {**live_result,
                    "optical_evidence": stored_result["optical_evidence"],
                    "artifacts": stored_result.get("artifacts", live_result.get("artifacts", []))}}
            return _with_rewrite({**state, "followups": snapshot.get("followups", []), "state_source": "temporal", "state_live": True}, snapshot)
    except Exception:
        pass
    return _with_rewrite({**snapshot, "state_source": "persisted_snapshot", "state_live": False}, snapshot)


def _with_rewrite(item: dict, snapshot: dict) -> dict:
    """A written answer produced after completion replaces the computed-summary fallback."""
    rewrite = snapshot.get("narrative_rewrite")
    if rewrite and isinstance(item.get("result"), dict):
        item = {**item, "result": {**item["result"], "narrative": rewrite, "narrative_status": "ready", "narrative_note": None}}
    return with_signatures(item)


def with_signatures(item: dict) -> dict:
    """Patches measured before polarimetric signatures existed get them from their stored per-polarization change."""
    from .signatures import signature
    change = ((item.get("result") or {}).get("measurements") or {}).get("change") or {}
    spots = change.get("hotspots") or []
    if not spots or all(s.get("signature") for s in spots):
        return item
    signed = [s if s.get("signature") else {**s, "signature": signature(s.get("by_polarization") or {}, s["direction"])} for s in spots]
    result = item["result"]; measurements = result["measurements"]
    return {**item, "result": {**result, "measurements": {**measurements, "change": {**change, "hotspots": signed}}}}


@app.get("/api/investigations")
async def list_investigations():
    return {"investigations": get_store().investigations()}


@app.post("/api/investigations", status_code=201)
async def create_investigation(body: InvestigationRequest, idempotency_key: str | None = Header(default=None)):
    identifier = str(uuid5(NAMESPACE_URL, "terra:" + idempotency_key)) if idempotency_key else str(uuid4())
    if idempotency_key and len(idempotency_key) > 160:
        raise HTTPException(422, "Idempotency key is too long.")
    previous = get_store().investigation(identifier)
    if previous:
        if any(previous.get(key) != value for key, value in body.model_dump().items()):
            raise HTTPException(409, "This idempotency key belongs to a different investigation request.")
        return await current_investigation(identifier)
    for observation_id in body.observation_ids:
        observation = get_store().observation(observation_id)
        if observation is None:
            raise HTTPException(422, "Select observations returned by the catalog or local inputs endpoint first.")
        if observation.get("product") != "GCOV":
            raise HTTPException(422, "The current scientific method requires GCOV products.")
    client = await temporal_client()
    now = utc_now()
    request = {**body.model_dump(), "id": identifier, "created_at": now, "updated_at": now}
    snapshot = {**request, "status": "queued", "stage": "discover", "progress": 0,
                "events": [], "result": None, "blocker": None}
    # Index before starting so a fast activity cannot be overwritten by a queued snapshot.
    get_store().save_investigation(snapshot)
    try:
        await client.start_workflow(InvestigationWorkflow.run, request, id=identifier,
                                    task_queue=get_settings().task_queue,
                                    execution_timeout=timedelta(days=30))
    except Exception as exc:
        snapshot.update(status="failed", stage="launch", blocker="The durable service could not start this investigation.")
        get_store().save_investigation(snapshot)
        raise HTTPException(503, snapshot["blocker"]) from exc
    return snapshot


@app.get("/api/investigations/{identifier}")
async def get_investigation(identifier: str):
    return await current_investigation(identifier)


@app.get("/api/context")
async def area_context(bbox: str = Query(...)):
    from .agent import context_near
    try:
        region = validate_bbox([float(v) for v in bbox.split(",")], bounded=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await context_near(region)


@app.get("/api/terrain")
async def terrain_features(bbox: str = Query(...)):
    """Land cover and named terrain features for the relief map."""
    from .terrain import terrain_context
    try:
        region = validate_bbox([float(v) for v in bbox.split(",")], bounded=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if region[2] - region[0] > 2 or region[3] - region[1] > 2:
        raise HTTPException(422, "Terrain detail is available for areas up to 2 degrees across.")
    return await terrain_context(region)


@app.get("/api/thermal")
async def surface_temperature(bbox: str = Query(...)):
    """Measured land-surface temperature (Landsat band 10) for the drone cameras' IR mode."""
    from .connector import ConnectorError
    from .thermal import thermal
    try:
        region = validate_bbox([float(v) for v in bbox.split(",")], bounded=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if region[2] - region[0] > 1 or region[3] - region[1] > 1:
        raise HTTPException(422, "Thermal scenes are read for areas up to 1 degree across.")
    from rasterio.errors import RasterioError
    try:
        return await thermal(region)
    except (ConnectorError, TimeoutError) as exc:
        return {"available": False, "reason": f"Thermal imagery is unavailable right now ({exc}).", "bounds": region}
    except (RasterioError, KeyError, ValueError) as exc:  # an unreadable or unexpected scene is "no scene", not a server error
        log.warning("thermal scene unreadable: %s", exc.__class__.__name__)
        return {"available": False, "reason": "The latest Landsat scene for this area could not be read.", "bounds": region}


@app.get("/api/conditions")
async def modelled_surface_conditions(bbox: str = Query(...), dates: str = Query(..., max_length=100)):
    from .conditions import surface_conditions
    try:
        region = validate_bbox([float(v) for v in bbox.split(",")], bounded=True)
        return await surface_conditions(region, dates.split(","))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


class ProfileRequest(BaseModel):
    vertices: list[tuple[float, float]] = Field(min_length=2, max_length=16)
    samples: int = Field(default=80, ge=2, le=256, strict=True)
    investigation_id: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9-]+$")


@app.post("/api/profile")
async def geographic_profile(body: ProfileRequest):
    from .conditions import path_points, elevations, sample_profile_rasters
    try:
        points, distances = path_points(body.vertices, body.samples)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    declared = []
    directory = None
    if body.investigation_id:
        investigation = get_store().investigation(body.investigation_id)
        if investigation is None:
            raise HTTPException(404, "Investigation not found.")
        result = investigation.get("result") or {}
        declared = result.get("layers") or []
        directory = safe_job_directory(body.investigation_id)

    async def radar():
        return await asyncio.to_thread(sample_profile_rasters, directory, points, declared) if directory and declared else []
    heights, layers = await asyncio.gather(elevations(points), radar())
    measured = sum(height is not None for height in heights)
    samples = [{"lon": round(point[0], 7), "lat": round(point[1], 7), "distance_km": distances[index],
                "elevation_m": heights[index], "radar": {layer["id"]: layer["values"][index] for layer in layers}}
               for index, point in enumerate(points)]
    return {"points": samples, "layers": layers,
            "terrain_source": "Copernicus DEM GLO-90 (2021), via Open-Meteo · 90 m surface model",
            "terrain_source_url": "https://open-meteo.com/en/docs/elevation-api", "source": "Copernicus / Open-Meteo; computed NISAR artifacts when selected",
            "distance_km": distances, "elevation_m": heights,
            "status": "available" if measured == len(points) else "partial" if measured or layers else "unavailable",
            "notes": ["Elevation describes the reference surface; this is not a deformation profile.",
                      "Radar profiles sample actual computed pixels. Null values are missing or outside raster coverage.",
                      "Profile spacing can be finer than the 90 m elevation grid; it does not increase the source resolution."]}


@app.post("/api/investigations/{identifier}/profile")
async def investigation_profile(identifier: str, body: ProfileRequest):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", identifier):
        raise HTTPException(404, "Investigation not found.")
    return await geographic_profile(body.model_copy(update={"investigation_id": identifier}))


class FollowUp(BaseModel):
    question: str = Field(min_length=3, max_length=600)


@app.post("/api/investigations/{identifier}/ask")
async def ask_followup(identifier: str, body: FollowUp):
    from .agent import available, follow_up
    if not available():
        raise HTTPException(503, "Add OPENAI_API_KEY to .env to ask follow-up questions.")
    item = await current_investigation(identifier)
    if not item.get("result"):
        raise HTTPException(409, "Follow-up questions open once the investigation is complete.")
    try:
        answer = await follow_up(item, body.question, (item.get("result") or {}).get("context"))
    except Exception as exc:
        raise HTTPException(502, "The language model could not answer this request. Please retry shortly.") from None
    snapshot = get_store().investigation(identifier) or item
    snapshot["followups"] = [*snapshot.get("followups", []), answer][-20:]
    get_store().save_investigation(snapshot)
    return answer


@app.post("/api/investigations/{identifier}/narrate")
async def write_investigation_answer(identifier: str):
    """Write (or rewrite) the language answer for a completed investigation from its stored evidence."""
    from .agent import available, write_answer
    if not available():
        raise HTTPException(503, "Add OPENAI_API_KEY to .env to write answers.")
    item = await current_investigation(identifier)
    if item.get("status") != "completed" or not isinstance(item.get("result"), dict):
        raise HTTPException(409, "Answers can be written once the investigation is complete.")
    try:
        narrative = await write_answer(item, item["result"].get("context"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "The language model could not write an answer right now.") from exc
    snapshot = get_store().investigation(identifier) or item
    snapshot["narrative_rewrite"] = narrative
    get_store().save_investigation(snapshot)
    return narrative


@app.get("/api/investigations/{identifier}/sample")
async def sample_layers(identifier: str, lon: float = Query(..., ge=-180, le=180), lat: float = Query(..., ge=-90, le=90)):
    """Read the computed float raster values at a point (not the colour preview)."""
    import rasterio
    from rasterio.warp import transform as warp
    if get_store().investigation(identifier) is None:
        raise HTTPException(404, "Investigation not found.")
    directory = safe_job_directory(identifier)
    values = []
    for path in sorted(directory.glob("*.tif")):
        if path.is_symlink():
            continue
        with rasterio.open(path) as ds:
            xs, ys = warp("EPSG:4326", ds.crs, [lon], [lat])
            row, col = ds.index(xs[0], ys[0])
            if not (0 <= row < ds.height and 0 <= col < ds.width):
                continue
            value = float(ds.read(1, window=((row, row + 1), (col, col + 1)))[0, 0])
            nodata = ds.nodata
            valid = math.isfinite(value) and (nodata is None or value != nodata)
            values.append({"layer": path.stem, "value": round(value, 3) if valid else None, "units": "dB", "row": row, "col": col})
    return {"lon": lon, "lat": lat, "values": values}


@app.post("/api/investigations/{identifier}/cancel")
async def cancel_investigation(identifier: str):
    item = await current_investigation(identifier)
    if item["status"] in {"completed", "cancelled", "failed"}:
        raise HTTPException(409, "This investigation is not running.")
    client = await temporal_client()
    await client.get_workflow_handle(identifier).cancel()
    return {"id": identifier, "status": "cancellation_requested"}


@app.post("/api/investigations/{identifier}/resume")
async def resume_investigation(identifier: str):
    item = await current_investigation(identifier)
    client = await temporal_client()
    if item["status"] == "blocked":
        await client.get_workflow_handle(identifier).signal(InvestigationWorkflow.resume)
    elif item["status"] in {"failed", "cancelled"}:
        request = {key: item[key] for key in ("id", "title", "question", "bbox", "observation_ids", "mode", "created_at")}
        request["updated_at"] = utc_now()
        await client.start_workflow(InvestigationWorkflow.run, request, id=identifier,
                                    task_queue=get_settings().task_queue,
                                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                                    execution_timeout=timedelta(days=30))
    else:
        raise HTTPException(409, "Only blocked, failed, or cancelled investigations can be resumed.")
    return {"id": identifier, "status": "resume_requested"}


@app.get("/api/investigations/{identifier}/replay")
async def replay_investigation(identifier: str):
    item = await current_investigation(identifier)
    if item["status"] != "completed":
        raise HTTPException(409, "Only completed investigations have a recorded replay.")
    return {"replay": True, "recorded_at": item["updated_at"], "investigation": item,
            "note": "Recorded execution events and original artifacts; this is not a new computation."}


@app.get("/api/investigations/{identifier}/report")
async def investigation_report(identifier: str):
    item = get_store().investigation(identifier)
    if item is None:
        raise HTTPException(404, "Investigation not found.")
    path = safe_job_directory(identifier) / "report.md"
    if not path.is_file() or path.is_symlink():
        raise HTTPException(409, "This investigation has not produced a report yet.")
    return FileResponse(path, media_type="text/markdown", filename=f"terra-{identifier[:8]}-report.md")


@app.get("/api/investigations/{identifier}/artifacts/{filename}")
async def investigation_artifact(identifier: str, filename: str):
    if get_store().investigation(identifier) is None:
        raise HTTPException(404, "Investigation not found.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", filename) or filename.startswith("."):
        raise HTTPException(404, "Artifact not found.")
    if Path(filename).suffix not in {".png", ".tif", ".geojson", ".md"} and filename != "public-result.json":
        raise HTTPException(404, "Artifact is not public.")
    directory = safe_job_directory(identifier)
    path = directory / filename
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise HTTPException(404, "Artifact not found.")
    return FileResponse(path)
