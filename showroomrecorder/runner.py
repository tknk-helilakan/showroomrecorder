from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

from .compat import ZoneInfo, to_thread
from .config import AppConfig, RoomConfig
from .danmaku import DanmakuCaptureResult, DanmakuRecorder
from .media import MediaProcessor, assert_tool_available
from .models import LiveSession, SubtitleSegment
from .recorder import StreamRecorder
from .showroom import LiveStatus, ShowroomClient
from .subtitles import write_srt, write_transcript_json
from .templating import build_context, render_template, slugify, unique_path
from .transcription import create_transcriber
from .translation import Translator
from .upload import BiliupUploader

LOGGER = logging.getLogger(__name__)


class ShowroomRecorderService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.tz = ZoneInfo(config.service.timezone)
        self.showroom = ShowroomClient()
        self.recorder = StreamRecorder(config, self.showroom)
        self.danmaku = DanmakuRecorder(config, self.showroom)
        self.media = MediaProcessor(config.transcode)
        self.transcriber = create_transcriber(config.asr, config.transcode.ffmpeg_bin)
        self.translator = Translator(config.translation)
        self.uploader = BiliupUploader(config)
        self.processing_sem: asyncio.Semaphore | None = None
        self.status_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, config.service.status_parallelism),
            thread_name_prefix="showroom-status",
        )
        self._record_retry_after: dict[str, float] = {}
        self._stop: asyncio.Event | None = None
        self._processing_tasks: set[asyncio.Task[None]] = set()

    async def run(self, once: bool = False) -> None:
        self._preflight()
        self.processing_sem = asyncio.Semaphore(max(1, self.config.service.processing_parallelism))
        self._stop = asyncio.Event()
        self._processing_tasks.clear()
        LOGGER.info("Watching %d SHOWROOM room(s)", len(self.config.rooms))
        stagger_seconds = 0.0
        if not once and self.config.rooms:
            stagger_seconds = min(
                5.0,
                max(0.0, self.config.service.poll_interval_seconds / len(self.config.rooms)),
            )
        tasks = [
            asyncio.create_task(
                self._watch_room(room, once=once, initial_delay=index * stagger_seconds)
            )
            for index, room in enumerate(self.config.rooms)
        ]
        if not once and self.config.upload.enabled and self.config.service.upload_recovery_enabled:
            tasks.append(asyncio.create_task(self._daily_upload_recovery_loop(), name="showroom-upload-recovery"))
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            LOGGER.info("Stopping service")
            self._stop_event().set()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._drain_processing_tasks()
            self.status_executor.shutdown(wait=False)

    async def _watch_room(self, room: RoomConfig, once: bool = False, initial_delay: float = 0.0) -> None:
        LOGGER.info("Watcher started for %s", room.name)
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        stop_event = self._stop_event()
        while not stop_event.is_set():
            poll_interval = room.poll_interval_seconds or self.config.service.poll_interval_seconds
            try:
                status = await self._get_live_status(room)
                if status.is_live:
                    if self._record_retry_allowed(room):
                        await self._handle_live(room, status)
                        if not once:
                            LOGGER.info(
                                "Finished recording for %s; watcher continues in %s second(s)",
                                room.name,
                                poll_interval,
                            )
                    else:
                        LOGGER.debug("Skipping %s live retry during record cooldown", room.name)
                elif once:
                    LOGGER.info("%s is not live", room.name)
                else:
                    LOGGER.debug("%s is not live", room.name)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Watcher error for %s: %s", room.name, exc)
            if once:
                break
            await asyncio.sleep(poll_interval)

    async def _handle_live(self, room: RoomConfig, status: LiveStatus) -> None:
        started_at = datetime.now(self.tz)
        job_id = self._make_job_id(room, started_at)
        work_dir = self.config.paths.work_dir / slugify(room.name) / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        session = LiveSession(
            room=room,
            job_id=job_id,
            started_at=started_at,
            live_title=status.title or room.name,
            work_dir=work_dir,
            metadata={"showroom_status": status.raw or {}},
        )
        self._append_job_event(session, "live_detected")
        LOGGER.info("Live detected: room=%s title=%s job=%s", room.name, session.live_title, job_id)

        danmaku_stop, danmaku_task = self._start_danmaku_capture(session)
        record_failed = False
        try:
            session.raw_file = await to_thread(self.recorder.record, session)
            session.ended_at = datetime.now(self.tz)
            self._append_job_event(session, "recorded", {"raw_file": str(session.raw_file)})
        except Exception as exc:  # noqa: BLE001
            record_failed = True
            LOGGER.exception("Recording failed for %s: %s", room.name, exc)
            self._append_job_event(session, "record_failed", {"error": str(exc)})
            self._set_record_retry_cooldown(room)
        finally:
            await self._finish_danmaku_capture(session, danmaku_stop, danmaku_task)

        if record_failed:
            return

        self._append_job_event(session, "processing_queued")
        self._schedule_processing(session)

    def _start_danmaku_capture(
        self,
        session: LiveSession,
    ) -> tuple[Event | None, asyncio.Task[DanmakuCaptureResult] | None]:
        if not self.config.danmaku.enabled:
            return None, None
        stop_event = Event()
        task = asyncio.create_task(
            to_thread(self.danmaku.capture, session, stop_event),
            name=f"showroom-danmaku-{session.job_id}",
        )
        self._append_job_event(session, "danmaku_capture_started")
        return stop_event, task

    async def _finish_danmaku_capture(
        self,
        session: LiveSession,
        stop_event: Event | None,
        task: asyncio.Task[DanmakuCaptureResult] | None,
    ) -> None:
        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        timeout = max(
            5.0,
            float(self.config.danmaku.request_timeout_seconds) + float(self.config.danmaku.poll_seconds) + 2.0,
        )
        try:
            result = await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            LOGGER.warning("Timed out waiting for danmaku capture to finish for %s", session.job_id)
            self._append_job_event(session, "danmaku_capture_failed", {"error": "timeout"})
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Danmaku capture failed for %s: %s", session.job_id, exc)
            self._append_job_event(session, "danmaku_capture_failed", {"error": str(exc)})
            return

        session.danmaku_ass_file = result.ass_file
        session.danmaku_jsonl_file = result.jsonl_file
        self._append_job_event(
            session,
            "danmaku_captured",
            {
                "danmaku_ass_file": str(result.ass_file) if result.ass_file else None,
                "danmaku_jsonl_file": str(result.jsonl_file) if result.jsonl_file else None,
                "danmaku_count": result.count,
            },
        )

    def _schedule_processing(self, session: LiveSession) -> None:
        task = asyncio.create_task(
            self._process_session(session),
            name=f"showroom-process-{session.job_id}",
        )
        self._processing_tasks.add(task)

        def discard(done: asyncio.Task[None]) -> None:
            self._processing_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                LOGGER.warning("Processing task cancelled for %s", session.job_id)
            except Exception:
                LOGGER.exception("Processing task crashed for %s", session.job_id)

        task.add_done_callback(discard)
        LOGGER.info("Queued processing for %s job=%s", session.room.name, session.job_id)

    async def _process_session(self, session: LiveSession) -> None:
        async with self._processing_semaphore():
            try:
                await to_thread(self._process_and_upload, session)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Processing failed for %s: %s", session.room.name, exc)
                self._append_job_event(session, "processing_failed", {"error": str(exc)})
                return

    async def _drain_processing_tasks(self) -> None:
        while self._processing_tasks:
            tasks = tuple(self._processing_tasks)
            LOGGER.info("Waiting for %d queued processing task(s) to finish", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _daily_upload_recovery_loop(self) -> None:
        stop_event = self._stop_event()
        LOGGER.info(
            "Daily upload recovery enabled at %s; stale threshold=%d minute(s)",
            self.config.service.upload_recovery_time,
            self.config.service.upload_recovery_stale_minutes,
        )
        while not stop_event.is_set():
            delay = self._seconds_until_upload_recovery()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            if stop_event.is_set():
                break
            try:
                await self._recover_pending_uploads()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Daily upload recovery failed: %s", exc)

    def _seconds_until_upload_recovery(self) -> float:
        value = str(self.config.service.upload_recovery_time or "03:00").strip()
        try:
            hour_text, minute_text = value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            LOGGER.warning("Invalid service.upload_recovery_time=%r; falling back to 03:00", value)
            hour, minute = 3, 0

        now = datetime.now(self.tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    async def _recover_pending_uploads(self) -> None:
        sessions = self._find_pending_upload_sessions()
        if not sessions:
            LOGGER.info("Daily upload recovery found no pending upload(s)")
            return
        LOGGER.warning("Daily upload recovery found %d pending upload(s)", len(sessions))
        for session in sessions:
            if self._stop_event().is_set():
                break
            if self._is_job_active(session.job_id):
                LOGGER.info("Skipping upload recovery for active job %s", session.job_id)
                continue
            async with self._processing_semaphore():
                try:
                    await to_thread(self._recover_upload_session, session)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("Upload recovery failed for %s: %s", session.job_id, exc)

    def _recover_upload_session(self, session: LiveSession) -> None:
        if not session.upload_file:
            raise RuntimeError(f"Cannot recover upload without upload_file: {session.job_id}")
        session.work_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.warning("Recovering pending upload for job=%s file=%s", session.job_id, session.upload_file)
        segments = self._load_recovery_segments(session)
        self._append_job_event(
            session,
            "upload_recovery_started",
            {
                "upload_file": str(session.upload_file),
                "translated_segments": len(segments),
            },
        )
        try:
            bvid = self.uploader.upload(session, segments)
        except Exception as exc:  # noqa: BLE001
            if session.bvid:
                self._append_job_event(
                    session,
                    "uploaded",
                    {
                        "bvid": session.bvid,
                        "recovered": True,
                        "post_upload_error": str(exc),
                    },
                )
            self._append_job_event(
                session,
                "upload_recovery_failed",
                {
                    "bvid": session.bvid,
                    "error": str(exc),
                },
            )
            raise

        self._append_job_event(session, "uploaded", {"bvid": bvid, "recovered": True})
        if bvid and session.metadata.get("subtitle_uploaded"):
            self._append_job_event(session, "subtitle_uploaded", {"bvid": bvid, "recovered": True})
        elif bvid and session.metadata.get("subtitle_upload_error"):
            self._append_job_event(
                session,
                "subtitle_failed",
                {
                    "bvid": bvid,
                    "recovered": True,
                    "error": str(session.metadata["subtitle_upload_error"]),
                },
            )

    def _find_pending_upload_sessions(self) -> list[LiveSession]:
        events = self._read_job_events()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in events:
            job_id = str(item.get("job_id") or "").strip()
            if job_id:
                grouped.setdefault(job_id, []).append(item)

        sessions: list[LiveSession] = []
        for job_id, items in grouped.items():
            if self._job_has_upload_terminal_event(items):
                continue
            if any(item.get("bvid") for item in items):
                LOGGER.info("Skipping upload recovery for %s because a BVID is already recorded", job_id)
                continue
            if not any(item.get("event") == "upload_file_ready" and item.get("upload_file") for item in items):
                continue
            snapshot = self._job_snapshot(items)
            upload_file = self._event_path(snapshot.get("upload_file"))
            if not upload_file or not upload_file.exists():
                LOGGER.warning("Skipping upload recovery for %s because upload_file is missing: %s", job_id, upload_file)
                continue
            if self._is_job_active(job_id):
                LOGGER.info("Skipping upload recovery for active job %s", job_id)
                continue
            if self._job_looks_in_progress(items, snapshot):
                LOGGER.info("Skipping upload recovery for %s because upload appears to be in progress", job_id)
                continue
            session = self._session_from_job_snapshot(snapshot)
            if session:
                sessions.append(session)
        return sessions

    def _read_job_events(self) -> list[dict[str, Any]]:
        path = self.config.paths.jobs_log
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    LOGGER.warning("Skipping invalid jobs log line %d: %s", line_number, exc)
                    continue
                if isinstance(item, dict):
                    events.append(item)
        return events

    def _job_has_upload_terminal_event(self, items: list[dict[str, Any]]) -> bool:
        terminal_events = {"uploaded", "upload_skipped", "cleanup_done"}
        return any(str(item.get("event") or "") in terminal_events for item in items)

    def _job_snapshot(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        snapshot = dict(items[-1])
        fields = (
            "room",
            "room_id",
            "title",
            "started_at",
            "ended_at",
            "raw_file",
            "mp4_file",
            "ja_srt_file",
            "zh_srt_file",
            "danmaku_ass_file",
            "danmaku_jsonl_file",
            "upload_file",
            "bvid",
        )
        for field in fields:
            for item in reversed(items):
                value = item.get(field)
                if value not in (None, ""):
                    snapshot[field] = value
                    break
        return snapshot

    def _job_looks_in_progress(self, items: list[dict[str, Any]], snapshot: dict[str, Any]) -> bool:
        last_event = str(items[-1].get("event") or "")
        if last_event in {"processing_failed", "upload_recovery_failed", "record_failed"}:
            return False
        stale_seconds = max(60, int(self.config.service.upload_recovery_stale_minutes) * 60)
        now = datetime.now(self.tz)
        last_ts = self._parse_event_datetime(items[-1].get("ts"))
        if last_ts and (now - last_ts).total_seconds() < stale_seconds:
            return True

        job_id = str(snapshot.get("job_id") or "")
        work_dir = self._event_work_dir(snapshot, job_id=job_id)
        if not work_dir.exists():
            return False
        for log_path in work_dir.glob("biliup-*.log"):
            try:
                age = time.time() - log_path.stat().st_mtime
            except OSError:
                continue
            if age < stale_seconds:
                return True
        return False

    def _session_from_job_snapshot(self, snapshot: dict[str, Any]) -> LiveSession | None:
        job_id = str(snapshot.get("job_id") or "").strip()
        if not job_id:
            return None
        room = self._resolve_event_room(snapshot)
        if room is None:
            LOGGER.warning("Skipping upload recovery for %s because room is no longer configured", job_id)
            return None
        started_at = self._parse_event_datetime(snapshot.get("started_at")) or self._started_at_from_job_id(job_id)
        if started_at is None:
            LOGGER.warning("Skipping upload recovery for %s because started_at is unknown", job_id)
            return None
        ended_at = self._parse_event_datetime(snapshot.get("ended_at"))
        session = LiveSession(
            room=room,
            job_id=job_id,
            started_at=started_at,
            ended_at=ended_at,
            live_title=str(snapshot.get("title") or room.name),
            work_dir=self._event_work_dir(snapshot, room=room, job_id=job_id),
            raw_file=self._event_path(snapshot.get("raw_file")),
            mp4_file=self._event_path(snapshot.get("mp4_file")),
            ja_srt_file=self._event_path(snapshot.get("ja_srt_file")),
            zh_srt_file=self._event_path(snapshot.get("zh_srt_file")),
            danmaku_ass_file=self._event_path(snapshot.get("danmaku_ass_file")),
            danmaku_jsonl_file=self._event_path(snapshot.get("danmaku_jsonl_file")),
            upload_file=self._event_path(snapshot.get("upload_file")),
            metadata={"upload_recovery": True},
        )
        return session

    def _resolve_event_room(self, snapshot: dict[str, Any]) -> RoomConfig | None:
        room_id = snapshot.get("room_id")
        if room_id not in (None, ""):
            try:
                room_id_int = int(room_id)
            except (TypeError, ValueError):
                room_id_int = None
            if room_id_int is not None:
                for room in self.config.rooms:
                    if room.room_id == room_id_int:
                        return room
        room_name = str(snapshot.get("room") or "").strip()
        for room in self.config.rooms:
            if room.name == room_name or slugify(room.name) == room_name:
                return room
        return None

    def _event_work_dir(
        self,
        snapshot: dict[str, Any],
        *,
        room: RoomConfig | None = None,
        job_id: str | None = None,
    ) -> Path:
        job_id = job_id or str(snapshot.get("job_id") or "")
        room = room or self._resolve_event_room(snapshot)
        room_slug = slugify(room.name) if room else str(snapshot.get("room") or "unknown")
        return self.config.paths.work_dir / room_slug / job_id

    def _event_path(self, value: Any) -> Path | None:
        if value in (None, ""):
            return None
        return Path(str(value))

    def _parse_event_datetime(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.tz)
        return parsed

    def _is_job_active(self, job_id: str) -> bool:
        task_name = f"showroom-process-{job_id}"
        return any(not task.done() and task.get_name() == task_name for task in self._processing_tasks)

    def _load_recovery_segments(self, session: LiveSession) -> list[SubtitleSegment]:
        transcript = self._find_recovery_transcript(session)
        if not transcript:
            return []
        try:
            payload = json.loads(transcript.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("translated transcript must contain a list")
            segments = [
                SubtitleSegment(
                    index=int(item["index"]),
                    start=float(item["start"]),
                    end=float(item["end"]),
                    text=str(item.get("text") or ""),
                    translation=item.get("translation"),
                )
                for item in payload
                if isinstance(item, dict)
            ]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not load translated transcript for %s: %s", session.job_id, exc)
            return []
        LOGGER.info("Loaded %d translated subtitle segment(s) for upload recovery: %s", len(segments), transcript)
        return segments

    def _find_recovery_transcript(self, session: LiveSession) -> Path | None:
        candidates: list[Path] = []
        if session.zh_srt_file:
            name = session.zh_srt_file.name
            if name.endswith(".zh.srt"):
                candidates.append(session.zh_srt_file.with_name(name[: -len(".zh.srt")] + ".translated.json"))
        if session.started_at:
            key = f"{session.started_at:%Y%m%d_%H%M%S}"
            subtitle_dir = self.config.paths.subtitles_dir / slugify(session.room.name)
            candidates.extend(sorted(subtitle_dir.glob(f"*{key}*.translated.json")))
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _recovery_file_stem(self, session: LiveSession) -> str:
        if session.mp4_file:
            return session.mp4_file.stem
        if session.zh_srt_file:
            name = session.zh_srt_file.name
            if name.endswith(".zh.srt"):
                return name[: -len(".zh.srt")]
        if session.upload_file:
            return session.upload_file.stem
        return session.job_id

    def _processing_semaphore(self) -> asyncio.Semaphore:
        if self.processing_sem is None:
            self.processing_sem = asyncio.Semaphore(max(1, self.config.service.processing_parallelism))
        return self.processing_sem

    def _stop_event(self) -> asyncio.Event:
        if self._stop is None:
            self._stop = asyncio.Event()
        return self._stop

    async def _get_live_status(self, room: RoomConfig) -> LiveStatus:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.status_executor, self.showroom.get_live_status, room)

    def process_existing_recording(
        self,
        raw_file: Path,
        *,
        room_ref: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        title: str | None = None,
    ) -> None:
        self._preflight()
        raw_file = raw_file.resolve()
        if not raw_file.exists():
            raise FileNotFoundError(f"Raw recording not found: {raw_file}")
        room = self._resolve_recording_room(raw_file, room_ref)
        job_id = raw_file.parent.name
        started_at = started_at or self._started_at_from_job_id(job_id) or datetime.fromtimestamp(
            raw_file.stat().st_mtime,
            self.tz,
        )
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=self.tz)
        ended_at = ended_at or datetime.fromtimestamp(raw_file.stat().st_mtime, self.tz)
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=self.tz)
        work_dir = self.config.paths.work_dir / slugify(room.name) / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        session = LiveSession(
            room=room,
            job_id=job_id,
            started_at=started_at,
            ended_at=ended_at,
            live_title=title or room.name,
            raw_file=raw_file,
            work_dir=work_dir,
            metadata={"resumed_from_raw": str(raw_file)},
        )
        self._resolve_danmaku_files(session)
        LOGGER.info("Resuming processing from raw recording: room=%s job=%s", room.name, job_id)
        self._append_job_event(session, "processing_resumed", {"raw_file": str(raw_file)})
        try:
            self._process_and_upload(session)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Processing failed for %s: %s", room.name, exc)
            self._append_job_event(session, "processing_failed", {"error": str(exc)})
            raise

    def _resolve_recording_room(self, raw_file: Path, room_ref: str | None) -> RoomConfig:
        refs = []
        if room_ref:
            refs.append(room_ref)
        if raw_file.parent.parent.name:
            refs.append(raw_file.parent.parent.name)
        refs.append(raw_file.parent.name.rsplit("_", 1)[-1])
        normalized_refs = {str(item).strip() for item in refs if str(item).strip()}
        for room in self.config.rooms:
            candidates = {
                room.name,
                str(room.room_id or ""),
                slugify(room.name),
            }
            if normalized_refs.intersection(candidates):
                return room
        raise ValueError(
            f"Could not match raw recording to a configured room. Pass --room. Tried: {sorted(normalized_refs)}"
        )

    def _started_at_from_job_id(self, job_id: str) -> datetime | None:
        match = re.match(r"^(\d{8}_\d{6})", job_id)
        if not match:
            return None
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=self.tz)

    def _room_key(self, room: RoomConfig) -> str:
        return str(room.room_id or room.url or room.name)

    def _record_retry_allowed(self, room: RoomConfig) -> bool:
        retry_at = self._record_retry_after.get(self._room_key(room), 0.0)
        return time.monotonic() >= retry_at

    def _set_record_retry_cooldown(self, room: RoomConfig) -> None:
        cooldown = max(0, int(self.config.service.record_retry_cooldown_seconds))
        if cooldown <= 0:
            return
        self._record_retry_after[self._room_key(room)] = time.monotonic() + cooldown
        LOGGER.warning(
            "Recording retry for %s is cooled down for %d second(s)",
            room.name,
            cooldown,
        )

    def _process_and_upload(self, session: LiveSession) -> None:
        if not session.raw_file:
            raise RuntimeError("Missing raw recording")

        context = build_context(
            streamer=session.room.name,
            room_url=session.room.url,
            room_id=session.room.room_id,
            title=session.live_title,
            started_at=session.started_at,
            ended_at=session.ended_at,
            job_id=session.job_id,
        )
        file_stem = slugify(render_template(self.config.naming.filename_template, context))
        self._resolve_danmaku_files(session)
        danmaku_ass = (
            session.danmaku_ass_file
            if self.config.danmaku.enabled
            and self.config.danmaku.burn_in
            and session.danmaku_ass_file
            and session.danmaku_ass_file.exists()
            else None
        )

        if self.config.transcode.enabled:
            mp4_path = unique_path(
                self.config.paths.processed_dir / slugify(session.room.name),
                file_stem,
                ".mp4",
            )
            session.mp4_file = self.media.transcode(session.raw_file, mp4_path, danmaku_file=danmaku_ass)
        else:
            mp4_path = unique_path(
                self.config.paths.processed_dir / slugify(session.room.name),
                file_stem,
                session.raw_file.suffix,
            )
            shutil.copy2(session.raw_file, mp4_path)
            session.mp4_file = mp4_path
        self._append_job_event(
            session,
            "transcoded",
            {
                "mp4_file": str(session.mp4_file),
                "danmaku_burned": bool(danmaku_ass),
            },
        )

        segments: list[SubtitleSegment] = []
        if self.config.asr.enabled:
            segments = self.transcriber.transcribe(session.mp4_file)
            subtitle_dir = self.config.paths.subtitles_dir / slugify(session.room.name)
            session.ja_srt_file = unique_path(subtitle_dir, f"{file_stem}.ja", ".srt")
            write_srt(
                session.ja_srt_file,
                segments,
                language="ja",
                max_line_chars=self.config.subtitles.max_line_chars,
            )
            write_transcript_json(subtitle_dir / f"{file_stem}.transcript.json", segments)
            self._append_job_event(session, "asr_done", {"ja_srt_file": str(session.ja_srt_file)})

            segments = self.translator.translate(segments)
            session.zh_srt_file = unique_path(subtitle_dir, f"{file_stem}.zh", ".srt")
            write_srt(
                session.zh_srt_file,
                segments,
                language="zh",
                max_line_chars=self.config.subtitles.max_line_chars,
                bilingual=self.config.subtitles.bilingual,
            )
            write_transcript_json(subtitle_dir / f"{file_stem}.translated.json", segments)
            self._append_job_event(session, "translation_done", {"zh_srt_file": str(session.zh_srt_file)})

        session.upload_file = self._prepare_upload_file(session, file_stem)
        self._append_job_event(session, "upload_file_ready", {"upload_file": str(session.upload_file)})

        bvid = self.uploader.upload(session, segments)
        event = "uploaded" if self.config.upload.enabled else "upload_skipped"
        self._append_job_event(session, event, {"bvid": bvid})
        if self.config.upload.enabled and bvid:
            if session.metadata.get("subtitle_uploaded"):
                self._append_job_event(session, "subtitle_uploaded", {"bvid": bvid})
            elif session.metadata.get("subtitle_upload_error"):
                self._append_job_event(
                    session,
                    "subtitle_failed",
                    {
                        "bvid": bvid,
                        "error": str(session.metadata["subtitle_upload_error"]),
                    },
                )
        if self.config.upload.enabled and bvid and self.config.upload.cleanup_after_success:
            removed_paths = self._cleanup_after_success(session, file_stem)
            self._append_job_event(session, "cleanup_done", {"removed_paths": removed_paths})

    def _prepare_upload_file(self, session: LiveSession, file_stem: str) -> Path:
        if not session.mp4_file:
            raise RuntimeError("Missing mp4 file")
        mode = self.config.upload.subtitle_mode
        upload_stem = self._upload_file_stem(session, file_stem)
        upload_dir = self.config.paths.upload_dir / slugify(session.room.name)
        upload_dir.mkdir(parents=True, exist_ok=True)

        if mode == "hard_subbed" and session.zh_srt_file:
            output_file = unique_path(upload_dir, f"{upload_stem}.hardsub", ".mp4")
            output_file = self.media.burn_subtitles(session.mp4_file, session.zh_srt_file, output_file)
            self._copy_danmaku_sidecars(session, output_file)
            return output_file

        output_file = unique_path(upload_dir, upload_stem, ".mp4")
        shutil.copy2(session.mp4_file, output_file)
        if mode == "sidecar" and session.zh_srt_file:
            shutil.copy2(session.zh_srt_file, output_file.with_suffix(".zh.srt"))
        self._copy_danmaku_sidecars(session, output_file)
        return output_file

    def _copy_danmaku_sidecars(self, session: LiveSession, output_file: Path) -> None:
        self._resolve_danmaku_files(session)
        for source, suffix in (
            (session.danmaku_ass_file, ".danmaku.ass"),
            (session.danmaku_jsonl_file, ".danmaku.jsonl"),
        ):
            if source and source.exists():
                shutil.copy2(source, output_file.with_suffix(suffix))

    def _resolve_danmaku_files(self, session: LiveSession) -> None:
        if session.danmaku_ass_file and session.danmaku_jsonl_file:
            return
        danmaku_dir = self.config.paths.danmaku_dir / slugify(session.room.name) / session.job_id
        if not danmaku_dir.exists():
            return
        if not session.danmaku_ass_file:
            session.danmaku_ass_file = self._first_existing_path(
                sorted(danmaku_dir.glob("*.danmaku.ass")) + sorted(danmaku_dir.glob("*.ass"))
            )
        if not session.danmaku_jsonl_file:
            session.danmaku_jsonl_file = self._first_existing_path(
                sorted(danmaku_dir.glob("*.danmaku.jsonl")) + sorted(danmaku_dir.glob("*.jsonl"))
            )

    def _first_existing_path(self, paths: list[Path]) -> Path | None:
        for path in paths:
            if path.exists():
                return path
        return None

    def _upload_file_stem(self, session: LiveSession, fallback: str) -> str:
        upload_mode = str(self.config.upload.biliup.get("mode", "upload")).lower()
        if upload_mode in {"append", "monthly", "auto_monthly", "monthly_append"}:
            context = build_context(
                streamer=session.room.name,
                room_url=session.room.url,
                room_id=session.room.room_id,
                title=session.live_title,
                started_at=session.started_at,
                ended_at=session.ended_at,
                job_id=session.job_id,
            )
            return slugify(render_template(self.config.naming.part_title_template, context))
        return fallback

    def _cleanup_after_success(self, session: LiveSession, file_stem: str) -> list[str]:
        keep: set[Path] = set()
        removed: list[str] = []

        def remember_keep(path: Path | None) -> None:
            if path and path.exists():
                keep.add(path.resolve())

        remember_keep(session.upload_file)
        if session.upload_file:
            remember_keep(session.upload_file.with_suffix(".zh.srt"))
            remember_keep(session.upload_file.with_suffix(".danmaku.ass"))
            remember_keep(session.upload_file.with_suffix(".danmaku.jsonl"))

        candidates: list[Path] = []
        for path in (
            session.raw_file,
            session.mp4_file,
            session.ja_srt_file,
            session.zh_srt_file,
            session.danmaku_ass_file,
            session.danmaku_jsonl_file,
        ):
            if path:
                candidates.append(path)
        if session.raw_file:
            candidates.append(session.raw_file.parent)
        if session.mp4_file:
            candidates.append(session.mp4_file.with_suffix(".ffmpeg.log"))
            candidates.extend(session.mp4_file.parent.glob(f"{session.mp4_file.stem}.asr*"))
        subtitle_dir = self.config.paths.subtitles_dir / slugify(session.room.name)
        candidates.extend(subtitle_dir.glob(f"{file_stem}*"))
        if session.danmaku_ass_file:
            candidates.append(session.danmaku_ass_file.parent)
        candidates.append(session.work_dir)

        for path in candidates:
            removed.extend(self._remove_cleanup_path(path, keep))

        if self.config.upload.keep_latest_upload_per_room and session.upload_file:
            upload_dir = session.upload_file.parent
            for path in upload_dir.iterdir():
                removed.extend(self._remove_cleanup_path(path, keep))
            room_slug = slugify(session.room.name)
            for parent_dir in (
                self.config.paths.raw_dir,
                self.config.paths.processed_dir,
                self.config.paths.subtitles_dir,
                self.config.paths.danmaku_dir,
                self.config.paths.work_dir,
            ):
                room_dir = parent_dir / room_slug
                if not room_dir.exists():
                    continue
                for path in room_dir.iterdir():
                    removed.extend(self._remove_cleanup_path(path, keep))

        LOGGER.info("Cleanup after successful upload removed %d path(s)", len(removed))
        return removed

    def _remove_cleanup_path(self, path: Path, keep: set[Path]) -> list[str]:
        if not path.exists():
            return []
        resolved = path.resolve()
        if resolved in keep:
            return []
        if not self._is_under_data_dir(resolved):
            LOGGER.warning("Skipping cleanup outside data_dir: %s", resolved)
            return []
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            LOGGER.warning("Cleanup failed for %s: %s", path, exc)
            return []
        return [str(resolved)]

    def _is_under_data_dir(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.config.paths.data_dir.resolve())
            return True
        except ValueError:
            return False

    def _preflight(self) -> None:
        if self.config.record.strategy == "yt_dlp":
            if (
                shutil.which(self.config.record.yt_dlp_bin) is None
                and importlib.util.find_spec("yt_dlp") is None
            ):
                raise RuntimeError(
                    "yt-dlp is required. Install dependencies with: pip install -r requirements.txt"
                )
        if self.config.transcode.enabled or self.config.record.strategy == "ffmpeg":
            assert_tool_available(self.config.transcode.ffmpeg_bin)
        if self.config.asr.enabled and self.config.asr.provider in {"openai", "openai_compatible"}:
            assert_tool_available(self.config.transcode.ffmpeg_bin)
            if not os.getenv(self.config.asr.api_key_env, ""):
                raise RuntimeError(
                    f"OpenAI-compatible ASR requires environment variable {self.config.asr.api_key_env}"
                )
        if self.config.asr.enabled and self.config.asr.provider == "faster_whisper":
            self._assert_python_package(
                "faster_whisper",
                "Local ASR requires faster-whisper. Run with .\\.venv\\Scripts\\python.exe or install local model dependencies with: pip install -r requirements-local.txt",
            )
        if self.config.translation.enabled and self.config.translation.provider == "openai_responses":
            cfg = self.config.translation.openai_responses
            api_key_env = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
            if not os.getenv(api_key_env, ""):
                raise RuntimeError(
                    f"OpenAI translation requires environment variable {api_key_env}"
                )
        if self.config.translation.enabled and self.config.translation.provider == "transformers_seq2seq":
            for package in ("torch", "transformers"):
                self._assert_python_package(
                    package,
                    "Local translation requires torch and transformers. Run with .\\.venv\\Scripts\\python.exe or install local model dependencies with: pip install -r requirements-local.txt",
                )
        if self.config.upload.enabled:
            bin_name = str(self.config.upload.biliup.get("bin", "biliup"))
            assert_tool_available(bin_name)

    def _assert_python_package(self, package: str, message: str) -> None:
        if importlib.util.find_spec(package) is None:
            raise RuntimeError(message)

    def _make_job_id(self, room: RoomConfig, started_at: datetime) -> str:
        return f"{started_at:%Y%m%d_%H%M%S}_{slugify(room.name, 40)}"

    def _append_job_event(
        self,
        session: LiveSession,
        event: str,
        extra: dict | None = None,
    ) -> None:
        payload = {
            "ts": datetime.now(self.tz).isoformat(),
            "event": event,
            "job_id": session.job_id,
            "room": session.room.name,
            "room_id": session.room.room_id,
            "title": session.live_title,
            "started_at": session.started_at.isoformat(),
            "ended_at": session.ended_at.isoformat() if session.ended_at else None,
            "raw_file": str(session.raw_file) if session.raw_file else None,
            "mp4_file": str(session.mp4_file) if session.mp4_file else None,
            "ja_srt_file": str(session.ja_srt_file) if session.ja_srt_file else None,
            "zh_srt_file": str(session.zh_srt_file) if session.zh_srt_file else None,
            "danmaku_ass_file": str(session.danmaku_ass_file) if session.danmaku_ass_file else None,
            "danmaku_jsonl_file": str(session.danmaku_jsonl_file) if session.danmaku_jsonl_file else None,
            "upload_file": str(session.upload_file) if session.upload_file else None,
            "bvid": session.bvid,
        }
        if extra:
            payload.update(extra)
        self.config.paths.jobs_log.parent.mkdir(parents=True, exist_ok=True)
        with self.config.paths.jobs_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
