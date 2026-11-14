"""Run the scientific worker separately from the HTTP server."""
import asyncio
import logging
from temporalio.client import Client
from temporalio.worker import Worker
from .config import get_settings
from .workflows import InvestigationWorkflow
from .fleet.workflows import DroneAgentWorkflow, FleetCoordinatorWorkflow
from .fleet.activities import coordination_round, drone_agent_round
from .activities import (persist_snapshot, discover_inputs, prepare_inputs, compute_science, verify_science,
                         publish_science, plan_investigation, narrate_findings, enrich_science)


async def main() -> None:
    settings = get_settings()
    settings.ensure_directories()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = Worker(client, task_queue=settings.task_queue, workflows=[InvestigationWorkflow, FleetCoordinatorWorkflow, DroneAgentWorkflow],
                    activities=[persist_snapshot, discover_inputs, prepare_inputs, compute_science,
                                verify_science, publish_science, plan_investigation, narrate_findings, enrich_science,
                                coordination_round, drone_agent_round], max_concurrent_activities=12,
                    max_concurrent_workflow_tasks=8)
    logging.info("TERRA scientific worker ready on queue %s", settings.task_queue)
    from .connector import close
    from .feeds import keep_warm
    warm = asyncio.create_task(keep_warm())  # live context is ready before the first investigation asks
    try:
        await worker.run()
    finally:
        warm.cancel()
        await close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for noisy in ("fsspec", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main())
