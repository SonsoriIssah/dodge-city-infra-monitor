"""One-command pipeline: download -> process -> seed -> generate -> detect -> analyze -> export.

    python run_pipeline.py                    # all seven stages; cached raw data is reused
    python run_pipeline.py --refresh          # download the raw data again, then rebuild everything
    python run_pipeline.py --skip-download    # use the committed data/raw cache (no network)
    python run_pipeline.py --skip-export      # stop before the static dashboard snapshot
    python run_pipeline.py --only detect      # a single stage
    python run_pipeline.py --from generate    # this stage and every later one

Stages run in-process, in order, and the run stops at the first stage that returns a non-zero exit code.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.logging_utils import setup_logging  # noqa: E402
from pipeline.stages import STAGE_NAMES, STAGES, Stage, get_stage, run_stage  # noqa: E402

logger = logging.getLogger("run_pipeline")


def _stage_arg(value: str) -> Stage:
    try:
        return get_stage(value)
    except KeyError as exc:
        raise argparse.ArgumentTypeError(str(exc.args[0])) from exc


def select_stages(args: argparse.Namespace) -> list[Stage]:
    """Stages to run for the parsed command line, in pipeline order."""
    if args.only is not None:
        return [args.only]
    selected = [stage for stage in STAGES if args.from_stage is None or stage.number >= args.from_stage.number]
    if args.skip_download:
        selected = [stage for stage in selected if stage.name != "download"]
    if args.skip_export:
        selected = [stage for stage in selected if stage.name != "export"]
    return selected


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected stages; returns 0 or the exit code of the first failing stage."""
    parser = argparse.ArgumentParser(
        prog="run_pipeline",
        description="Run the data pipeline stages in order: " + " -> ".join(STAGE_NAMES),
    )
    parser.add_argument("--refresh", action="store_true", help="download the raw data again (stage 1)")
    parser.add_argument("--skip-download", action="store_true", help="skip stage 1 and use the cached data/raw files")
    parser.add_argument("--skip-export", action="store_true", help="skip stage 7 (static dashboard snapshot)")
    parser.add_argument("--only", type=_stage_arg, metavar="STAGE", help="run a single stage (name or number)")
    parser.add_argument(
        "--from", dest="from_stage", type=_stage_arg, metavar="STAGE", help="run this stage and every later one"
    )
    args = parser.parse_args(argv)
    if args.only is not None and args.from_stage is not None:
        parser.error("--only and --from cannot be combined")
    if args.refresh and args.skip_download:
        parser.error("--refresh and --skip-download cannot be combined")

    setup_logging()
    stages = select_stages(args)
    if not stages:
        logger.error("no stage selected")
        return 1
    logger.info("pipeline: %s", " -> ".join(stage.name for stage in stages))
    started = time.perf_counter()
    for stage in stages:
        stage_args = ["--refresh"] if stage.name == "download" and args.refresh else []
        logger.info("[%d/%d] %s: %s", stage.number, len(STAGES), stage.name, stage.description)
        t0 = time.perf_counter()
        code = run_stage(stage.name, stage_args)
        if code != 0:
            logger.error("pipeline stopped: stage %d '%s' exited with code %d", stage.number, stage.name, code)
            return code
        logger.info(
            "[%d/%d] %s finished in %.1f s", stage.number, len(STAGES), stage.name, time.perf_counter() - t0
        )
    logger.info("pipeline finished in %.1f s", time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
