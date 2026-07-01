from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from .config import AppConfig
from .models import LiveSession
from .showroom import ShowroomClient
from .templating import slugify

LOGGER = logging.getLogger(__name__)


@dataclass
class DanmakuEntry:
    index: int
    offset: float
    timestamp: float
    text: str
    user_name: str = ""
    user_id: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class DanmakuCaptureResult:
    ass_file: Path | None
    jsonl_file: Path | None
    count: int


class DanmakuRecorder:
    def __init__(self, config: AppConfig, showroom: ShowroomClient) -> None:
        self.config = config
        self.showroom = showroom

    def capture(self, session: LiveSession, stop_event: Event) -> DanmakuCaptureResult:
        cfg = self.config.danmaku
        if not cfg.enabled:
            return DanmakuCaptureResult(ass_file=None, jsonl_file=None, count=0)

        output_dir = self.config.paths.danmaku_dir / slugify(session.room.name) / session.job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        jsonl_file = output_dir / f"{session.job_id}.danmaku.jsonl"
        ass_file = output_dir / f"{session.job_id}.danmaku.ass"

        started_ts = session.started_at.timestamp()
        started_monotonic = time.monotonic()
        entries: list[DanmakuEntry] = []
        seen: set[str] = set()
        failures = 0

        LOGGER.info("Starting danmaku capture for %s job=%s", session.room.name, session.job_id)
        with jsonl_file.open("w", encoding="utf-8") as fh:
            while not stop_event.is_set():
                added = self._collect_once(
                    session=session,
                    started_ts=started_ts,
                    started_monotonic=started_monotonic,
                    entries=entries,
                    seen=seen,
                    output=fh,
                )
                if added >= 0:
                    failures = 0
                else:
                    failures += 1
                    if failures == 1 or failures % 30 == 0:
                        LOGGER.warning(
                            "Danmaku capture request failed for %s job=%s; consecutive=%d",
                            session.room.name,
                            session.job_id,
                            failures,
                        )
                if cfg.max_entries and len(entries) >= cfg.max_entries:
                    LOGGER.warning(
                        "Danmaku capture reached max_entries=%d for %s job=%s",
                        cfg.max_entries,
                        session.room.name,
                        session.job_id,
                    )
                    break
                stop_event.wait(cfg.poll_seconds)

            self._collect_once(
                session=session,
                started_ts=started_ts,
                started_monotonic=started_monotonic,
                entries=entries,
                seen=seen,
                output=fh,
            )

        entries.sort(key=lambda item: (item.offset, item.index))
        self.write_ass(ass_file, entries)
        LOGGER.info(
            "Danmaku capture saved %d comment(s): jsonl=%s ass=%s",
            len(entries),
            jsonl_file,
            ass_file,
        )
        return DanmakuCaptureResult(ass_file=ass_file, jsonl_file=jsonl_file, count=len(entries))

    def _collect_once(
        self,
        *,
        session: LiveSession,
        started_ts: float,
        started_monotonic: float,
        entries: list[DanmakuEntry],
        seen: set[str],
        output,
    ) -> int:
        try:
            raw_items = self.showroom.get_comment_log(
                session.room,
                timeout_seconds=self.config.danmaku.request_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("SHOWROOM comment_log request failed for %s: %s", session.room.name, exc)
            return -1

        added = 0
        for raw in raw_items:
            if self.config.danmaku.max_entries and len(entries) >= self.config.danmaku.max_entries:
                break
            entry = self._entry_from_raw(
                raw,
                index=len(entries) + 1,
                started_ts=started_ts,
                started_monotonic=started_monotonic,
            )
            if entry is None:
                continue
            key = self._dedupe_key(raw, entry)
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
            output.write(json.dumps(self._json_payload(entry), ensure_ascii=False) + "\n")
            added += 1
        if added:
            output.flush()
        return added

    def _entry_from_raw(
        self,
        raw: dict[str, Any],
        *,
        index: int,
        started_ts: float,
        started_monotonic: float,
    ) -> DanmakuEntry | None:
        text = _clean_text(_extract_text(raw), max_chars=self.config.danmaku.max_text_chars)
        if not text:
            return None
        user_name = _clean_text(
            _extract_string(raw, ("name", "user_name", "userName")),
            max_chars=self.config.danmaku.max_user_name_chars,
        )
        user_id = _clean_text(_extract_string(raw, ("user_id", "userId", "account_id")), max_chars=40)
        if not self.config.danmaku.include_system_messages and _is_system_message(user_name, user_id):
            return None

        timestamp = _extract_timestamp(raw)
        if timestamp is None:
            offset = max(0.0, time.monotonic() - started_monotonic)
            timestamp = started_ts + offset
        elif timestamp < 10_000_000:
            offset = max(0.0, timestamp)
            timestamp = started_ts + offset
        else:
            if timestamp < started_ts - 1.0:
                return None
            offset = max(0.0, timestamp - started_ts)

        return DanmakuEntry(
            index=index,
            offset=round(offset, 3),
            timestamp=round(timestamp, 3),
            text=text,
            user_name=user_name,
            user_id=user_id,
            raw=raw,
        )

    def _dedupe_key(self, raw: dict[str, Any], entry: DanmakuEntry) -> str:
        for key in ("comment_id", "id", "log_id"):
            value = raw.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
        raw_timestamp = _extract_timestamp(raw)
        if raw_timestamp is None:
            try:
                raw_key = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
            except TypeError:
                raw_key = str(raw)
            return f"raw:{raw_key}:{entry.user_id}:{entry.text}"
        return f"{entry.timestamp:.3f}:{entry.user_id}:{entry.text}"

    def _json_payload(self, entry: DanmakuEntry) -> dict[str, Any]:
        return {
            "index": entry.index,
            "offset": entry.offset,
            "timestamp": entry.timestamp,
            "user_name": entry.user_name,
            "user_id": entry.user_id,
            "text": entry.text,
            "raw": entry.raw or {},
        }

    def write_ass(self, path: Path, entries: list[DanmakuEntry]) -> None:
        cfg = self.config.danmaku
        width = int(self.config.transcode.width or 1280)
        height = int(self.config.transcode.height or 720)
        font_size = max(8, int(cfg.font_size))
        lane_height = max(font_size + 8, int(font_size * 1.25))
        available_height = max(lane_height, height - cfg.top_margin - cfg.bottom_margin)
        lane_count = max(1, min(int(cfg.lane_count), available_height // lane_height))

        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
                "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
            ),
            (
                f"Style: Danmaku,{_ass_style_field(cfg.font_name)},{font_size},"
                f"{_ass_color('#FFFFFF', cfg.font_opacity)},&H00FFFFFF,"
                "&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,7,0,0,0,1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        lane_available = [0.0 for _ in range(lane_count)]
        for entry in sorted(entries, key=lambda item: (item.offset, item.index)):
            text = entry.text
            if cfg.include_user_name and entry.user_name:
                text = f"{entry.user_name}\uff1a{text}"
            start = max(0.0, float(entry.offset))
            end = start + float(cfg.display_seconds)
            lane = _choose_lane(lane_available, start)
            text_width = max(font_size * 4.0, len(text) * font_size * 0.75)
            travel = width + text_width
            speed = travel / max(1.0, float(cfg.display_seconds))
            lane_available[lane] = start + min(float(cfg.display_seconds), text_width / speed + 0.35)
            y = cfg.top_margin + lane * lane_height
            move = rf"{{\an7\move({width + 20},{y},{-int(text_width) - 20},{y})}}"
            lines.append(
                "Dialogue: 0,"
                f"{_format_ass_timestamp(start)},{_format_ass_timestamp(end)},"
                f"Danmaku,,0,0,0,,{move}{_escape_ass_text(text)}"
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def _choose_lane(lane_available: list[float], start: float) -> int:
    ready = [index for index, available_at in enumerate(lane_available) if available_at <= start]
    if ready:
        return ready[0]
    return min(range(len(lane_available)), key=lambda index: lane_available[index])


def _format_ass_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    centis = int(round(seconds * 100))
    hours, remainder = divmod(centis, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centis:02}"


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("comment", "message", "text", "body", "content"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
            if isinstance(candidate, dict):
                nested = _extract_text(candidate)
                if nested:
                    return nested
        for key in ("data", "log", "comment_log"):
            nested = _extract_text(value.get(key))
            if nested:
                return nested
    return ""


def _extract_string(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return str(candidate)
        for key in ("user", "data", "profile"):
            nested = _extract_string(value.get(key), keys)
            if nested:
                return nested
    return ""


def _extract_timestamp(value: Any) -> float | None:
    raw = _extract_string(value, ("created_at", "posted_at", "timestamp", "time", "ts"))
    if not raw:
        return None
    try:
        timestamp = float(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.timestamp()
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return timestamp


def _clean_text(text: str, *, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
    return text


def _escape_ass_text(text: str) -> str:
    text = text.replace("{", "(").replace("}", ")")
    text = text.replace("\n", " ").replace("\r", " ")
    return text


def _ass_style_field(value: str) -> str:
    return (value or "Microsoft YaHei").replace(",", " ")


def _ass_color(rgb: str, opacity: float) -> str:
    value = rgb.strip().lstrip("#")
    if len(value) != 6:
        value = "FFFFFF"
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    alpha = int(round((1.0 - min(1.0, max(0.0, opacity))) * 255))
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"


def _is_system_message(user_name: str, user_id: str) -> bool:
    return user_id == "0" or user_name.lower() in {"showroom management", "showroom"}
