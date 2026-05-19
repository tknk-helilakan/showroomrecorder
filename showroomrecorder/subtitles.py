from __future__ import annotations

import json
import re
from pathlib import Path

from .models import SubtitleSegment


def format_srt_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    millis = int(round(seconds * 1000))
    hours, remainder = divmod(millis, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def clean_subtitle_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def split_lines(text: str, max_chars: int) -> str:
    text = clean_subtitle_text(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    chunks: list[str] = []
    current = ""
    for char in text:
        if len(current) >= max_chars:
            chunks.append(current)
            current = char
        else:
            current += char
    if current:
        chunks.append(current)
    return "\n".join(chunks)


def write_srt(
    path: Path,
    segments: list[SubtitleSegment],
    *,
    language: str,
    max_line_chars: int = 24,
    bilingual: bool = False,
) -> None:
    lines: list[str] = []
    for idx, segment in enumerate(segments, start=1):
        text = segment.text
        if language == "zh":
            text = segment.translation or segment.text
            if bilingual and segment.translation:
                text = f"{segment.translation}\n{segment.text}"
        lines.extend(
            [
                str(idx),
                f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}",
                split_lines(text, max_line_chars),
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def write_transcript_json(path: Path, segments: list[SubtitleSegment]) -> None:
    payload = [
        {
            "index": item.index,
            "start": item.start,
            "end": item.end,
            "text": item.text,
            "translation": item.translation,
        }
        for item in segments
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def to_bilibili_subtitle_json(segments: list[SubtitleSegment]) -> dict:
    return {
        "font_size": 0.4,
        "font_color": "#FFFFFF",
        "background_alpha": 0.5,
        "background_color": "#9C27B0",
        "Stroke": "none",
        "body": [
            {
                "from": round(item.start, 3),
                "to": round(item.end, 3),
                "location": 2,
                "content": item.translation or item.text,
            }
            for item in segments
            if (item.translation or item.text).strip()
        ],
    }
