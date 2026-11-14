"""Add dated optical context to completed local investigations without rerunning radar science."""
import asyncio
import json
import os
from pathlib import Path
import sys

from terra.config import get_settings
from terra.optical import collect_optical_evidence
from terra.store import Store


async def main(ids: list[str]) -> None:
    settings = get_settings()
    store = Store(settings.database)
    for identifier in ids:
        item = store.investigation(identifier)
        if not item or item.get("status") != "completed" or not isinstance(item.get("result"), dict):
            print(f"{identifier}: no completed result")
            continue
        directory = settings.artifacts / identifier
        if not directory.is_dir() or not directory.resolve().is_relative_to(settings.artifacts.resolve()):
            print(f"{identifier}: artifact directory unavailable")
            continue
        result = item["result"]
        optical = await collect_optical_evidence(result, directory)
        result["optical_evidence"] = optical
        base = f"/api/investigations/{identifier}/artifacts"
        previous = {entry["name"]: entry for entry in result.get("artifacts", [])
                    if not entry["name"].startswith("optical-")}
        for entry in optical:
            name = entry["artifact_name"]
            previous[name] = {"name": name, "url": f"{base}/{name}", "size_bytes": (directory / name).stat().st_size}
        result["artifacts"] = list(previous.values())
        target = directory / "public-result.json"
        tmp = directory / ".public-result-optical.tmp"
        tmp.write_text(json.dumps(result, indent=2, allow_nan=False))
        os.replace(tmp, target)
        store.save_investigation(item)
        print(f"{identifier}: archived {len(optical)} suitable optical scenes " + ", ".join(f"{x['kind']} {x['date'][:10]}" for x in optical))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
