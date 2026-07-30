from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import AppConfig, RoomConfig
from .models import LiveSession
from .recording_proxy import RecordingProxyResolver, describe_proxy
from .showroom import ShowroomClient
from .templating import slugify

LOGGER = logging.getLogger(__name__)


class StreamRecorder:
    def __init__(self, config: AppConfig, showroom: ShowroomClient) -> None:
        self.config = config
        self.showroom = showroom
        self.proxy_resolver = RecordingProxyResolver(config.record.proxy)

    def record(self, session: LiveSession, *, segment_index: int | None = None) -> Path:
        strategy = self.config.record.strategy.lower()
        capture_dir = self._capture_dir(session, segment_index=segment_index)
        if strategy == "streamlink":
            return self._record_with_streamlink(session, capture_dir=capture_dir)
        if strategy == "yt_dlp":
            urls = self.showroom.get_streaming_urls(session.room)
            if not urls:
                raise RuntimeError(f"No streaming URL returned for room {session.room.name}")
            return self._record_with_ytdlp(
                session,
                self._ordered_stream_urls(urls)[0],
                capture_dir=capture_dir,
            )
        if strategy == "ffmpeg":
            return self._record_with_ffmpeg(session, capture_dir=capture_dir)
        raise ValueError(f"Unsupported record.strategy: {self.config.record.strategy}")

    def _record_with_ytdlp(
        self,
        session: LiveSession,
        source_url: str | None = None,
        *,
        capture_dir: Path | None = None,
    ) -> Path:
        bin_name = self.config.record.yt_dlp_bin
        command_prefix = self._yt_dlp_command_prefix(bin_name)
        room = session.room
        capture_dir = capture_dir or self._capture_dir(session)
        errors: list[str] = []
        for route_index, proxy_url in enumerate(self._recording_routes(), start=1):
            artifact_suffix = self._route_artifact_suffix(route_index, proxy_url)
            output_template = capture_dir / f"recording{artifact_suffix}.%(ext)s"
            command = [
                *command_prefix,
                "--newline",
                "--no-playlist",
                "--hls-use-mpegts",
                "--fragment-retries",
                str(self.config.record.hls_fragment_retries),
                "-o",
                str(output_template),
            ]
            if proxy_url:
                command.extend(["--proxy", proxy_url])
            for header in self._yt_dlp_input_headers():
                command.extend(["--add-header", header])
            cookies_file = room.cookies_file or self.config.record.cookies_file
            if cookies_file:
                command.extend(["--cookies", str(cookies_file)])
            if self.config.record.max_seconds:
                command.extend(
                    [
                        "--downloader",
                        "ffmpeg",
                        "--downloader-args",
                        f"ffmpeg:-t {self.config.record.max_seconds}",
                    ]
                )
            command.extend(self.config.record.extra_args)
            command.append(source_url or room.url)
            log_file = capture_dir / f"yt-dlp{artifact_suffix}.log"
            LOGGER.info("Trying yt-dlp recording via %s", self._route_label(proxy_url))
            attempt_started_at = time.monotonic()
            try:
                elapsed = self._run_record_command(command, log_file)
                recorded_file = self._find_recorded_file(
                    capture_dir,
                    filename_prefix=f"recording{artifact_suffix}.",
                )
                self._write_capture_health_report(recorded_file, elapsed, recorder="yt-dlp")
                return recorded_file
            except Exception as exc:  # noqa: BLE001
                elapsed = max(0.0, time.monotonic() - attempt_started_at)
                partial = self._find_usable_partial_recording(
                    capture_dir=capture_dir,
                    filename_prefix=f"recording{artifact_suffix}.",
                    wall_duration=elapsed,
                    recorder="yt-dlp",
                    command_error=exc,
                )
                if partial:
                    return partial
                errors.append(f"{self._route_label(proxy_url)}: {exc}")
                LOGGER.warning(
                    "yt-dlp recording failed via %s: %s",
                    self._route_label(proxy_url),
                    exc,
                )

        raise RuntimeError(f"All yt-dlp network routes failed: {'; '.join(errors[-3:])}")

    def _record_with_streamlink(
        self,
        session: LiveSession,
        *,
        capture_dir: Path | None = None,
    ) -> Path:
        urls = self.showroom.get_streaming_urls(session.room)
        if not urls:
            raise RuntimeError(f"No streaming URL returned for room {session.room.name}")
        stream_urls = self._ordered_stream_urls(urls)
        capture_dir = capture_dir or self._capture_dir(session)
        errors: list[str] = []
        for route_index, proxy_url in enumerate(self._recording_routes(), start=1):
            artifact_suffix = self._route_artifact_suffix(route_index, proxy_url)
            for index, stream_url in enumerate(stream_urls, start=1):
                output_file = capture_dir / f"recording-{index:02d}{artifact_suffix}.ts"
                if output_file.exists():
                    output_file.unlink()
                log_file = capture_dir / f"streamlink-record-{index:02d}{artifact_suffix}.log"
                command = self._streamlink_record_command(
                    stream_url,
                    output_file,
                    proxy_url=proxy_url,
                )
                LOGGER.info(
                    "Trying SHOWROOM stream URL %d/%d with Streamlink for %s via %s",
                    index,
                    len(stream_urls),
                    session.room.name,
                    self._route_label(proxy_url),
                )
                attempt_started_at = time.monotonic()
                try:
                    elapsed = self._run_record_command(command, log_file)
                    recorded_file = self._validate_recorded_file(output_file)
                    self._write_capture_health_report(output_file, elapsed, recorder="streamlink")
                    return recorded_file
                except Exception as exc:  # noqa: BLE001
                    elapsed = max(0.0, time.monotonic() - attempt_started_at)
                    partial = self._accept_usable_partial_recording(
                        output_file,
                        wall_duration=elapsed,
                        recorder="streamlink",
                        command_error=exc,
                    )
                    if partial:
                        return partial
                    errors.append(f"{self._route_label(proxy_url)} {stream_url}: {exc}")
                    LOGGER.warning(
                        "Streamlink recording attempt %d/%d failed for %s via %s: %s",
                        index,
                        len(stream_urls),
                        session.room.name,
                        self._route_label(proxy_url),
                        exc,
                    )

        if self.config.record.streamlink_fallback_to_ffmpeg:
            LOGGER.warning(
                "All Streamlink stream URL attempts failed for %s; falling back to FFmpeg",
                session.room.name,
            )
            return self._record_with_ffmpeg(session, capture_dir=capture_dir)

        details = "; ".join(errors[-3:])
        raise RuntimeError(f"All Streamlink recording attempts failed for {session.room.name}: {details}")

    def _streamlink_record_command(
        self,
        stream_url: str,
        output_file: Path,
        *,
        proxy_url: str | None = None,
    ) -> list[str]:
        command = [
            *self._streamlink_command_prefix(self.config.record.streamlink_bin),
            "--loglevel",
            "info",
            "--force",
            "--output",
            str(output_file),
            "--stream-segment-threads",
            str(self.config.record.hls_concurrent_fragments),
            "--stream-segment-attempts",
            str(max(1, self.config.record.hls_fragment_retries)),
        ]
        if proxy_url:
            command.extend(["--http-proxy", proxy_url])
        for header in self._streamlink_input_headers():
            command.extend(["--http-header", header])
        if self.config.record.max_seconds:
            command.extend(["--stream-segmented-duration", str(self.config.record.max_seconds)])
        command.extend(self.config.record.streamlink_extra_args)
        command.extend([stream_url, "best"])
        return command

    def _record_with_ffmpeg(
        self,
        session: LiveSession,
        *,
        capture_dir: Path | None = None,
    ) -> Path:
        urls = self.showroom.get_streaming_urls(session.room)
        if not urls:
            raise RuntimeError(f"No streaming URL returned for room {session.room.name}")
        stream_urls = self._ordered_stream_urls(urls)
        capture_dir = capture_dir or self._capture_dir(session)
        errors: list[str] = []
        for route_index, proxy_url in enumerate(self._recording_routes(), start=1):
            artifact_suffix = self._route_artifact_suffix(route_index, proxy_url)
            for index, stream_url in enumerate(stream_urls, start=1):
                output_file = capture_dir / f"recording-{index:02d}{artifact_suffix}.ts"
                if output_file.exists():
                    output_file.unlink()
                log_file = capture_dir / f"ffmpeg-record-{index:02d}{artifact_suffix}.log"
                command = self._ffmpeg_record_command(
                    stream_url,
                    output_file,
                    proxy_url=proxy_url,
                )
                LOGGER.info(
                    "Trying SHOWROOM stream URL %d/%d for %s via %s",
                    index,
                    len(stream_urls),
                    session.room.name,
                    self._route_label(proxy_url),
                )
                attempt_started_at = time.monotonic()
                try:
                    elapsed = self._run_record_command(command, log_file)
                    recorded_file = self._validate_recorded_file(output_file)
                    self._write_capture_health_report(output_file, elapsed, recorder="ffmpeg")
                    return recorded_file
                except Exception as exc:  # noqa: BLE001
                    elapsed = max(0.0, time.monotonic() - attempt_started_at)
                    partial = self._accept_usable_partial_recording(
                        output_file,
                        wall_duration=elapsed,
                        recorder="ffmpeg",
                        command_error=exc,
                    )
                    if partial:
                        return partial
                    errors.append(f"{self._route_label(proxy_url)} {stream_url}: {exc}")
                    LOGGER.warning(
                        "FFmpeg recording attempt %d/%d failed for %s via %s: %s",
                        index,
                        len(stream_urls),
                        session.room.name,
                        self._route_label(proxy_url),
                        exc,
                    )

        if self.config.record.ffmpeg_fallback_to_ytdlp:
            try:
                LOGGER.warning("All FFmpeg stream URL attempts failed for %s; falling back to yt-dlp", session.room.name)
                return self._record_with_ytdlp(
                    session,
                    stream_urls[0],
                    capture_dir=capture_dir,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"yt-dlp fallback: {exc}")

        details = "; ".join(errors[-3:])
        raise RuntimeError(f"All recording attempts failed for {session.room.name}: {details}")

    def _ffmpeg_record_command(
        self,
        stream_url: str,
        output_file: Path,
        *,
        proxy_url: str | None = None,
    ) -> list[str]:
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
            self._ffmpeg_input_headers(),
            "-http_persistent",
            "1",
            "-http_multiple",
            "1",
            "-seg_max_retry",
            str(self.config.record.hls_fragment_retries),
        ]
        if proxy_url:
            command.extend(["-http_proxy", proxy_url])
        command.extend(["-i", stream_url])
        if self.config.record.max_seconds:
            command.extend(["-t", str(self.config.record.max_seconds)])
        command.extend(
            [
                "-c",
                "copy",
                str(output_file),
            ]
        )
        return command

    def _ffmpeg_input_headers(self) -> str:
        return "\r\n".join(self._yt_dlp_input_headers()) + "\r\n"

    def _yt_dlp_input_headers(self) -> list[str]:
        headers = [
            "Referer: https://www.showroom-live.com/",
            "Origin: https://www.showroom-live.com",
            "Accept: */*",
        ]
        cookie_header = self._showroom_cookie_header()
        if cookie_header:
            headers.append(f"Cookie: {cookie_header}")
        return headers

    def _streamlink_input_headers(self) -> list[str]:
        headers = [
            (
                "User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Referer=https://www.showroom-live.com/",
            "Origin=https://www.showroom-live.com",
            "Accept=*/*",
        ]
        cookie_header = self._showroom_cookie_header()
        if cookie_header:
            headers.append(f"Cookie={cookie_header}")
        return headers

    def _showroom_cookie_header(self) -> str:
        cookies = getattr(self.showroom.session, "cookies", None)
        if not cookies:
            return ""
        values: list[str] = []
        for cookie in cookies:
            if cookie.name and cookie.value:
                values.append(f"{cookie.name}={cookie.value}")
        return "; ".join(values)

    def _capture_dir(self, session: LiveSession, *, segment_index: int | None = None) -> Path:
        directory = self.config.paths.raw_dir / slugify(session.room.name) / session.job_id
        if segment_index is not None:
            directory = directory / "segments" / f"{max(1, int(segment_index)):04d}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _run_record_command(self, command: list[str], log_file: Path) -> float:
        LOGGER.info("Starting recording command: %s", self._format_command_for_log(command))
        started_at = time.monotonic()
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
        elapsed = max(0.0, time.monotonic() - started_at)
        if code != 0:
            raise RuntimeError(f"Recording command failed with exit code {code}. See log: {log_file}")
        return elapsed

    def _find_recorded_file(
        self,
        capture_dir: Path,
        *,
        filename_prefix: str | None = None,
    ) -> Path:
        candidates = [
            item
            for item in capture_dir.iterdir()
            if item.is_file()
            and (filename_prefix is None or item.name.startswith(filename_prefix))
            and item.suffix.lower() not in {".log", ".part", ".ytdl", ".json"}
            and not item.name.endswith(".part-Frag")
        ]
        if not candidates:
            raise RuntimeError(f"No recording output found in {capture_dir}")
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return self._validate_recorded_file(candidates[0])

    def _find_usable_partial_recording(
        self,
        *,
        capture_dir: Path,
        filename_prefix: str,
        wall_duration: float,
        recorder: str,
        command_error: Exception,
    ) -> Path | None:
        try:
            media_file = self._find_recorded_file(
                capture_dir,
                filename_prefix=filename_prefix,
            )
        except (OSError, RuntimeError):
            return None
        return self._accept_usable_partial_recording(
            media_file,
            wall_duration=wall_duration,
            recorder=recorder,
            command_error=command_error,
            already_validated=True,
        )

    def _accept_usable_partial_recording(
        self,
        media_file: Path,
        *,
        wall_duration: float,
        recorder: str,
        command_error: Exception,
        already_validated: bool = False,
    ) -> Path | None:
        if not media_file.exists():
            return None
        try:
            recorded_file = (
                media_file if already_validated else self._validate_recorded_file(media_file)
            )
        except (OSError, RuntimeError):
            return None

        LOGGER.warning(
            "Keeping usable partial %s segment after command failure: %s",
            recorder,
            command_error,
        )
        self._write_capture_health_report(
            recorded_file,
            wall_duration,
            recorder=recorder,
            command_succeeded=False,
            command_error=str(command_error),
        )
        return recorded_file

    def _validate_recorded_file(self, selected: Path) -> Path:
        min_bytes = int(self.config.record.min_file_size_mb * 1024 * 1024)
        if selected.stat().st_size < min_bytes:
            raise RuntimeError(
                f"Recording output is too small: {selected} ({selected.stat().st_size} bytes)"
            )
        self._validate_recording_duration(selected)
        LOGGER.info("Recording saved: %s", selected)
        return selected

    def _validate_recording_duration(self, media_file: Path) -> None:
        min_duration = float(self.config.record.min_duration_seconds or 0.0)
        if min_duration <= 0:
            return
        if self.config.record.max_seconds:
            min_duration = min(min_duration, max(1.0, float(self.config.record.max_seconds) * 0.8))

        duration = self._probe_duration(media_file)
        if duration is None:
            raise RuntimeError(f"Could not probe recording duration: {media_file}")
        if duration < min_duration:
            raise RuntimeError(
                f"Recording output is too short: {media_file} ({duration:.2f}s < {min_duration:.2f}s)"
            )
        LOGGER.info("Recording duration validated: %s %.2fs", media_file, duration)

    def _probe_duration(self, media_file: Path) -> float | None:
        ffprobe = self._ffprobe_bin()
        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(media_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        try:
            duration = float(json.loads(completed.stdout)["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not math.isfinite(duration):
            return None
        return duration

    def probe_duration(self, media_file: Path) -> float | None:
        return self._probe_duration(media_file)

    def read_capture_health_report(self, media_file: Path) -> dict:
        report_file = media_file.with_suffix(media_file.suffix + ".capture.json")
        try:
            payload = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_capture_health_report(
        self,
        media_file: Path,
        wall_duration: float,
        *,
        recorder: str,
        command_succeeded: bool = True,
        command_error: str | None = None,
    ) -> None:
        media_duration = self._probe_duration(media_file)
        captured_at = time.time()
        ratio = media_duration / wall_duration if media_duration is not None and wall_duration > 0 else None
        threshold = self.config.record.capture_realtime_ratio_warning
        degraded = ratio is not None and threshold > 0 and ratio < threshold
        report = {
            "recorder": recorder,
            "media_file": str(media_file),
            "capture_started_at": round(captured_at - max(0.0, wall_duration), 3),
            "capture_ended_at": round(captured_at, 3),
            "wall_duration_seconds": round(wall_duration, 3),
            "media_duration_seconds": round(media_duration, 3) if media_duration is not None else None,
            "realtime_ratio": round(ratio, 6) if ratio is not None else None,
            "warning_threshold": threshold,
            "degraded": degraded,
            "partial": not command_succeeded,
            "command_succeeded": command_succeeded,
            "command_error": command_error,
        }
        report_file = media_file.with_suffix(media_file.suffix + ".capture.json")
        try:
            report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("Could not save capture health report for %s: %s", media_file, exc)
            return
        if degraded:
            LOGGER.warning(
                "Capture health degraded for %s: media %.2fs / wall %.2fs = %.3f (< %.3f)",
                media_file,
                media_duration,
                wall_duration,
                ratio,
                threshold,
            )
        else:
            LOGGER.info(
                "Capture health report saved: %s realtime_ratio=%s",
                report_file,
                f"{ratio:.3f}" if ratio is not None else "unknown",
            )

    def _ffprobe_bin(self) -> str:
        ffmpeg_path = Path(self.config.transcode.ffmpeg_bin)
        if ffmpeg_path.name.lower().startswith("ffmpeg"):
            candidate = ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")
            if candidate.exists():
                return str(candidate)
        return "ffprobe"

    def _choose_stream_url(self, urls: list[str]) -> str:
        return self._ordered_stream_urls(urls)[0]

    def _ordered_stream_urls(self, urls: list[str]) -> list[str]:
        hls = [url for url in urls if ".m3u8" in url or "hls" in url.lower()]
        ordered: list[str] = []
        for marker in ("_main_mm.m3u8", "_main_ll.m3u8", "_main_ss.m3u8"):
            for url in hls:
                if marker in url:
                    ordered.append(url)
        ordered.extend(url for url in hls if "_abr" not in url and url not in ordered)
        ordered.extend(url for url in hls if url not in ordered)
        ordered.extend(url for url in urls if url not in ordered)
        return ordered or urls

    def _recording_routes(self) -> list[str | None]:
        resolver = getattr(self, "proxy_resolver", None)
        if resolver is None:
            proxy_config = getattr(self.config.record, "proxy", None)
            if proxy_config is None:
                return [None]
            resolver = RecordingProxyResolver(proxy_config)
            self.proxy_resolver = resolver
        return resolver.routes()

    def _route_artifact_suffix(self, route_index: int, proxy_url: str | None) -> str:
        if proxy_url is None:
            return ""
        return f"-proxy{route_index:02d}"

    def _route_label(self, proxy_url: str | None) -> str:
        if proxy_url is None:
            return "system/direct route"
        return describe_proxy(proxy_url)

    def _format_command_for_log(self, command: list[str]) -> str:
        redacted: list[str] = []
        for index, item in enumerate(command):
            if index > 0 and command[index - 1] == "-headers":
                redacted.append(self._redact_header_value(item))
            elif (
                index > 0
                and command[index - 1] == "--add-header"
                and item.lower().startswith("cookie:")
            ):
                redacted.append("Cookie: <redacted>")
            elif (
                index > 0
                and command[index - 1] == "--http-header"
                and item.lower().startswith("cookie=")
            ):
                redacted.append("Cookie=<redacted>")
            elif index > 0 and command[index - 1] in {
                "--http-proxy",
                "--proxy",
                "-http_proxy",
            }:
                redacted.append(describe_proxy(item))
            else:
                redacted.append(item)
        return " ".join(redacted)

    def _redact_header_value(self, value: str) -> str:
        lines = value.replace("\r\n", "\n").split("\n")
        redacted = [
            "Cookie: <redacted>" if line.lower().startswith("cookie:") else line
            for line in lines
        ]
        return "\\r\\n".join(redacted)

    def _yt_dlp_command_prefix(self, bin_name: str) -> list[str]:
        if shutil.which(bin_name) is not None:
            return [bin_name]
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"yt-dlp executable not found in PATH and yt_dlp module is not installed: {bin_name}"
            ) from exc
        if getattr(sys, "frozen", False):
            return [sys.executable, "--yt-dlp-worker"]
        return [sys.executable, "-m", "yt_dlp"]

    def _streamlink_command_prefix(self, bin_name: str) -> list[str]:
        if shutil.which(bin_name) is not None:
            return [bin_name]
        try:
            import streamlink  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"Streamlink executable not found in PATH and streamlink module is not installed: {bin_name}"
            ) from exc
        if getattr(sys, "frozen", False):
            return [sys.executable, "--streamlink-worker"]
        return [sys.executable, "-m", "streamlink"]
