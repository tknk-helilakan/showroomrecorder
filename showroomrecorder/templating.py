from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

INVALID_FILENAME_CHARS = r'<>:"/\|?*'


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def slugify(value: str, max_length: int = 80) -> str:
    value = value.strip()
    for char in INVALID_FILENAME_CHARS:
        value = value.replace(char, "_")
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._ ")
    return value[:max_length] or "untitled"


def render_template(template: str, context: dict[str, Any]) -> str:
    return template.format_map(SafeDict(context))


def build_context(
    *,
    streamer: str,
    room_url: str,
    room_id: int | None,
    title: str,
    started_at: datetime,
    ended_at: datetime | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    title = title or streamer
    return {
        "streamer": streamer,
        "streamer_slug": slugify(streamer),
        "room_url": room_url,
        "room_id": room_id or "",
        "title": title,
        "title_slug": slugify(title),
        "started_at": started_at,
        "ended_at": ended_at or "",
        "job_id": job_id,
    }


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}{suffix}"
    if not path.exists():
        return path
    for idx in range(1, 1000):
        candidate = directory / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create a unique path for {path}")

