"""Run a persistent Temporal dev server via the Python SDK when the CLI is not installed."""
import asyncio
import os
from pathlib import Path

from temporalio.testing import WorkflowEnvironment

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    data = ROOT / ".data"
    data.mkdir(exist_ok=True)
    (data / "temporal-bin").mkdir(exist_ok=True)
    env = await WorkflowEnvironment.start_local(
        port=int(os.getenv("TERRA_TEMPORAL_PORT", "7233")),
        ui=True,
        ui_port=8233,
        download_dest_dir=str(data / "temporal-bin"),
        dev_server_database_filename=str(data / "temporal.db"),
        dev_server_log_level="warn",
    )
    print("Temporal dev server ready on 127.0.0.1:7233 (UI on 8233)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await env.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
