"""The area watch loop, as a durable Temporal workflow (deterministic; all I/O happens in `watch` activities).

One workflow per watched area. It survives worker restarts at any point: while it sleeps until a predicted pass,
while the change investigation (a child workflow) runs, and while a proposal waits for the operator.
"""
import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_attempts=4)
DATA_LATENCY = timedelta(hours=12)       # products appear hours to days after a pass
DATA_RETRY = timedelta(hours=6)          # look again this often while waiting for the product
DATA_TRIES = 8                           # ~2 days of looking before waiting for the next pass instead
PROPOSAL_TTL = timedelta(hours=72)       # an unanswered proposal expires (the next pass brings a fresh one)


@workflow.defn
class AreaWatchWorkflow:
    def __init__(self) -> None:
        self.state: dict = {}
        self.check_requested = False
        self.decision: dict | None = None
        self.stopping = False

    @workflow.query
    def snapshot(self) -> dict:
        return self.state

    @workflow.signal
    def check_now(self) -> None:
        """Look for a new acquisition now instead of waiting for the predicted pass."""
        self.check_requested = True

    @workflow.signal
    def decide(self, decision: dict) -> None:
        """The operator's answer to a proposal: {"action": "approve" | "dismiss", "proposal": id}."""
        proposal = self.state.get("proposal") or {}
        if decision.get("proposal") == proposal.get("id") and decision.get("action") in ("approve", "dismiss"):
            self.decision = decision

    @workflow.signal
    def stop(self) -> None:
        self.stopping = True

    async def act(self, name: str, payload, minutes: int = 3):
        return await workflow.execute_activity(name, payload, start_to_close_timeout=timedelta(minutes=minutes), retry_policy=RETRY)

    async def save(self, status: str, message: str) -> None:
        now = workflow.now().isoformat()
        self.state.update(status=status, message=message, updated_at=now)
        self.state["history"] = (self.state.get("history") or [])[-29:] + [{"at": now, "status": status, "message": message}]
        await self.act("watch_save", self.state, minutes=1)

    async def sleep_until(self, when: datetime) -> None:
        """Sleep until `when`, or until the operator asks to check now or stops the watch."""
        delay = when - workflow.now()
        if delay.total_seconds() <= 0:
            return
        try:
            await workflow.wait_condition(lambda: self.check_requested or self.stopping, timeout=delay)
        except asyncio.TimeoutError:
            pass

    @workflow.run
    async def run(self, config: dict) -> dict:
        carried = config.get("state") or {}
        self.state = {**carried, "id": config["id"], "name": config["name"], "bbox": config["bbox"],
                      "investigation_id": config.get("investigation_id"), "min_changed_pct": config.get("min_changed_pct", 0.5),
                      "created_at": carried.get("created_at") or workflow.now().isoformat(), "cycles": carried.get("cycles", 0)}
        if not self.state.get("reference"):
            reference = await self.act("watch_reference", {"bbox": self.state["bbox"], "investigation_id": self.state["investigation_id"]})
            if not reference:
                await self.save("failed", "No NISAR GCOV acquisition covers this area yet, so there is nothing to compare against.")
                return self.state
            self.state["reference"] = reference
            await self.save("watching", f"Baseline: NISAR {str(reference.get('date', ''))[:10]} (track {reference.get('track')}, {str(reference.get('direction') or '').lower()}).")

        while not self.stopping:
            # 1. Wait for the next pass whose swath covers the area (or an operator's "check now").
            if not self.check_requested:
                nxt = await self.act("watch_next_pass", {"bbox": self.state["bbox"]})
                self.state["next_pass_at"] = nxt.get("at")
                when = datetime.fromisoformat(nxt["at"]) + DATA_LATENCY if nxt.get("at") else workflow.now() + timedelta(days=1)
                await self.save("waiting_pass", "Waiting for NISAR's next pass over the area." if nxt.get("at") else "No NISAR pass predicted in the next 13 days; checking again tomorrow.")
                await self.sleep_until(when)
                if self.stopping:
                    break

            # 2. Look for the new acquisition; products arrive after the pass, so keep looking for a while.
            found = None
            for _ in range(DATA_TRIES):
                self.check_requested = False
                await self.save("checking", "Searching the NASA/ASF archive for a new acquisition with the same viewing geometry.")
                self.state["last_check_at"] = workflow.now().isoformat()
                found = await self.act("watch_find_new", {"bbox": self.state["bbox"], "reference": self.state["reference"]})
                if found or self.stopping:
                    break
                await self.save("waiting_data", "No new same-track acquisition yet; NISAR products arrive hours to days after a pass.")
                await self.sleep_until(workflow.now() + DATA_RETRY)
                if self.stopping:
                    break
            if self.stopping:
                break
            if not found:
                continue

            # 3. Measure the change against the previous acquisition (a child investigation, durable on its own).
            request = await self.act("watch_register_investigation", {"watch_id": self.state["id"], "name": self.state["name"], "bbox": self.state["bbox"],
                                                                         "reference": self.state["reference"], "new": found})
            self.state["latest_investigation_id"] = request["id"]
            await self.save("investigating", f"Measuring the change from {str(self.state['reference'].get('date', ''))[:10]} to {str(found.get('date', ''))[:10]}.")
            await workflow.execute_child_workflow("InvestigationWorkflow", request, id=request["id"], task_queue=workflow.info().task_queue,
                                                  execution_timeout=timedelta(days=30))
            assessment = await self.act("watch_assess", {"investigation_id": request["id"], "min_changed_pct": self.state["min_changed_pct"]})
            self.state["assessment"] = assessment
            self.state["reference"] = found
            self.state["cycles"] = self.state.get("cycles", 0) + 1

            # 4. Significant change: propose a dispatch and wait for the operator (never dispatch on our own).
            if assessment.get("significant"):
                self.state["proposal"] = {"id": request["id"], "investigation_id": request["id"], "summary": assessment["summary"],
                                          "patches": assessment.get("patches"), "at": workflow.now().isoformat()}
                self.decision = None
                await self.save("proposal", f"Significant change: {assessment['summary']} Send drones to verify?")
                try:
                    await workflow.wait_condition(lambda: self.decision is not None or self.stopping, timeout=PROPOSAL_TTL)
                except asyncio.TimeoutError:
                    pass
                if self.decision and self.decision["action"] == "approve":
                    out = await self.act("watch_dispatch", {"investigation_id": request["id"]})
                    self.state["dispatched"] = {**out, "investigation_id": request["id"], "at": workflow.now().isoformat()}
                    await self.save("dispatched", f"Approved: the fleet is verifying {out.get('targets')} patches.")
                elif self.decision and self.decision["action"] == "dismiss":
                    await self.save("dismissed", "Proposal dismissed by the operator.")
                elif not self.stopping:
                    await self.save("expired", "The proposal expired without an answer.")
                self.state["proposal"] = None
                self.decision = None
            else:
                await self.save("watching", f"No significant change: {assessment.get('summary', '')}")

            # 5. Keep the event history short for a watch that runs for months.
            if workflow.info().get_current_history_length() > 4000:
                workflow.continue_as_new({**config, "state": self.state})

        await self.save("stopped", "Watch stopped by the operator.")
        return self.state
