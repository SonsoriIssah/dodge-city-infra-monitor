"""Stage 4 - place the simulated sensors and ingest their readings.

    python scripts/generate_sensors.py

Thin wrapper: puts the repository root on sys.path and runs the stage registered as "generate" in
``pipeline.stages`` (imported lazily; a stage that is not implemented yet exits with a clear message).
"""
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the "generate" stage with the given arguments (default: the command line)."""
    from pipeline.stages import run_stage

    return run_stage("generate", argv)


if __name__ == "__main__":
    raise SystemExit(main())
