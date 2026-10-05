"""Pipeline stages and the registry the command-line wrappers use.

Each stage is a module with ``main(argv=None) -> int`` (0 = success). Stage modules are imported lazily by
name, so a stage that has not been written yet is reported as "not available yet" instead of breaking the
other entry points.

    1 download  scripts/download_data.py      pipeline.stages.download
    2 process   scripts/process_data.py       pipeline.stages.process
    3 seed      scripts/seed_database.py      pipeline.stages.seed
    4 generate  scripts/generate_sensors.py   pipeline.stages.generate
    5 detect    scripts/detect_anomalies.py   pipeline.stages.detect
    6 analyze   scripts/analyze_spatial.py    pipeline.stages.analyze
    7 export    scripts/export_static.py      backend.export
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pipeline.logging_utils import setup_logging

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DATABASE_UNAVAILABLE = 2
EXIT_STAGE_UNAVAILABLE = 3


@dataclass(frozen=True, slots=True)
class Stage:
    """One pipeline stage."""

    number: int
    name: str
    module: str
    script: str
    description: str


STAGES: tuple[Stage, ...] = (
    Stage(1, "download", "pipeline.stages.download", "download_data", "download / refresh the raw GIS data"),
    Stage(2, "process", "pipeline.stages.process", "process_data", "clip and classify the GIS data"),
    Stage(3, "seed", "pipeline.stages.seed", "seed_database", "migrate the database and load the GIS layers"),
    Stage(4, "generate", "pipeline.stages.generate", "generate_sensors", "place sensors and ingest readings"),
    Stage(5, "detect", "pipeline.stages.detect", "detect_anomalies", "run the anomaly detection"),
    Stage(6, "analyze", "pipeline.stages.analyze", "analyze_spatial", "clusters, risk zones, asset health"),
    Stage(7, "export", "backend.export", "export_static", "write the static dashboard snapshot"),
)
STAGE_NAMES: tuple[str, ...] = tuple(stage.name for stage in STAGES)


def get_stage(key: str | int) -> Stage:
    """Look a stage up by name ('seed'), script name ('seed_database') or number (3)."""
    text = str(key).strip().lower().removesuffix(".py")
    for stage in STAGES:
        if text in (stage.name, stage.script, str(stage.number)):
            return stage
    raise KeyError(f"unknown stage {key!r}; expected one of: {', '.join(STAGE_NAMES)}")


def load_stage_main(stage: Stage) -> Callable[[Sequence[str] | None], int] | None:
    """Import the stage module and return its ``main``; None when the module does not exist yet.

    Only a missing stage module (or its missing parent package) counts as "not available"; an import error
    raised from inside an existing stage module is a real defect and propagates.
    """
    try:
        module = importlib.import_module(stage.module)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == stage.module or stage.module.startswith(missing + "."):
            return None
        raise
    main = getattr(module, "main", None)
    if not callable(main):
        raise AttributeError(f"{stage.module} has no main(argv) function")
    return main


def run_stage(key: str | int, argv: Sequence[str] | None = None) -> int:
    """Run one stage in-process and return its exit code.

    ``argv`` is the stage's own argument list; None means "use the process command line" (script wrappers),
    an empty list means "defaults" (the orchestrator).
    """
    setup_logging()
    stage = get_stage(key)
    main = load_stage_main(stage)
    if main is None:
        logger.error(
            "stage %d '%s' is not available yet: module %s has not been implemented (scripts/%s.py)",
            stage.number, stage.name, stage.module, stage.script,
        )  # fmt: skip
        return EXIT_STAGE_UNAVAILABLE
    try:
        code = main(argv)
    except KeyboardInterrupt:
        logger.error("stage %d '%s' interrupted", stage.number, stage.name)
        return 130
    except Exception:
        logger.exception("stage %d '%s' failed", stage.number, stage.name)
        return EXIT_FAILED
    return int(code or 0)
