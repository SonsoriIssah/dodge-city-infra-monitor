"""Stage 2 - clip and classify the raw GIS data into data/processed (no database access).

    python scripts/process_data.py

Thin wrapper: puts the repository root on sys.path and runs the stage registered as "process" in
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
    """Run the "process" stage with the given arguments (default: the command line)."""
    from pipeline.stages import run_stage

    return run_stage("process", argv)


if __name__ == "__main__":
    raise SystemExit(main())
