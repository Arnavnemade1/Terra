"""Trend orchestration: one coordinator workflow per area and one child agent per tile.

The coordinator plans the dates, acquires the originals, builds the shared quantized dataset, starts tile
agents and publishes the result. It is deterministic: no I/O, no clock reads outside `workflow.now`, no random
numbers and no numpy. Every activity is imported by name so this module never imports the science stack.
"""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_attempts=4)
TILE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_attempts=3)
PLAN_TIMEOUT = timedelta(minutes=3)
ACQUIRE_TIMEOUT = timedelta(minutes=6)
STACK_TIMEOUT = timedelta(minutes=10)
TILE_TIMEOUT = timedelta(minutes=10)
PUBLISH_TIMEOUT = timedelta(minutes=5)
MAX_ACQUIRE = 3
MAX_TILE_AGENTS = 3
MIN_DATES = 4


@workflow.defn
class TrendTileAgentWorkflow:
    """One agent per tile: fits the robust trend of its block and reports the tuned Huber delta."""

    def __init__(self) -> None:
        self.state: dict = {}

    @workflow.query
    def snapshot(self) -> dict:
        return self.state

    @workflow.run
    async def run(self, payload: dict) -> dict:
        self.state = {"index": payload["tile"]["index"], "status": "fitting", "delta": None}
        result = await workflow.execute_activity("trend_fit_tile", payload, start_to_close_timeout=TILE_TIMEOUT,
                                                 retry_policy=TILE_RETRY)
        self.state = {**self.state, "status": "done", "delta": result.get("delta"),
                      "pixels_fit": result.get("pixels_fit"), "median_slope": result.get("median_slope")}
        return result


@workflow.defn
class TrendWorkflow:
    """Coordinator for one trend area: plan, acquire, stack, tile agents, publish."""

    def __init__(self) -> None:
        self.state: dict = {}

    @workflow.query
    def snapshot(self) -> dict:
        return self.state

    async def act(self, name: str, payload, timeout: timedelta, retry: RetryPolicy = RETRY):
        return await workflow.execute_activity(name, payload, start_to_close_timeout=timeout, retry_policy=retry)

    def failed(self, reason: str) -> dict:
        self.state.update(status="failed", message=str(reason or "The analysis stopped without a reason."))
        return self.state

    @workflow.run
    async def run(self, config: dict) -> dict:
        identifier = config["id"]
        self.state = {
            "id": identifier,
            "name": config.get("name") or "Trend analysis",
            "bbox": config["bbox"],
            "status": "running",
            "stage": "planning",
            "message": "Searching the archive for acquisitions with one viewing geometry.",
            "dates": [],
            "agents": [],
            "result": None,
        }

        # 1. Plan: one geometry group, at least four dates, k dates chosen by the QUBO.
        plan = await self.act("trend_plan", {"bbox": config["bbox"], "max_dates": int(config.get("max_dates") or 10)},
                              PLAN_TIMEOUT)
        if not plan.get("ok"):
            return self.failed(plan.get("reason"))
        observations = plan.get("observations") or []
        self.state["dates"] = [str(observation.get("date") or "") for observation in observations]
        self.state.update(stage="acquiring",
                          message=f"Reading {len(observations)} original products for the shared stack.")

        # 2. Acquire, at most three at a time. One bad date is reported and excluded, not raised.
        acquired: list[dict] = []
        for start in range(0, len(observations), MAX_ACQUIRE):
            batch = observations[start:start + MAX_ACQUIRE]
            acquired.extend(await asyncio.gather(*(
                self.act("trend_acquire", {"observation": observation, "bbox": config["bbox"]}, ACQUIRE_TIMEOUT)
                for observation in batch)))
        inputs = [{"path": result["path"], "date": result.get("date")} for result in acquired if result.get("ok")]
        failures = [str(result.get("reason") or "") for result in acquired if not result.get("ok")]
        if len(inputs) < MIN_DATES:
            reason = (failures[0] if failures else
                      f"Only {len(inputs)} acquisition(s) could be read; a trend needs at least {MIN_DATES}.")
            return self.failed(reason)

        # 3. Build the one quantized dataset every agent reads.
        self.state.update(stage="stacking", message="Writing the shared 8-bit stack date by date.")
        stack = await self.act("trend_build_stack", {"id": identifier, "bbox": config["bbox"], "inputs": inputs},
                               STACK_TIMEOUT)
        if not stack.get("ok"):
            return self.failed(stack.get("reason"))
        tiles = stack.get("tiles") or []
        self.state.update(stage="agents",
                          message=f"Fitting {len(tiles)} tile(s) across {len(stack.get('dates') or [])} dates.",
                          agents=[{"index": tile["index"], "status": "queued", "delta": None} for tile in tiles])

        # 4. One child workflow per tile, at most three running at once, updating state as each finishes.
        async def run_tile(tile: dict) -> dict:
            for agent in self.state["agents"]:
                if agent["index"] == tile["index"]:
                    agent["status"] = "fitting"
            result = await workflow.execute_child_workflow(
                TrendTileAgentWorkflow.run, {"id": identifier, "tile": tile},
                id=f"{identifier}-tile-{tile['index']}", task_queue=workflow.info().task_queue,
                execution_timeout=timedelta(minutes=30))
            for agent in self.state["agents"]:
                if agent["index"] == tile["index"]:
                    agent.update(status="done", delta=result.get("delta"))
            return result

        fitted: list[dict] = []
        for start in range(0, len(tiles), MAX_TILE_AGENTS):
            batch = tiles[start:start + MAX_TILE_AGENTS]
            fitted.extend(await asyncio.gather(*(run_tile(tile) for tile in batch)))

        # 5. Publish the assembled layers and the result document.
        self.state.update(stage="publishing", message="Assembling the tiles and exporting the trend layers.")
        published = await self.act("trend_publish",
                                   {"id": identifier, "tiles": fitted, "plan": plan, "stack": stack}, PUBLISH_TIMEOUT)
        if not published.get("ok"):
            return self.failed(published.get("reason"))
        summary = published.get("summary") or {}
        self.state.update(status="done", stage="done", result=published,
                          message=(f"Trend over {len(published.get('dates') or [])} dates: median "
                                   f"{summary.get('median_slope_db_per_year')} dB/year."))
        return self.state


WORKFLOWS = [TrendWorkflow, TrendTileAgentWorkflow]
