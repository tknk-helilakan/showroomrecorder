from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RoomConfig


@dataclass
class SubtitleSegment:
    index: int
    start: float
    end: float
    text: str
    translation: str | None = None


@dataclass
class LiveSession:
    room: RoomConfig
    job_id: str
    started_at: datetime
    live_title: str
    work_dir: Path
    ended_at: datetime | None = None
    raw_file: Path | None = None
    mp4_file: Path | None = None
    ja_srt_file: Path | None = None
    zh_srt_file: Path | None = None
    danmaku_ass_file: Path | None = None
    danmaku_jsonl_file: Path | None = None
    upload_file: Path | None = None
    bvid: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
