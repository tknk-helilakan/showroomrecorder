from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from .config import AppConfig, RoomConfig
from .models import LiveSession
from .showroom import ShowroomClient
from .templating import slugify

LOGGER = logging.getLogger(__name__)


class StreamRecorder:
    def __init__(self, config: AppConfig, showroom: ShowroomClient) -> None:
        self.config = config
        self.showroom = showroom

    def record(self, session: LiveSession) -> Path:
        strategy = self.config.record.strategy.lower()
        if strategy == "yt_dlp":
            if self.config.record.max_seconds:
                LOGGER.info("record.max_seconds is set; using ffmpeg recorder for this capture")
                return self._record_with_ffmpeg(session)
            return self._record_with_ytdlp(session)
        if strategy == "ffmpeg":
            return self._record_with_ffmpeg(session)
        raise ValueError(f"Unsupported record.strategy: {self.config.record.strategy}")

    def _record_with_ytdlp(self, session: LiveSession) -> Path:
        bin_name = self.config.record.yt_dlp_bin
        command_prefix = self._yt_dlp_command_prefix(bin_name)
        room = session.room
        capture_dir = self._capture_dir(session)
        output_template = capture_dir / "recording.%(ext)s"
        command = [
            *command_prefix,
            "--newline",
            "--no-playlist",
            "--hls-use-mpegts",
            "-o",
            str(output_template),
        ]
        cookies_file = room.cookies_file or self.config.record.cookies_file
        if cookies_file:
            command.extend(["--cookies", str(cookies_file)])
        command.extend(self.config.record.extra_args)
        command.append(room.url)
        self._run_record_command(command, capture_dir / "yt-dlp.log")
        return self._find_recorded_file(capture_dir)

    def _record_with_ffmpeg(self, session: LiveSession) -> Path:
        urls = self.showroom.get_streaming_urls(session.room)
        if not urls:
            raise RuntimeError(f"No streaming URL returned for room {session.room.name}")
        stream_url = self._choose_stream_url(urls)
        capture_dir = self._capture_dir(session)
        output_file = capture_dir / "recording.ts"
        command = [
            self.config.transcode.ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-user_agent",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "-headers",
            "Referer: https://www.showroom-live.com/\r\nOrigin: https://www.showroom-live.com\r\n",
            "-i",
            stream_url,
        ]
        if self.config.record.max_seconds:
            command.extend(["-t", str(self.config.record.max_seconds)])
        command.extend(
            [
                "-c",
                "copy",
                str(output_file),
            ]
        )
        self._run_record_command(command, capture_dir / "ffmpeg-record.log")
        return self._find_recorded_file(capture_dir)

    def _capture_dir(self, session: LiveSession) -> Path:
        directory = self.config.paths.raw_dir / slugify(session.room.name) / session.job_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _run_record_command(self, command: list[str], log_file: Path) -> None:
        LOGGER.info("Starting recording command: %s", " ".join(command))
        with log_file.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                LOGGER.info("[record] %s", line.rstrip())
            code = process.wait()
        if code != 0:
            raise RuntimeError(f"Recording command failed with exit code {code}. See log: {log_file}")

    def _find_recorded_file(self, capture_dir: Path) -> Path:
        candidates = [
            item
            for item in capture_dir.iterdir()
            if item.is_file()
            and item.suffix.lower() not in {".log", ".part", ".ytdl", ".json"}
            and not item.name.endswith(".part-Frag")
        ]
        if not candidates:
            raise RuntimeError(f"No recording output found in {capture_dir}")
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        selected = candidates[0]
        min_bytes = int(self.config.record.min_file_size_mb * 1024 * 1024)
        if selected.stat().st_size < min_bytes:
            raise RuntimeError(
                f"Recording output is too small: {selected} ({selected.stat().st_size} bytes)"
            )
        LOGGER.info("Recording saved: %s", selected)
        return selected

    def _choose_stream_url(self, urls: list[str]) -> str:
        hls = [url for url in urls if ".m3u8" in url or "hls" in url.lower()]
        for marker in ("_main_mm.m3u8", "_main_ll.m3u8", "_main_ss.m3u8"):
            for url in hls:
                if marker in url:
                    return url
        concrete_hls = [url for url in hls if "_abr" not in url]
        if concrete_hls:
            return concrete_hls[0]
        return hls[0] if hls else urls[0]

    def _yt_dlp_command_prefix(self, bin_name: str) -> list[str]:
        if shutil.which(bin_name) is not None:
            return [bin_name]
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"yt-dlp executable not found in PATH and yt_dlp module is not installed: {bin_name}"
            ) from exc
        return [sys.executable, "-m", "yt_dlp"]
