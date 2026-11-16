"""Run the scientific worker separately from the HTTP server."""
import asyncio
import logging
import os
from datetime import timedelta
from temporalio.client import Client
from temporalio.worker import Worker
from .config import get_settings
from .runtime_health import write_heartbeat
from .workflows import InvestigationWorkflow
from .fleet.workflows import DroneAgentWorkflow, FleetCoordinatorWorkflow
from .fleet.activities import coordination_round, drone_agent_round
from .watch import ACTIVITIES as WATCH_ACTIVITIES
from .watch_workflows import AreaWatchWorkflow
from .trend_workflows import WORKFLOWS as TREND_WORKFLOWS
from .trend_activities import ACTIVITIES as TREND_ACTIVITIES
from .activities import (persist_snapshot, discover_inputs, prepare_inputs, compute_science, verify_science,
                         publish_science, plan_investigation, narrate_findings, enrich_science)


async def main() -> None:
    settings = get_settings()
    settings.ensure_directories()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = Worker(client, task_queue=settings.task_queue, workflows=[InvestigationWorkflow, FleetCoordinatorWorkflow, DroneAgentWorkflow, AreaWatchWorkflow, *TREND_WORKFLOWS],
                    activities=[persist_snapshot, discover_inputs, prepare_inputs, compute_science,
                                verify_science, publish_science, plan_investigation, narrate_findings, enrich_science,
                                coordination_round, drone_agent_round, *WATCH_ACTIVITIES, *TREND_ACTIVITIES], max_concurrent_activities=int(os.getenv('TERRA_MAX_ACTIVITIES', '12')),
                    max_concurrent_workflow_tasks=int(os.getenv('TERRA_MAX_WORKFLOW_TASKS', '8')),
                    graceful_shutdown_timeout=timedelta(seconds=20))
    logging.info("TERRA scientific worker ready on queue %s", settings.task_queue)
    from .connector import close
    from .feeds import keep_warm
    warm = asyncio.create_task(keep_warm())  # live context is ready before the first investigation asks
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(5)
            await asyncio.to_thread(write_heartbeat, settings.data_dir)
    pulse = asyncio.create_task(heartbeat())
    try:
        await worker.run()
    finally:
        warm.cancel()
        pulse.cancel()
        await asyncio.gather(warm, pulse, return_exceptions=True)
        await close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for noisy in ("fsspec", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main())
