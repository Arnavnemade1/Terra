"""Deterministic, durable investigation orchestration; no I/O in workflow code."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class InvestigationWorkflow:
    def __init__(self) -> None:
        self.state: dict = {}
        self.resume_requested = False
        self.started_at = None
        self.stage_started_at = None

    @workflow.query
    def state_snapshot(self) -> dict:
        return self.state

    @workflow.signal
    def resume(self) -> None:
        self.resume_requested = True

    async def checkpoint(self, stage: str, status: str, message: str, progress: int) -> None:
        stamp = workflow.now()
        now = stamp.isoformat()
        timings = self.state.setdefault("timings", {})
        if self.started_at is not None:
            timings["elapsed_seconds"] = round((stamp - self.started_at).total_seconds(), 3)
        previous = self.state.get("stage")
        if previous != stage and self.stage_started_at is not None:
            durations = timings.setdefault("stages_seconds", {})
            durations[previous] = round(durations.get(previous, 0) + (stamp - self.stage_started_at).total_seconds(), 3)
            self.stage_started_at = stamp
        self.state.update(stage=stage, status=status, progress=progress, updated_at=now)
        self.state["events"].append({"stage": stage, "status": status, "message": message, "at": now,
                                     "elapsed_seconds": timings.get("elapsed_seconds")})
        await workflow.execute_activity(
            "persist_snapshot", self.state,
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def step(self, name: str, payload: dict, timeout: int = 120) -> dict:
        started = workflow.now()
        result = await workflow.execute_activity(
            name, payload,
            start_to_close_timeout=timedelta(seconds=timeout),
            heartbeat_timeout=timedelta(seconds=30) if name == "compute_science" else None,
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3),
        )
        durations = self.state.setdefault("timings", {}).setdefault("activities_seconds", {})
        durations[name] = round(durations.get(name, 0) + (workflow.now() - started).total_seconds(), 3)
        return result

    async def publish_incrementally(self, request: dict, verified: dict) -> None:
        """Make verified measurements useful before optional enrichment finishes."""
        payload = {**request, **verified, "narrative_status": "pending", "enrichment_status": "pending"}
        self.state["result"] = await self.step("publish_science", {**payload, "timings": self.state["timings"]})
        self.state["timings"]["first_result_seconds"] = round((workflow.now() - self.started_at).total_seconds(), 3)
        self.state["result"]["timings"] = dict(self.state["timings"])
        self.state["answer_ready"] = True
        await self.checkpoint("synthesize", "running", "Measurements and the computed answer are ready. Dated weather context, interpretation, and optical imagery are loading independently.", 82)
        narrator = asyncio.create_task(self.step("narrate_findings", payload, timeout=90))
        enrichment = asyncio.create_task(self.step("enrich_science", payload, timeout=360))
        pending = {narrator, enrichment}
        try:
            while pending:
                done, pending = await workflow.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                # Stable traversal order keeps simultaneous completions deterministic.
                for task in (narrator, enrichment):
                    if task not in done:
                        continue
                    try:
                        payload.update(task.result())
                    except Exception:
                        if task is narrator:
                            payload.update(narrative_status="computed_summary", narrative_note="The language answer was unavailable; the computed summary remains available.")
                        else:
                            payload.update(enrichment_status="unavailable", enrichment={"optical_evidence": [], "optical_note": "Optional imagery enrichment did not complete. The radar measurements are unaffected."})
                    if task is narrator:
                        self.state["timings"]["interpreted_answer_seconds"] = round((workflow.now() - self.started_at).total_seconds(), 3)
                    else:
                        self.state["timings"]["enrichment_ready_seconds"] = round((workflow.now() - self.started_at).total_seconds(), 3)
                    self.state["result"] = await self.step("publish_science", {**payload, "timings": self.state["timings"]})
                    if workflow.patched("early-complete-v1"):
                        # The investigation is complete once its answer is written and checked; optical
                        # imagery keeps attaching inside the same durable workflow.
                        if task is narrator or not pending:
                            message = ("Answer ready. Optical imagery is still being attached." if enrichment in pending
                                       else "Optical imagery attached; the radar evidence is unchanged.")
                            await self.checkpoint("complete", "completed", message, 100)
                        else:
                            await self.checkpoint("synthesize", "running", "Optical imagery attached; the answer is still being written.", 92)
                    else:
                        message = "The answer is ready; optional optical imagery is still loading." if enrichment in pending else "Optional imagery processing finished; the report retains the original radar evidence."
                        await self.checkpoint("synthesize", "running", message, 92 if pending else 98)
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    @workflow.run
    async def run(self, request: dict) -> dict:
        self.started_at = workflow.now()
        self.stage_started_at = self.started_at
        self.state = {**request, "status": "queued", "stage": "discover", "progress": 0,
                      "events": [], "result": None, "blocker": None, "timings": {}, "answer_ready": False}
        try:
            if request.get("auto") and workflow.patched("planner-v1"):
                await self.checkpoint("discover", "running", "Reading the question and searching the NASA/ASF archive for this area.", 3)
                planned = await self.step("plan_investigation", request, timeout=180)
                request = {**request, "observation_ids": planned["observation_ids"], "mode": planned["mode"], "plan": planned["plan"], "context": planned["context"]}
                self.state.update(observation_ids=planned["observation_ids"], mode=planned["mode"], plan=planned["plan"], context=planned["context"])
                await self.checkpoint("discover", "running", planned["plan"]["selection"], 8)
            await self.checkpoint("discover", "running", "Checking the selected NASA–ISRO observation records.", 10)
            selection = await self.step("discover_inputs", request)
            await self.checkpoint("prepare", "running", "Reading the area of interest from the original GCOV products (NASA/ASF Earthdata Cloud).", 20)
            while True:
                prepared = await self.step("prepare_inputs", {**request, **selection}, timeout=900)
                if prepared.get("ready"):
                    break
                self.resume_requested = False
                self.state["blocker"] = prepared["reason"]
                await self.checkpoint("prepare", "blocked", prepared["reason"], 20)
                await workflow.wait_condition(lambda: self.resume_requested)
                self.state["blocker"] = None
                await self.checkpoint("prepare", "running", "Checking whether the required source files are now available.", 20)
            await self.checkpoint("analyze", "running", "Executing calibrated backscatter calculations on actual raster values.", 40)
            analysis = await self.step("compute_science", {**request, **prepared}, timeout=660)
            await self.checkpoint("verify", "running", "Checking the computed outputs, coverage, provenance, and limitations.", 75)
            verified = await self.step("verify_science", {**request, **analysis})
            if workflow.patched("incremental-results-v2"):
                await self.publish_incrementally(request, verified)
            else:
                if workflow.patched("narrator-v1"):
                    await self.checkpoint("synthesize", "running", "Writing the answer from the measurements and checking every number.", 85)
                    written = await self.step("narrate_findings", {**request, **verified}, timeout=150)
                    verified = {**verified, **written}
                await self.checkpoint("synthesize", "running", "Saving the evidence-backed report and geographic artifacts.", 95)
                self.state["result"] = await self.step("publish_science", {**request, **verified}, timeout=360)
            if self.state.get("status") != "completed":
                await self.checkpoint("complete", "completed", "Computation complete. Review the validation status and limitations in the report.", 100)
        except asyncio.CancelledError:
            self.state["blocker"] = None
            await asyncio.shield(self.checkpoint("cancelled", "cancelled", "Investigation cancelled. Completed artifacts remain in the execution history.", self.state.get("progress", 0)))
            raise
        except Exception as exc:
            # Only controlled activity errors are exposed; never include process environments.
            cause = getattr(exc, "cause", None)
            message = str(cause or exc)[:600]
            self.state["blocker"] = message
            await self.checkpoint("failed", "failed", message, self.state.get("progress", 0))
        return self.state
