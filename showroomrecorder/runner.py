from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from .compat import ZoneInfo, to_thread
from .config import AppConfig, RoomConfig
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
        self.media = MediaProcessor(config.transcode)
        self.transcriber = create_transcriber(config.asr, config.transcode.ffmpeg_bin)
        self.translator = Translator(config.translation)
        self.uploader = BiliupUploader(config)
        self.processing_sem = asyncio.Semaphore(max(1, config.service.processing_parallelism))
        self._stop = asyncio.Event()

    async def run(self, once: bool = False) -> None:
        self._preflight()
        LOGGER.info("Watching %d SHOWROOM room(s)", len(self.config.rooms))
        tasks = [asyncio.create_task(self._watch_room(room, once=once)) for room in self.config.rooms]
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            LOGGER.info("Stopping service")
            self._stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_room(self, room: RoomConfig, once: bool = False) -> None:
        LOGGER.info("Watcher started for %s", room.name)
        while not self._stop.is_set():
            try:
                status = await to_thread(self.showroom.get_live_status, room)
                if status.is_live:
                    await self._handle_live(room, status)
                elif once:
                    LOGGER.info("%s is not live", room.name)
                else:
                    LOGGER.debug("%s is not live", room.name)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Watcher error for %s: %s", room.name, exc)
            if once:
                break
            await asyncio.sleep(room.poll_interval_seconds or self.config.service.poll_interval_seconds)

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

        try:
            session.raw_file = await to_thread(self.recorder.record, session)
            session.ended_at = datetime.now(self.tz)
            self._append_job_event(session, "recorded", {"raw_file": str(session.raw_file)})
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Recording failed for %s: %s", room.name, exc)
            self._append_job_event(session, "record_failed", {"error": str(exc)})
            return

        async with self.processing_sem:
            try:
                await to_thread(self._process_and_upload, session)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Processing failed for %s: %s", room.name, exc)
                self._append_job_event(session, "processing_failed", {"error": str(exc)})
                return

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

        if self.config.transcode.enabled:
            mp4_path = unique_path(
                self.config.paths.processed_dir / slugify(session.room.name),
                file_stem,
                ".mp4",
            )
            session.mp4_file = self.media.transcode(session.raw_file, mp4_path)
        else:
            mp4_path = unique_path(
                self.config.paths.processed_dir / slugify(session.room.name),
                file_stem,
                session.raw_file.suffix,
            )
            shutil.copy2(session.raw_file, mp4_path)
            session.mp4_file = mp4_path
        self._append_job_event(session, "transcoded", {"mp4_file": str(session.mp4_file)})

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
            return self.media.burn_subtitles(session.mp4_file, session.zh_srt_file, output_file)

        output_file = unique_path(upload_dir, upload_stem, ".mp4")
        shutil.copy2(session.mp4_file, output_file)
        if mode == "sidecar" and session.zh_srt_file:
            shutil.copy2(session.zh_srt_file, output_file.with_suffix(".zh.srt"))
        return output_file

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

        candidates: list[Path] = []
        for path in (session.raw_file, session.mp4_file, session.ja_srt_file, session.zh_srt_file):
            if path:
                candidates.append(path)
        if session.mp4_file:
            candidates.extend(session.mp4_file.parent.glob(f"{session.mp4_file.stem}.asr*"))
        subtitle_dir = self.config.paths.subtitles_dir / slugify(session.room.name)
        candidates.extend(subtitle_dir.glob(f"{file_stem}*"))
        candidates.append(session.work_dir)

        for path in candidates:
            removed.extend(self._remove_cleanup_path(path, keep))

        if self.config.upload.keep_latest_upload_per_room and session.upload_file:
            upload_dir = session.upload_file.parent
            for path in upload_dir.iterdir():
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
        if self.config.translation.enabled and self.config.translation.provider == "openai_responses":
            cfg = self.config.translation.openai_responses
            api_key_env = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
            if not os.getenv(api_key_env, ""):
                raise RuntimeError(
                    f"OpenAI translation requires environment variable {api_key_env}"
                )
        if self.config.upload.enabled:
            bin_name = str(self.config.upload.biliup.get("bin", "biliup"))
            assert_tool_available(bin_name)

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
            "upload_file": str(session.upload_file) if session.upload_file else None,
            "bvid": session.bvid,
        }
        if extra:
            payload.update(extra)
        self.config.paths.jobs_log.parent.mkdir(parents=True, exist_ok=True)
        with self.config.paths.jobs_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
