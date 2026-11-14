"""Fixed-entrypoint compute process. No user-provided Python is executed."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .science import ScienceError, run_analysis


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded NISAR GCOV analysis")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        run_analysis(manifest, args.output_dir)
    except (ScienceError, OSError, ValueError, KeyError) as exc:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        error = {"status": "failed", "error": {"code": getattr(exc, "code", "processing_error"), "message": str(exc)}}
        (args.output_dir / "result.json").write_text(json.dumps(error, indent=2))
        print(json.dumps(error))
        return 1
    print(json.dumps({"status": "completed", "result": "result.json"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
