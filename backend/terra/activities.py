"""I/O activities for the durable workflow. Science runs in a fixed process."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.parse import urlparse

import httpx

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .config import get_settings
from .store import Store
from .connector import request as connector_request

SURFACE_CONDITIONS_TIMEOUT = 8.0


def safe_job_directory(identifier: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", identifier):
        raise ValueError("Invalid investigation identifier")
    settings = get_settings()
    path = settings.artifacts / identifier
    path.mkdir(parents=True, exist_ok=True)
    if not path.resolve().is_relative_to(settings.artifacts.resolve()):
        raise ValueError("Invalid artifact directory")
    return path


def available_inputs() -> list[Path]:
    root = get_settings().inputs.resolve()
    if not root.exists():
        return []
    return [p for p in sorted(root.iterdir()) if p.suffix.lower() in {".h5", ".hdf5", ".tif", ".tiff"}
            and p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root)]


def resolve_input(observation: dict) -> Path | None:
    candidates = {str(observation.get("id", "")), str(observation.get("title", ""))}
    candidates |= {Path(value).stem for value in candidates}
    for path in available_inputs():
        if "__aoi-" in path.name:
            continue  # AOI subsets are matched by coverage in prepare_inputs
        if path.name in candidates or path.stem in candidates:
            return path
        for sidecar in (Path(str(path) + ".provenance.json"), path.with_suffix(".provenance.json")):
            if not sidecar.is_file() or sidecar.is_symlink():
                continue
            try:
                source = json.loads(sidecar.read_text())
                source_ids = {str(source.get(key, "")) for key in ("id", "source_id", "granule_id", "source_granule")}
                if candidates.intersection(source_ids) - {""}:
                    return path
            except (OSError, ValueError):
                continue
    return None


@activity.defn
async def persist_snapshot(payload: dict) -> None:
    Store(get_settings().database).save_investigation(payload)


@activity.defn
async def discover_inputs(payload: dict) -> dict:
    store = Store(get_settings().database)
    observations = [store.observation(identifier) for identifier in payload["observation_ids"]]
    if not observations or any(item is None for item in observations):
        raise ApplicationError("Selected observations are no longer in the local catalog. Search the region again.", non_retryable=True)
    if any(item.get("product") != "GCOV" for item in observations):
        raise ApplicationError("This analysis supports GCOV backscatter only. Other products require a different scientific method.", non_retryable=True)
    return {"observations": observations}


@activity.defn
async def prepare_inputs(payload: dict) -> dict:
    from .acquire import AcquisitionError, acquire_subset, earthdata_token, find_subset
    async def prepare_one(observation: dict) -> dict:
        started = time.perf_counter()
        failure = None
        path = resolve_input(observation)
        if path is None and observation.get("download_url"):
            path = find_subset(observation["id"], payload["bbox"])
            if path is None and earthdata_token():
                try:
                    path = await asyncio.to_thread(acquire_subset, observation, payload["bbox"], "A")
                except AcquisitionError as exc:
                    failure = str(exc)
                except Exception as exc:  # network or HDF5 failure: block, never substitute
                    failure = f"Reading the original product failed ({type(exc).__name__})."
        if path is None:
            return {"missing": observation.get("title") or observation["id"], "failure": failure}
        return {"input": {"path": str(path.resolve()), "observation": observation},
                "seconds": round(time.perf_counter() - started, 3)}

    # Each acquisition is independent; preserve chronological order after gathering.
    # HDF5 may serialize some native calls internally, but remote discovery can overlap.
    prepared = await asyncio.gather(*(prepare_one(observation) for observation in payload["observations"]))
    inputs = [entry["input"] for entry in prepared if "input" in entry]
    missing = [entry["missing"] for entry in prepared if "missing" in entry]
    failures = [entry["failure"] for entry in prepared if entry.get("failure")]
    if missing:
        reason = failures[0] if failures else (
            "Original GCOV rasters are required. Add EARTHDATA_TOKEN to .env so TERRA can read the original products, "
            "or place them in .data/inputs, then resume. Catalog footprints and browse images cannot substitute for measurements.")
        return {"ready": False, "reason": reason, "missing": missing}
    return {"ready": True, "inputs": inputs, "preparation_seconds": [entry["seconds"] for entry in prepared]}


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False))
    temporary.replace(path)


def _manifest(payload: dict) -> dict:
    settings = get_settings()
    return {"mode": payload["mode"], "bbox": payload["bbox"], "inputs": payload["inputs"],
            "input_root": str(settings.inputs.resolve()), "threshold_db": 3.0, "frequency": "A"}


@activity.defn
async def compute_science(payload: dict) -> dict:
    settings = get_settings()
    directory = safe_job_directory(payload["id"])
    manifest = _manifest(payload)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    # Activity retries reuse only a complete result for exactly this manifest.
    cache = directory / "completed.json"
    result_path = directory / "result.json"
    if cache.exists() and result_path.exists():
        old = json.loads(cache.read_text())
        if old.get("manifest_sha256") == digest:
            return {"result_path": "result.json"}
    manifest_path = directory / "manifest.json"
    _atomic_json(manifest_path, manifest)
    if settings.compute_mode == "docker":
        docker_manifest = {**manifest, "input_root": "/inputs", "inputs": [
            {**item, "path": "/inputs/" + Path(item["path"]).name} for item in manifest["inputs"]]}
        _atomic_json(directory / "docker-manifest.json", docker_manifest)
        command = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--memory=2g", "--cpus=2", "--pids-limit=64",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
                   "-v", f"{settings.inputs}:/inputs:ro", "-v", f"{directory}:/output:rw",
                   settings.docker_image, "python", "-m", "terra.compute_runner", "/output/docker-manifest.json", "/output"]
    elif settings.compute_mode == "subprocess":
        command = [sys.executable, "-m", "terra.compute_runner", str(manifest_path), str(directory)]
    else:
        raise ApplicationError("Unsupported TERRA_COMPUTE_MODE. Use subprocess or docker.", non_retryable=True)
    # Never pass production credentials to scientific execution.
    environment = {"PATH": os.getenv("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(settings.root / "backend"),
                   "PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                   "TMPDIR": os.getenv("TMPDIR", "/tmp")}
    log_path = directory / "compute.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(*command, cwd=settings.root, env=environment,
                                                       stdout=log, stderr=log, start_new_session=True)
        started = asyncio.get_running_loop().time()
        try:
            while process.returncode is None:
                activity.heartbeat({"stage": "analyze", "elapsed_seconds": int(asyncio.get_running_loop().time() - started)})
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    if asyncio.get_running_loop().time() - started > settings.compute_timeout:
                        raise ApplicationError("Scientific computation exceeded the configured time limit.", non_retryable=True)
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
    if process.returncode != 0:
        message = "Scientific computation failed. Check the selected products, quality masks, and analysis region."
        if result_path.exists():
            try:
                failure = json.loads(result_path.read_text())
                message = str(failure.get("error", {}).get("message", message))[:600]
            except (ValueError, OSError):
                pass
        raise ApplicationError(message, non_retryable=True)
    if not result_path.is_file():
        raise ApplicationError("The scientific process did not produce a result artifact.", non_retryable=True)
    _atomic_json(cache, {"manifest_sha256": digest})
    return {"result_path": "result.json"}


@activity.defn
async def verify_science(payload: dict) -> dict:
    directory = safe_job_directory(payload["id"])
    result = json.loads((directory / "result.json").read_text())
    if result.get("status") == "failed" or not result.get("provenance"):
        raise ApplicationError("The computation did not provide valid provenance.", non_retryable=True)
    if not result.get("validation") or not result.get("measurements"):
        raise ApplicationError("Required scientific validation or measurements are missing.", non_retryable=True)
    json.dumps(result, allow_nan=False)
    for layer in result.get("layers", []):
        name = Path(layer["path"]).name
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise ApplicationError("A referenced scientific layer is unavailable.", non_retryable=True)
    return {"result_path": "result.json"}


@activity.defn
async def publish_science(payload: dict) -> dict:
    """Publish existing computed artifacts without waiting for network enrichment."""
    from .agent import condition_bullets, measured_answer
    directory = safe_job_directory(payload["id"])
    result = json.loads((directory / "result.json").read_text())
    result["surface_conditions"] = payload.get("surface_conditions") or result.get("surface_conditions") or {
        "status": "pending" if payload.get("narrative_status") == "pending" else "unavailable",
        "acquisitions": [], "unavailable": [],
        "note": "Modelled conditions at the radar acquisition times have not been collected yet.",
    }
    base = f"/api/investigations/{payload['id']}/artifacts"
    for layer in result.get("layers", []):
        layer["path"] = Path(layer["path"]).name
        layer["url"] = f"{base}/{layer['path']}"
    report = result.get("report", "")
    if not isinstance(report, str):
        report = json.dumps(report, indent=2)
    narrative = payload.get("narrative") or measured_answer({**payload, "result": result}, payload.get("context"))
    result["narrative"] = narrative
    result["narrative_note"] = payload.get("narrative_note")
    result["context"] = payload.get("context")
    result["plan"] = payload.get("plan")
    result["timings"] = payload.get("timings", {})
    result["narrative_status"] = payload.get("narrative_status", "ready")
    result["enrichment_status"] = payload.get("enrichment_status", "not_requested")
    enrichment = payload.get("enrichment") or {}
    result["optical_evidence"] = enrichment.get("optical_evidence", [])
    if enrichment.get("optical_note"):
        result["optical_note"] = enrichment["optical_note"]
    for index, source in enumerate(result.get("provenance", {}).get("inputs", []), 1):
        name = f"source-browse-{index}.png"
        if name in enrichment.get("browse_artifacts", []):
            source.setdefault("catalog", {})["evidence_browse_url"] = f"{base}/{name}"
    if narrative:
        lines = [f"## Answer\n\n**{narrative['headline']}**\n", narrative["answer"], "", "### Evidence"]
        lines += [f"- {item}" for item in narrative["evidence"]]
        if narrative["possible_explanations"]:
            lines += ["", "### Possible explanations (hypotheses, not findings)"] + [f"- {item}" for item in narrative["possible_explanations"]]
        verification = narrative.get("verification", {})
        lines += ["", f"_Written by {narrative.get('model')} from the computed evidence; "
                  f"numeric check: {verification.get('status', 'not_checked')}. "
                  "This does not validate causal or event-specific conclusions._", "", "---", ""]
        report = "\n".join(lines) + report
    conditions = result["surface_conditions"]
    weather_lines = ["", "## Modelled surface conditions at radar acquisition times", "",
                     "These are model values near the area's centre, not NISAR measurements or proof of what caused a radar change."]
    weather_lines.extend(f"- {line}" for line in condition_bullets(conditions))
    if conditions.get("source"):
        weather_lines.extend(["", f"Source: {conditions['source']}"])
    for item in conditions.get("acquisitions", []):
        details = f"Acquisition {item.get('date')}; model sample {item.get('model_time', 'not recorded')}; {item.get('model', 'model unspecified')}."
        if item.get("source_url"):
            details += f" Source: {item['source_url']}"
        weather_lines.append(f"- {details}")
        if item.get("precipitation_window_start") and item.get("precipitation_window_end"):
            weather_lines.append(f"  Precipitation window: {item['precipitation_window_start']} to {item['precipitation_window_end']}, ending at the last completed model hour before acquisition.")
        if item.get("precipitation_definition"):
            weather_lines.append("  " + item["precipitation_definition"] + ".")
        if item.get("rain_72h_mm") is None:
            weather_lines.append("  The precipitation total is unavailable or incomplete; it must not be interpreted as no rain.")
    if conditions.get("unavailable"):
        weather_lines.append("Unavailable acquisition dates: " + ", ".join(conditions["unavailable"]) + ".")
    if conditions.get("note"):
        weather_lines.extend(["", str(conditions["note"])])
    report += "\n".join(weather_lines) + "\n"
    result["report"] = report
    temporary_report = directory / ".report.md.tmp"
    temporary_report.write_text(report)
    temporary_report.replace(directory / "report.md")
    result["artifacts"] = [{"name": path.name, "url": f"{base}/{path.name}", "size_bytes": path.stat().st_size}
                           for path in sorted(directory.iterdir()) if path.is_file() and not path.name.startswith(".") and path.suffix in {".tif", ".png", ".geojson", ".md"}]
    result["report_url"] = f"/api/investigations/{payload['id']}/report"
    _atomic_json(directory / "public-result.json", result)
    return result


@activity.defn
async def enrich_science(payload: dict) -> dict:
    """Durable optional imagery activity, independent of answer publication.

    It never mutates the computed measurements or public result. The workflow
    merges its completed result through publish_science, preventing lost updates.
    """
    from .optical import collect_optical_evidence
    directory = safe_job_directory(payload["id"])
    result = json.loads((directory / "result.json").read_text())
    started = time.perf_counter()

    async def browse(index: int, source: dict) -> str | None:
        url = source.get("catalog", {}).get("browse_url", "")
        parsed = urlparse(url) if isinstance(url, str) else None
        if not parsed or (parsed.scheme, parsed.hostname) != ("https", "nisar.asf.earthdatacloud.nasa.gov") or not parsed.path.startswith("/BROWSE/") or not parsed.path.endswith(".png"):
            return None
        target = directory / f"source-browse-{index}.png"
        if target.is_file() and not target.is_symlink():
            return target.name
        try:
            response = await connector_request("asf", "GET", url, timeout=12, max_bytes=12_000_000)
            if response.status_code != 200 or not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
                return None
            temporary = directory / f".source-browse-{index}.tmp"
            temporary.write_bytes(response.content)
            temporary.replace(target)
            return target.name
        except Exception:
            return None

    async def optical() -> dict:
        try:
            images = await collect_optical_evidence(result, directory)
            return {"optical_evidence": images}
        except Exception as exc:
            return {"optical_evidence": [], "optical_note": f"Optical context could not be collected ({type(exc).__name__}). Radar analysis is unaffected."}

    tasks = [browse(index, source) for index, source in enumerate(result.get("provenance", {}).get("inputs", []), 1)]
    outputs = await asyncio.gather(optical(), *tasks)
    enrichment = {**outputs[0], "browse_artifacts": [name for name in outputs[1:] if name],
                  "duration_seconds": round(time.perf_counter() - started, 3)}
    _atomic_json(directory / "enrichment.json", enrichment)
    return {"enrichment": enrichment, "enrichment_status": "ready"}


@activity.defn
async def plan_investigation(payload: dict) -> dict:
    """Coordinator: find acquisitions for the area, gather live context, choose the method."""
    from .acquire import earthdata_token
    from .agent import context_near, plan
    from .catalog import search_observations
    store = Store(get_settings().database)
    context_task = asyncio.create_task(context_near(payload["bbox"]))
    try:
        observations = await search_observations(payload["bbox"], limit=60)
    except BaseException:
        context_task.cancel()
        await asyncio.gather(context_task, return_exceptions=True)
        raise
    store.save_observations(observations)
    if not earthdata_token():
        from .app import local_observations
        local = await asyncio.to_thread(local_observations)
        store.save_observations(local)
        observations += [{**o, "local": True} for o in local]
    # Sign the download links of the most likely pair while the language model plans: the usual
    # choice is the widest compatible span, and signing takes about two seconds per product.
    import threading
    from .acquire import presign
    from .agent import select_acquisitions
    likely, _ = select_acquisitions("change", observations, payload["bbox"])
    threading.Thread(target=presign, args=([o for o in observations if o.get("id") in set(likely)],), daemon=True).start()
    # The question and catalog determine the radar method. Context is not needed
    # to choose a compatible pair, so language planning can overlap its retrieval.
    decided = await plan(payload.get("question", ""), payload.get("title", ""), payload["bbox"], observations, {})
    if not decided["observation_ids"]:
        context_task.cancel()
        raise ApplicationError(decided["selection"], non_retryable=True)
    # Feeds are kept warm by the worker, so context is normally ready already. A cold provider must
    # not hold up the radar work: the writer fetches context again later if it is missing here.
    context = None
    try:
        context = await asyncio.wait_for(asyncio.shield(context_task), timeout=1.0)
    except (asyncio.TimeoutError, Exception):
        pass
    # Products the model chose beyond the likely pair are signed in the background as well;
    # reading in the next activity waits for (and reuses) any signing already in flight.
    others = [o for o in observations if o.get("id") in set(decided["observation_ids"]) - set(likely)]
    if others:
        threading.Thread(target=presign, args=(others,), daemon=True).start()
    return {"plan": decided, "observation_ids": decided["observation_ids"], "mode": decided["mode"], "context": context}


@activity.defn
async def narrate_findings(payload: dict) -> dict:
    """Synthesis agent: write the answer from the computed result; numbers are checked afterwards."""
    from .agent import available, context_near, measured_answer, write_answer
    directory = safe_job_directory(payload["id"])
    result = json.loads((directory / "result.json").read_text())

    async def context_for_result() -> dict:
        return payload.get("context") or await context_near(payload["bbox"])

    context, conditions = await asyncio.gather(context_for_result(), _surface_conditions(payload, result, directory))
    investigation = {**payload, "surface_conditions": conditions, "result": {**result, "surface_conditions": conditions}}
    additions = {"context": context, "surface_conditions": conditions}
    if not available():
        return {**additions, "narrative": measured_answer(investigation, context), "narrative_status": "computed_summary",
                "narrative_note": "The answer is generated directly from computed measurements and labelled model context; no language model is configured."}
    try:
        narrative = await write_answer(investigation, context)
        return {**additions, "narrative": narrative, "narrative_status": "ready"}
    except Exception as exc:
        return {**additions, "narrative": measured_answer(investigation, context), "narrative_status": "computed_summary",
                "narrative_note": f"The language answer was unavailable or failed evidence checks ({type(exc).__name__}); the computed summary is shown."}


async def _surface_conditions(payload: dict, result: dict, directory: Path) -> dict:
    """Bounded optional acquisition-time model context, retained for activity retries."""
    from .conditions import surface_conditions
    dates = [item.get("metadata", {}).get("date") for item in result.get("provenance", {}).get("inputs", [])[:2]]
    dates = list(dict.fromkeys(date for date in dates if isinstance(date, str) and date))
    bbox = payload.get("bbox") or result.get("provenance", {}).get("parameters", {}).get("bbox")
    cache = directory / "surface-conditions.json"
    if cache.is_file() and not cache.is_symlink():
        try:
            cached = json.loads(cache.read_text())
            if cached.get("requested_dates") == dates and cached.get("requested_bbox") == bbox and cached.get("status") in {"available", "partial"}:
                return cached
        except (OSError, ValueError):
            pass
    try:
        if not dates:
            raise ValueError("Acquisition times unavailable")
        conditions = await asyncio.wait_for(surface_conditions(bbox, dates), timeout=SURFACE_CONDITIONS_TIMEOUT)
    except Exception:
        conditions = {"status": "unavailable", "acquisitions": [], "unavailable": dates,
                      "note": "Modelled surface conditions could not be obtained within the time budget. Precipitation and soil moisture are unknown; the radar measurements are unchanged."}
    conditions = {**conditions, "requested_dates": dates, "requested_bbox": bbox}
    acquisitions = sorted(conditions.get("acquisitions", []), key=lambda item: item.get("date", ""))
    comparison: dict = {"interpretation": "Model context only; differences do not establish the cause of radar changes."}
    if len(acquisitions) == 2:
        before, after = acquisitions
        same_model = bool(before.get("model")) and before.get("model") == after.get("model")
        same_layer = bool(before.get("soil_layer")) and before.get("soil_layer") == after.get("soil_layer")
        if same_model and same_layer:
            comparison["soil_moisture_comparability"] = "same model and soil layer"
            a, b = before.get("soil_moisture_m3m3"), after.get("soil_moisture_m3m3")
            if isinstance(a, (float, int)) and isinstance(b, (float, int)):
                comparison["soil_moisture_change_m3m3"] = round(b - a, 3)
        else:
            comparison["soil_moisture_comparability"] = "not comparable: model or soil layer differs"
        a, b = before.get("rain_72h_mm"), after.get("rain_72h_mm")
        comparison["precipitation_comparability"] = "same model" if same_model else "different or unknown model products; no difference computed"
        if same_model and isinstance(a, (float, int)) and isinstance(b, (float, int)) and all(
            item.get("rain_observed_hours") == item.get("rain_expected_hours") == 72 for item in acquisitions
        ):
            comparison["rain_72h_change_mm"] = round(b - a, 1)
            comparison["precipitation_72h_change_mm"] = comparison["rain_72h_change_mm"]
    conditions["comparison"] = comparison
    _atomic_json(cache, conditions)
    return conditions
