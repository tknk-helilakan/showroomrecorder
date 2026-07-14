from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .config import load_config
from .logging_setup import setup_logging
from .runner import ShowroomRecorderService

DEFAULT_CONFIG = r"E:\helilokan\Test\showroomrecord\config.yaml"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
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
    parser.add_argument(
        "--process-raw",
        type=Path,
        help="Process, subtitle, and upload an existing raw recording file.",
    )
    parser.add_argument(
        "--room",
        help="Room name or room_id for --process-raw. Defaults to inferring from the raw path.",
    )
    parser.add_argument(
        "--started-at",
        help="ISO timestamp override for --process-raw, for example 2026-06-03T21:22:03.",
    )
    parser.add_argument(
        "--ended-at",
        help="ISO timestamp override for --process-raw. Defaults to the raw file modified time.",
    )
    parser.add_argument(
        "--title",
        help="Live title override for --process-raw. Defaults to the configured room name.",
    )
    parser.add_argument(
        "--yt-dlp-worker",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--streamlink-worker",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    if args.yt_dlp_worker is not None:
        _run_yt_dlp_worker(args.yt_dlp_worker)
        return
    if args.streamlink_worker is not None:
        _run_streamlink_worker(args.streamlink_worker)
        return

    config = load_config(Path(args.config))
    setup_logging(config.service.log_level, config.paths.logs_dir)
    service = ShowroomRecorderService(config)
    if args.process_raw:
        service.process_existing_recording(
            args.process_raw,
            room_ref=args.room,
            started_at=_parse_datetime(args.started_at),
            ended_at=_parse_datetime(args.ended_at),
            title=args.title,
        )
        return
    asyncio.run(service.run(once=args.once))


def _run_yt_dlp_worker(arguments: Sequence[str]) -> None:
    from yt_dlp import main as yt_dlp_main

    yt_dlp_main(list(arguments))


def _run_streamlink_worker(arguments: Sequence[str]) -> None:
    from streamlink_cli.main import main as streamlink_main

    original_argv = sys.argv
    sys.argv = ["streamlink", *arguments]
    try:
        streamlink_main()
    finally:
        sys.argv = original_argv


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)
