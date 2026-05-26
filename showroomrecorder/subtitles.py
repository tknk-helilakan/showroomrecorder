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


BILIBILI_MAX_CONTENT_CHARS = 80
BILIBILI_MAX_DURATION_SECONDS = 10.0


def _split_bilibili_text(text: str, max_chars: int = BILIBILI_MAX_CONTENT_CHARS) -> list[str]:
    text = clean_subtitle_text(text)
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def _bilibili_subtitle_entries(segment: SubtitleSegment, max_end: float | None = None) -> list[dict]:
    content = segment.translation or segment.text
    chunks = _split_bilibili_text(content)
    if not chunks:
        return []

    start = max(0.0, float(segment.start))
    if max_end is not None and start >= max_end:
        return []
    end = max(start + 0.2, float(segment.end))
    if max_end is not None:
        end = min(end, max_end)
        if end <= start:
            return []
    duration = min(end - start, BILIBILI_MAX_DURATION_SECONDS * len(chunks))

    entries: list[dict] = []
    for index, chunk in enumerate(chunks):
        chunk_start = start + duration * index / len(chunks)
        chunk_end = start + duration * (index + 1) / len(chunks)
        entries.append(
            {
                "from": round(chunk_start, 3),
                "to": round(chunk_end, 3),
                "location": 2,
                "content": chunk,
            }
        )
    return entries


def to_bilibili_subtitle_json(segments: list[SubtitleSegment], max_end: float | None = None) -> dict:
    body: list[dict] = []
    for item in segments:
        body.extend(_bilibili_subtitle_entries(item, max_end=max_end))
    return {
        "font_size": 0.4,
        "font_color": "#FFFFFF",
        "background_alpha": 0.5,
        "background_color": "#9C27B0",
        "Stroke": "none",
        "body": body,
    }
