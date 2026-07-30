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
from datetime import datetime
from pathlib import Path
from typing import Any

from .compat import ZoneInfo, to_thread
from .config import AppConfig, RoomConfig
from .media import MediaProcessor, assert_tool_available
from .models import LiveSession, RecordingSegment, SubtitleSegment
from .recorder import StreamRecorder
from .showroom import LiveStatus, ShowroomClient
from .subtitles import write_srt, write_transcript_json
from .templating import build_context, render_template, slugify, unique_path
from .transcription import create_transcriber
from .translation import Translator

LOGGER = logging.getLogger(__name__)


class ShowroomRecorderService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.tz = ZoneInfo(config.service.timezone)
        self.showroom = ShowroomClient()
        self.recorder = StreamRecorder(config, self.showroom)
        self.media = MediaProcessor(config.transcode)
        self.transcriber = create_transcriber(config.asr, config.transcode.ffmpeg_bin)
        self.translator = Translator(config.translation)
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
        self.processing_sem = asyncio.Semaphore(
            max(1, self.config.service.processing_parallelism)
        )
        self._stop = asyncio.Event()
        self._processing_tasks.clear()
        LOGGER.info("Watching %d SHOWROOM room(s)", len(self.config.rooms))
        stagger_seconds = 0.0
        if not once and self.config.rooms:
            stagger_seconds = min(
                5.0,
                max(
                    0.0,
                    self.config.service.poll_interval_seconds / len(self.config.rooms),
                ),
            )
        tasks = [
            asyncio.create_task(
                self._watch_room(
                    room,
                    once=once,
                    initial_delay=index * stagger_seconds,
                )
            )
            for index, room in enumerate(self.config.rooms)
        ]
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            LOGGER.info("Stopping service")
            self._stop_event().set()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._drain_processing_tasks()
            self.status_executor.shutdown(wait=False)

    async def _watch_room(
        self,
        room: RoomConfig,
        once: bool = False,
        initial_delay: float = 0.0,
    ) -> None:
        LOGGER.info("Watcher started for %s", room.name)
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        stop_event = self._stop_event()
        while not stop_event.is_set():
            poll_interval = (
                room.poll_interval_seconds
                or self.config.service.poll_interval_seconds
            )
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
                        LOGGER.debug(
                            "Skipping %s live retry during record cooldown",
                            room.name,
                        )
                elif once:
                    LOGGER.info("%s is not live", room.name)
                else:
                    LOGGER.debug("%s is not live", room.name)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Watcher error for %s: %s", room.name, exc)
            if once:
                break
            await asyncio.sleep(poll_interval)

    async def _handle_live(
        self,
        room: RoomConfig,
        status: LiveStatus,
    ) -> None:
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
        LOGGER.info(
            "Live detected: room=%s title=%s job=%s",
            room.name,
            session.live_title,
            job_id,
        )

        try:
            segments, recording_errors = await self._record_live_segments(session)
        except Exception as exc:  # noqa: BLE001
            session.ended_at = datetime.now(self.tz)
            LOGGER.exception(
                "Live recording session crashed for %s: %s",
                room.name,
                exc,
            )
            self._append_job_event(session, "record_failed", {"error": str(exc)})
            self._set_record_retry_cooldown(room)
            return

        session.raw_segments = [segment.file for segment in segments]
        session.metadata["recording_segments"] = [
            self._recording_segment_payload(segment) for segment in segments
        ]
        session.metadata["recording_timeline"] = self._build_recording_timeline(
            session,
            segments,
        )
        session.ended_at = (
            segments[-1].ended_at if segments else datetime.now(self.tz)
        )

        if not segments:
            error = (
                "; ".join(recording_errors[-3:])
                or "No usable recording segment was captured"
            )
            LOGGER.error("Recording session failed for %s: %s", room.name, error)
            self._append_job_event(session, "record_failed", {"error": error})
            self._set_record_retry_cooldown(room)
            return

        raw_dir = (
            self.config.paths.raw_dir / slugify(room.name) / session.job_id
        )
        merged_file = raw_dir / "recording-merged.mkv"
        try:
            session.raw_file = await to_thread(
                self.media.merge_recording_segments,
                session.raw_segments,
                merged_file,
            )
            merged_duration = await to_thread(
                self.recorder.probe_duration,
                session.raw_file,
            )
            if merged_duration is None or merged_duration <= 0:
                raise RuntimeError(
                    f"Could not probe merged recording duration: {session.raw_file}"
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception(
                "Recording segment merge failed for %s: %s",
                room.name,
                exc,
            )
            self._append_job_event(
                session,
                "record_failed",
                {
                    "error": f"Recording segment merge failed: {exc}",
                    "raw_segments": [
                        str(path) for path in session.raw_segments
                    ],
                },
            )
            return

        self._append_job_event(
            session,
            "recording_merged",
            {
                "raw_file": str(session.raw_file),
                "raw_segments": [str(path) for path in session.raw_segments],
                "segment_count": len(segments),
                "media_duration_seconds": merged_duration,
                "recording_timeline": session.metadata["recording_timeline"],
            },
        )
        self._append_job_event(
            session,
            "recorded",
            {
                "raw_file": str(session.raw_file),
                "raw_segments": [str(path) for path in session.raw_segments],
                "segment_count": len(segments),
            },
        )
        self._append_job_event(session, "processing_queued")
        self._schedule_processing(session)

    async def _record_live_segments(
        self,
        session: LiveSession,
    ) -> tuple[list[RecordingSegment], list[str]]:
        segments: list[RecordingSegment] = []
        errors: list[str] = []
        segment_index = 0

        while not self._stop_event().is_set():
            segment_index += 1
            segment_started_at = datetime.now(self.tz)
            self._append_job_event(
                session,
                "recording_segment_started",
                {
                    "segment_index": segment_index,
                    "segment_started_at": segment_started_at.isoformat(),
                },
            )
            try:
                media_file = await to_thread(
                    self.recorder.record,
                    session,
                    segment_index=segment_index,
                )
                segment_ended_at = datetime.now(self.tz)
                media_duration = await to_thread(
                    self.recorder.probe_duration,
                    media_file,
                )
                if media_duration is None or media_duration <= 0:
                    raise RuntimeError(
                        f"Could not determine segment duration: {media_file}"
                    )
                capture_health = await to_thread(
                    self.recorder.read_capture_health_report,
                    media_file,
                )
                capture_started_at, capture_ended_at = self._capture_window(
                    segment_started_at,
                    segment_ended_at,
                    capture_health,
                )
                segment = RecordingSegment(
                    index=segment_index,
                    file=media_file,
                    started_at=capture_started_at,
                    ended_at=capture_ended_at,
                    media_duration=media_duration,
                )
                segments.append(segment)
                session.raw_segments.append(media_file)
                self._append_job_event(
                    session,
                    "recording_segment_completed",
                    self._recording_segment_payload(segment),
                )
            except Exception as exc:  # noqa: BLE001
                segment_ended_at = datetime.now(self.tz)
                errors.append(str(exc))
                LOGGER.warning(
                    "Recording segment %d failed for %s: %s",
                    segment_index,
                    session.room.name,
                    exc,
                )
                self._append_job_event(
                    session,
                    "recording_segment_failed",
                    {
                        "segment_index": segment_index,
                        "segment_started_at": segment_started_at.isoformat(),
                        "segment_ended_at": segment_ended_at.isoformat(),
                        "error": str(exc),
                    },
                )

            if self.config.record.max_seconds:
                LOGGER.info(
                    "record.max_seconds is set; finishing the test recording session"
                )
                break
            if self._stop_event().is_set():
                break
            if not await self._live_session_should_continue(session):
                break

            self._append_job_event(
                session,
                "recording_reconnecting",
                {
                    "next_segment_index": segment_index + 1,
                    "delay_seconds": self.config.record.reconnect_delay_seconds,
                },
            )
            if await self._wait_for_stop(
                self.config.record.reconnect_delay_seconds
            ):
                break

        return segments, errors

    async def _live_session_should_continue(
        self,
        session: LiveSession,
    ) -> bool:
        required = max(1, int(self.config.record.live_end_confirmations))
        interval = max(
            0.0,
            float(self.config.record.live_end_check_interval_seconds),
        )
        offline_count = 0

        while not self._stop_event().is_set():
            try:
                status = await self._get_live_status(session.room)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "Could not confirm live end for %s; keeping the same session: %s",
                    session.room.name,
                    exc,
                )
                return True

            if status.is_live:
                LOGGER.info(
                    "Room %s is still live; recording will reconnect in the same job",
                    session.room.name,
                )
                return True
            if not status.raw:
                LOGGER.warning(
                    "SHOWROOM live status is unknown for %s; keeping the same session",
                    session.room.name,
                )
                return True

            offline_count += 1
            self._append_job_event(
                session,
                "live_end_check",
                {
                    "offline_confirmation": offline_count,
                    "offline_confirmations_required": required,
                },
            )
            if offline_count >= required:
                self._append_job_event(
                    session,
                    "live_ended",
                    {"offline_confirmations": offline_count},
                )
                return False
            if await self._wait_for_stop(interval):
                return False

        return False

    async def _wait_for_stop(self, seconds: float) -> bool:
        delay = max(0.0, float(seconds))
        if delay <= 0:
            await asyncio.sleep(0)
            return self._stop_event().is_set()
        try:
            await asyncio.wait_for(self._stop_event().wait(), timeout=delay)
        except asyncio.TimeoutError:
            return False
        return True

    def _recording_segment_payload(
        self,
        segment: RecordingSegment,
    ) -> dict[str, Any]:
        return {
            "segment_index": segment.index,
            "segment_file": str(segment.file),
            "segment_started_at": segment.started_at.isoformat(),
            "segment_ended_at": segment.ended_at.isoformat(),
            "media_duration_seconds": round(segment.media_duration, 3),
        }

    def _capture_window(
        self,
        fallback_start: datetime,
        fallback_end: datetime,
        capture_health: dict,
    ) -> tuple[datetime, datetime]:
        try:
            started_at = datetime.fromtimestamp(
                float(capture_health["capture_started_at"]),
                self.tz,
            )
            ended_at = datetime.fromtimestamp(
                float(capture_health["capture_ended_at"]),
                self.tz,
            )
        except (KeyError, TypeError, ValueError, OSError):
            return fallback_start, fallback_end
        if ended_at <= started_at:
            return fallback_start, fallback_end
        clamped_start = max(fallback_start, started_at)
        clamped_end = min(fallback_end, ended_at)
        if clamped_end <= clamped_start:
            return fallback_start, fallback_end
        return clamped_start, clamped_end

    def _build_recording_timeline(
        self,
        session: LiveSession,
        segments: list[RecordingSegment],
    ) -> list[dict[str, float | int]]:
        media_cursor = 0.0
        timeline: list[dict[str, float | int]] = []
        for segment in segments:
            wall_start = max(
                0.0,
                (segment.started_at - session.started_at).total_seconds(),
            )
            wall_end = max(
                wall_start,
                (segment.ended_at - session.started_at).total_seconds(),
            )
            media_end = media_cursor + max(0.0, segment.media_duration)
            timeline.append(
                {
                    "segment_index": segment.index,
                    "wall_start": round(wall_start, 3),
                    "wall_end": round(wall_end, 3),
                    "media_start": round(media_cursor, 3),
                    "media_end": round(media_end, 3),
                }
            )
            media_cursor = media_end
        return timeline

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
                LOGGER.warning(
                    "Processing task cancelled for %s",
                    session.job_id,
                )
            except Exception:
                LOGGER.exception(
                    "Processing task crashed for %s",
                    session.job_id,
                )

        task.add_done_callback(discard)
        LOGGER.info(
            "Queued processing for %s job=%s",
            session.room.name,
            session.job_id,
        )

    async def _process_session(self, session: LiveSession) -> None:
        async with self._processing_semaphore():
            try:
                await to_thread(self._process_recording, session)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception(
                    "Processing failed for %s: %s",
                    session.room.name,
                    exc,
                )
                self._append_job_event(
                    session,
                    "processing_failed",
                    {"error": str(exc)},
                )

    async def _drain_processing_tasks(self) -> None:
        while self._processing_tasks:
            tasks = tuple(self._processing_tasks)
            LOGGER.info(
                "Waiting for %d queued processing task(s) to finish",
                len(tasks),
            )
            await asyncio.gather(*tasks, return_exceptions=True)

    def _processing_semaphore(self) -> asyncio.Semaphore:
        if self.processing_sem is None:
            self.processing_sem = asyncio.Semaphore(
                max(1, self.config.service.processing_parallelism)
            )
        return self.processing_sem

    def _stop_event(self) -> asyncio.Event:
        if self._stop is None:
            self._stop = asyncio.Event()
        return self._stop

    async def _get_live_status(self, room: RoomConfig) -> LiveStatus:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.status_executor,
            self.showroom.get_live_status,
            room,
        )

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
        started_at = (
            started_at
            or self._started_at_from_job_id(job_id)
            or datetime.fromtimestamp(raw_file.stat().st_mtime, self.tz)
        )
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=self.tz)
        ended_at = ended_at or datetime.fromtimestamp(
            raw_file.stat().st_mtime,
            self.tz,
        )
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
        LOGGER.info(
            "Resuming processing from raw recording: room=%s job=%s",
            room.name,
            job_id,
        )
        self._append_job_event(
            session,
            "processing_resumed",
            {"raw_file": str(raw_file)},
        )
        try:
            self._process_recording(session)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception(
                "Processing failed for %s: %s",
                room.name,
                exc,
            )
            self._append_job_event(
                session,
                "processing_failed",
                {"error": str(exc)},
            )
            raise

    def _resolve_recording_room(
        self,
        raw_file: Path,
        room_ref: str | None,
    ) -> RoomConfig:
        refs = []
        if room_ref:
            refs.append(room_ref)
        if raw_file.parent.parent.name:
            refs.append(raw_file.parent.parent.name)
        refs.append(raw_file.parent.name.rsplit("_", 1)[-1])
        normalized_refs = {
            str(item).strip() for item in refs if str(item).strip()
        }
        for room in self.config.rooms:
            candidates = {
                room.name,
                str(room.room_id or ""),
                slugify(room.name),
            }
            if normalized_refs.intersection(candidates):
                return room
        raise ValueError(
            "Could not match raw recording to a configured room. "
            f"Pass --room. Tried: {sorted(normalized_refs)}"
        )

    def _started_at_from_job_id(
        self,
        job_id: str,
    ) -> datetime | None:
        match = re.match(r"^(\d{8}_\d{6})", job_id)
        if not match:
            return None
        return datetime.strptime(
            match.group(1),
            "%Y%m%d_%H%M%S",
        ).replace(tzinfo=self.tz)

    def _room_key(self, room: RoomConfig) -> str:
        return str(room.room_id or room.url or room.name)

    def _record_retry_allowed(self, room: RoomConfig) -> bool:
        retry_at = self._record_retry_after.get(
            self._room_key(room),
            0.0,
        )
        return time.monotonic() >= retry_at

    def _set_record_retry_cooldown(self, room: RoomConfig) -> None:
        cooldown = max(
            0,
            int(self.config.service.record_retry_cooldown_seconds),
        )
        if cooldown <= 0:
            return
        self._record_retry_after[self._room_key(room)] = (
            time.monotonic() + cooldown
        )
        LOGGER.warning(
            "Recording retry for %s is cooled down for %d second(s)",
            room.name,
            cooldown,
        )

    def _process_recording(self, session: LiveSession) -> None:
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
        file_stem = slugify(
            render_template(
                self.config.naming.filename_template,
                context,
            )
        )
        processed_dir = (
            self.config.paths.processed_dir / slugify(session.room.name)
        )

        if self.config.transcode.enabled:
            mp4_path = unique_path(processed_dir, file_stem, ".mp4")
            session.mp4_file = self.media.transcode(
                session.raw_file,
                mp4_path,
            )
        else:
            mp4_path = unique_path(
                processed_dir,
                file_stem,
                session.raw_file.suffix,
            )
            shutil.copy2(session.raw_file, mp4_path)
            session.mp4_file = mp4_path
        self._append_job_event(
            session,
            "transcoded",
            {"mp4_file": str(session.mp4_file)},
        )

        segments: list[SubtitleSegment] = []
        if self.config.asr.enabled:
            segments = self.transcriber.transcribe(session.mp4_file)
            subtitle_dir = (
                self.config.paths.subtitles_dir
                / slugify(session.room.name)
            )
            session.ja_srt_file = unique_path(
                subtitle_dir,
                f"{file_stem}.ja",
                ".srt",
            )
            write_srt(
                session.ja_srt_file,
                segments,
                language="ja",
                max_line_chars=self.config.subtitles.max_line_chars,
            )
            write_transcript_json(
                subtitle_dir / f"{file_stem}.transcript.json",
                segments,
            )
            self._append_job_event(
                session,
                "asr_done",
                {"ja_srt_file": str(session.ja_srt_file)},
            )

            segments = self.translator.translate(segments)
            session.zh_srt_file = unique_path(
                subtitle_dir,
                f"{file_stem}.zh",
                ".srt",
            )
            write_srt(
                session.zh_srt_file,
                segments,
                language="zh",
                max_line_chars=self.config.subtitles.max_line_chars,
                bilingual=self.config.subtitles.bilingual,
            )
            write_transcript_json(
                subtitle_dir / f"{file_stem}.translated.json",
                segments,
            )
            self._append_job_event(
                session,
                "translation_done",
                {"zh_srt_file": str(session.zh_srt_file)},
            )

            if self.config.subtitles.burn_in:
                subtitled_path = unique_path(
                    processed_dir,
                    f"{file_stem}.subtitled",
                    ".mp4",
                )
                session.mp4_file = self.media.burn_subtitles(
                    session.mp4_file,
                    session.zh_srt_file,
                    subtitled_path,
                )
                self._append_job_event(
                    session,
                    "subtitles_burned",
                    {"mp4_file": str(session.mp4_file)},
                )

        self._append_job_event(
            session,
            "processing_done",
            {
                "mp4_file": str(session.mp4_file),
                "ja_srt_file": (
                    str(session.ja_srt_file)
                    if session.ja_srt_file
                    else None
                ),
                "zh_srt_file": (
                    str(session.zh_srt_file)
                    if session.zh_srt_file
                    else None
                ),
            },
        )

    def _preflight(self) -> None:
        if self.config.record.strategy == "yt_dlp":
            if (
                shutil.which(self.config.record.yt_dlp_bin) is None
                and importlib.util.find_spec("yt_dlp") is None
            ):
                raise RuntimeError(
                    "yt-dlp is required. Install dependencies with: "
                    "pip install -r requirements.txt"
                )
        assert_tool_available(self.config.transcode.ffmpeg_bin)
        assert_tool_available(self.media.ffprobe_bin())
        if (
            self.config.asr.enabled
            and self.config.asr.provider
            in {"openai", "openai_compatible"}
        ):
            if not os.getenv(self.config.asr.api_key_env, ""):
                raise RuntimeError(
                    "OpenAI-compatible ASR requires environment variable "
                    f"{self.config.asr.api_key_env}"
                )
        if (
            self.config.asr.enabled
            and self.config.asr.provider == "faster_whisper"
        ):
            self._assert_python_package(
                "faster_whisper",
                "Local ASR requires faster-whisper. Run with "
                ".\\.venv\\Scripts\\python.exe or install local model "
                "dependencies with: pip install -r requirements-local.txt",
            )
        if (
            self.config.translation.enabled
            and self.config.translation.provider == "openai_responses"
        ):
            cfg = self.config.translation.openai_responses
            api_key_env = str(
                cfg.get("api_key_env", "OPENAI_API_KEY")
            )
            if not os.getenv(api_key_env, ""):
                raise RuntimeError(
                    "OpenAI translation requires environment variable "
                    f"{api_key_env}"
                )
        if (
            self.config.translation.enabled
            and self.config.translation.provider
            == "transformers_seq2seq"
        ):
            for package in ("torch", "transformers"):
                self._assert_python_package(
                    package,
                    "Local translation requires torch and transformers. "
                    "Run with .\\.venv\\Scripts\\python.exe or install local "
                    "model dependencies with: "
                    "pip install -r requirements-local.txt",
                )

    def _assert_python_package(
        self,
        package: str,
        message: str,
    ) -> None:
        if importlib.util.find_spec(package) is None:
            raise RuntimeError(message)

    def _make_job_id(
        self,
        room: RoomConfig,
        started_at: datetime,
    ) -> str:
        return (
            f"{started_at:%Y%m%d_%H%M%S}_"
            f"{slugify(room.name, 40)}"
        )

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
            "ended_at": (
                session.ended_at.isoformat()
                if session.ended_at
                else None
            ),
            "raw_file": (
                str(session.raw_file) if session.raw_file else None
            ),
            "raw_segments": [
                str(path) for path in session.raw_segments
            ],
            "mp4_file": (
                str(session.mp4_file) if session.mp4_file else None
            ),
            "ja_srt_file": (
                str(session.ja_srt_file)
                if session.ja_srt_file
                else None
            ),
            "zh_srt_file": (
                str(session.zh_srt_file)
                if session.zh_srt_file
                else None
            ),
        }
        if extra:
            payload.update(extra)
        self.config.paths.jobs_log.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        with self.config.paths.jobs_log.open(
            "a",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
