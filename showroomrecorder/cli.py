from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import load_config
from .logging_setup import setup_logging
from .runner import ShowroomRecorderService

DEFAULT_CONFIG = r"E:\helilokan\Test\showroomrecord\config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="showroomrecorder",
        description="Watch SHOWROOM rooms, record lives, transcode, subtitle, translate, and upload.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to the YAML config file. Defaults to {DEFAULT_CONFIG}.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check all rooms once and exit unless a live recording starts.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))
    setup_logging(config.service.log_level, config.paths.logs_dir)
    service = ShowroomRecorderService(config)
    asyncio.run(service.run(once=args.once))
