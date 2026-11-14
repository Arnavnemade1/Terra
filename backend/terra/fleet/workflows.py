"""Durable fleet agents (Temporal workflows). No I/O here: every read and write goes through activities.

FleetCoordinatorWorkflow (one, id "fleet-coordinator")
    Every few seconds, or at once when an agent or a person signals, runs a coordination round: jobs from the
    monitoring targets, an auction over the agents' bids, airspace separation, relief for low batteries, relays
    for weak links. Starts a DroneAgentWorkflow for any aircraft without a live agent.

DroneAgentWorkflow (one per aircraft, id "drone-agent-<id>")
    Watches one aircraft: bids on every open job from its own position, battery and sensors; checks health and
    conformance to its reserved airspace; reports changes to the shared message log; signals the coordinator
    when something needs a decision now. Retires after the aircraft has been silent for ten minutes.

Both continue as new periodically so their histories stay small, and both survive restarts of any process.
"""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError

COORDINATOR_ID = "fleet-coordinator"
ROUND_SECONDS = 4
AGENT_SECONDS = 3
RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=5), maximum_attempts=3)


@workflow.defn
class DroneAgentWorkflow:
    def __init__(self) -> None:
        self.last: dict = {}

    @workflow.query
    def status(self) -> dict:
        return self.last

    @workflow.run
    async def run(self, drone_id: str, start_round: int = 0) -> str:
        n = start_round
        while True:
            try:
                result = await workflow.execute_activity("drone_agent_round", {"drone_id": drone_id, "round": n, "workflow_id": workflow.info().workflow_id},
                                                         start_to_close_timeout=timedelta(seconds=20), retry_policy=RETRY)
            except ActivityError:
                result = {}
            self.last = result; n += 1
            if result.get("retire"):
                return f"{drone_id}: retired after losing contact"
            if result.get("urgent"):
                try:
                    await workflow.get_external_workflow_handle(COORDINATOR_ID).signal("nudge", f"{drone_id}: {result.get('reason', 'needs a decision')}")
                except Exception:  # the coordinator may be restarting; the next round still sees the report
                    pass
            if n - start_round >= 400:
                workflow.continue_as_new(args=[drone_id, n])
            await asyncio.sleep(AGENT_SECONDS)


@workflow.defn
class FleetCoordinatorWorkflow:
    def __init__(self) -> None:
        self.nudged = False
        self.stopping = False
        self.reasons: list[str] = []
        self.last: dict = {}
        self.round = 0

    @workflow.signal
    def nudge(self, reason: str) -> None:
        self.nudged = True
        self.reasons = (self.reasons + [str(reason)[:200]])[-20:]

    @workflow.signal
    def stop(self) -> None:
        self.stopping = True

    @workflow.query
    def status(self) -> dict:
        return {"round": self.round, "last": self.last, "pending": self.reasons[-5:]}

    @workflow.run
    async def run(self, start_round: int = 0) -> str:
        self.round = start_round
        while not self.stopping:
            reasons, self.reasons = self.reasons, []
            try:
                result = await workflow.execute_activity("coordination_round", {"round": self.round, "reasons": reasons, "workflow_id": workflow.info().workflow_id},
                                                         start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
            except ActivityError:
                result = {}
            self.last = result.get("summary", {}); self.round += 1
            for drone_id in result.get("need_agents", []):
                try:
                    await workflow.start_child_workflow(DroneAgentWorkflow.run, args=[drone_id, 0], id=f"drone-agent-{drone_id}",
                                                        parent_close_policy=workflow.ParentClosePolicy.ABANDON)
                except WorkflowAlreadyStartedError:
                    pass
            if self.round - start_round >= 250:
                workflow.continue_as_new(self.round)
            try:
                await workflow.wait_condition(lambda: self.nudged or self.stopping, timeout=timedelta(seconds=ROUND_SECONDS))
            except asyncio.TimeoutError:
                pass
            self.nudged = False
        return "stopped"
