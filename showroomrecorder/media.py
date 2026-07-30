from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import TranscodeConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaTiming:
    video_start: float
    video_duration: float
    audio_start: float
    audio_duration: float
    format_duration: float | None = None

    @property
    def video_end(self) -> float:
        return self.video_start + self.video_duration

    @property
    def audio_end(self) -> float:
        return self.audio_start + self.audio_duration

    @property
    def av_end_delta(self) -> float:
        return abs(self.video_end - self.audio_end)


class MediaProcessor:
    def __init__(self, config: TranscodeConfig) -> None:
        self.config = config

    def merge_recording_segments(self, input_files: list[Path], output_file: Path) -> Path:
        if not input_files:
            raise ValueError("At least one recording segment is required")
        missing = [path for path in input_files if not path.exists()]
        if missing:
            raise FileNotFoundError(f"Recording segment does not exist: {missing[0]}")

        output_file.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-fflags",
            "+genpts+discardcorrupt",
        ]
        for path in input_files:
            command.extend(["-i", str(path)])

        width = _even_dimension(min(int(self.config.width or 640), 640))
        height = _even_dimension(min(int(self.config.height or 360), 360))
        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        for index in range(len(input_files)):
            video_filters = [
                "settb=AVTB",
                "setpts=PTS-STARTPTS",
            ]
            if self.config.fps:
                video_filters.append(f"fps={self.config.fps}")
            video_filters.extend(
                [
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                    "setsar=1",
                    "format=yuv420p",
                ]
            )
            filter_parts.append(f"[{index}:v:0]{','.join(video_filters)}[v{index}]")
            filter_parts.append(
                f"[{index}:a:0]asetpts=PTS-STARTPTS,"
                "aresample=48000:async=1:first_pts=0,"
                f"aformat=sample_rates=48000:channel_layouts=stereo[a{index}]"
            )
            concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
        filter_parts.append(
            f"{''.join(concat_inputs)}concat=n={len(input_files)}:v=1:a=1[vout][aout]"
        )

        merge_crf = max(0, int(self.config.crf) - 2)
        command.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                self.config.video_codec,
                "-preset",
                self.config.preset,
                "-crf",
                str(merge_crf),
                "-c:a",
                self.config.audio_codec,
                "-b:a",
                self.config.audio_bitrate,
            ]
        )
        command.extend(self._frame_timing_args())
        command.extend(
            [
                "-avoid_negative_ts",
                "make_zero",
                "-max_muxing_queue_size",
                "4096",
                str(output_file),
            ]
        )
        _run(command, output_file.with_suffix(".merge.log"))
        if not output_file.exists() or output_file.stat().st_size <= 0:
            raise RuntimeError(f"Merged recording was not created: {output_file}")
        self.validate_av_sync(output_file)
        return output_file

    def transcode(self, input_file: Path, output_file: Path) -> Path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        vf = self._video_filter()
        command = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            str(input_file),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
        ]
        if vf:
            command.extend(["-vf", vf])
        command.extend(
            [
                "-c:v",
                self.config.video_codec,
                "-preset",
                self.config.preset,
                "-crf",
                str(self.config.crf),
                "-c:a",
                self.config.audio_codec,
                "-b:a",
                self.config.audio_bitrate,
                "-af",
                "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
            ]
        )
        command.extend(self._frame_timing_args())
        command.extend(
            [
                "-avoid_negative_ts",
                "make_zero",
                "-max_muxing_queue_size",
                "4096",
                "-movflags",
                "+faststart",
            ]
        )
        command.extend(self.config.extra_args)
        command.append(str(output_file))
        _run(command, output_file.with_suffix(".ffmpeg.log"))
        if not output_file.exists() or output_file.stat().st_size <= 0:
            raise RuntimeError(f"Transcoded recording was not created: {output_file}")
        self.validate_av_sync(output_file)
        return output_file

    def burn_subtitles(self, input_file: Path, subtitle_file: Path, output_file: Path) -> Path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        subtitle_filter = (
            "settb=AVTB,setpts=PTS-STARTPTS,"
            f"subtitles={_escape_subtitle_path(subtitle_file)}"
        )
        command = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            str(input_file),
            "-vf",
            subtitle_filter,
            "-c:v",
            self.config.video_codec,
            "-preset",
            self.config.preset,
            "-crf",
            str(self.config.crf),
            "-c:a",
            "copy",
        ]
        command.extend(self._frame_timing_args())
        command.extend(
            [
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
                str(output_file),
            ]
        )
        _run(command, output_file.with_suffix(".hardsub.log"))
        if not output_file.exists() or output_file.stat().st_size <= 0:
            raise RuntimeError(f"Hard-subbed recording was not created: {output_file}")
        self.validate_av_sync(output_file)
        return output_file

    def validate_av_sync(self, media_file: Path) -> MediaTiming | None:
        if not bool(getattr(self.config, "validate_av_sync", True)):
            return None

        timing = self._probe_media_timing(media_file)
        max_delta = max(
            0.1,
            float(getattr(self.config, "max_av_desync_seconds", 3.0) or 3.0),
        )
        LOGGER.info(
            "A/V timing validated: %s video_end=%.3fs audio_end=%.3fs delta=%.3fs",
            media_file,
            timing.video_end,
            timing.audio_end,
            timing.av_end_delta,
        )
        if timing.av_end_delta > max_delta:
            raise RuntimeError(
                "A/V sync validation failed for "
                f"{media_file}: video ends at {timing.video_end:.3f}s, "
                f"audio ends at {timing.audio_end:.3f}s "
                f"(delta {timing.av_end_delta:.3f}s > {max_delta:.3f}s). "
                "The file was retained and processing was stopped."
            )
        return timing

    def ffprobe_bin(self) -> str:
        configured = str(getattr(self.config, "ffprobe_bin", "") or "").strip()
        if configured:
            return configured

        ffmpeg_path = Path(self.config.ffmpeg_bin)
        if ffmpeg_path.name.lower().startswith("ffmpeg"):
            candidate = ffmpeg_path.with_name(
                "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
            )
            if candidate.exists():
                return str(candidate)
        return "ffprobe"

    def _probe_media_timing(self, media_file: Path) -> MediaTiming:
        if not media_file.exists() or media_file.stat().st_size <= 0:
            raise RuntimeError(f"Cannot validate missing or empty media file: {media_file}")

        command = [
            self.ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,start_time,duration:stream_tags=DURATION:format=duration",
            "-of",
            "json",
            str(media_file),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not run ffprobe for A/V validation: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-500:] or "no ffprobe error output"
            raise RuntimeError(
                f"ffprobe failed while validating {media_file}: {detail}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"ffprobe returned invalid JSON while validating {media_file}"
            ) from exc

        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise RuntimeError(f"ffprobe returned no streams for {media_file}")
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        if video is None or audio is None:
            missing = "video" if video is None else "audio"
            raise RuntimeError(
                f"A/V sync validation failed for {media_file}: missing {missing} stream. "
                "The file was retained and processing was stopped."
            )

        video_duration = _stream_duration(video)
        audio_duration = _stream_duration(audio)
        if video_duration is None or audio_duration is None:
            raise RuntimeError(
                f"A/V sync validation failed for {media_file}: ffprobe did not report "
                "usable video and audio durations. The file was retained and processing was stopped."
            )
        return MediaTiming(
            video_start=_finite_float(video.get("start_time")) or 0.0,
            video_duration=video_duration,
            audio_start=_finite_float(audio.get("start_time")) or 0.0,
            audio_duration=audio_duration,
            format_duration=_finite_float((payload.get("format") or {}).get("duration")),
        )

    def _video_filter(self) -> str:
        filters: list[str] = ["settb=AVTB", "setpts=PTS-STARTPTS"]
        width = self.config.width
        height = self.config.height
        if width and height:
            if self.config.scale_mode == "fit":
                filters.append(
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                )
            elif self.config.scale_mode == "fill":
                filters.append(
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}"
                )
            elif self.config.scale_mode == "stretch":
                filters.append(f"scale={width}:{height}")
            else:
                raise ValueError(f"Unsupported transcode.scale_mode: {self.config.scale_mode}")
        if self.config.fps:
            filters.append(f"fps={self.config.fps}")
        filters.append("format=yuv420p")
        return ",".join(filters)

    def _frame_timing_args(self) -> list[str]:
        if self.config.fps:
            return ["-fps_mode", "cfr"]
        # SHOWROOM MPEG-TS metadata often reports 20 fps for a variable 25-40 fps stream.
        # A 60 Hz encoder time base preserves those source timestamps without frame drops.
        return ["-fps_mode", "vfr", "-enc_time_base:v", "1:60"]


def assert_tool_available(bin_name: str) -> None:
    if shutil.which(bin_name) is None:
        raise RuntimeError(f"Required executable not found in PATH: {bin_name}")


def _run(command: list[str], log_file: Path) -> None:
    LOGGER.info("Running command: %s", " ".join(command))
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
            LOGGER.debug(line.rstrip())
        code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}. See log: {log_file}")


def _escape_subtitle_path(path: Path) -> str:
    # FFmpeg filter paths need escaping for Windows drive colons and quotes.
    value = path.resolve().as_posix()
    value = value.replace("\\", "/")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return f"'{value}'"


def _even_dimension(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


def _stream_duration(stream: dict) -> float | None:
    duration = _finite_float(stream.get("duration"))
    if duration is not None and duration > 0:
        return duration
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return None
    return _clock_duration(tags.get("DURATION"))


def _finite_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clock_duration(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None
    duration = hours * 3600 + minutes * 60 + seconds
    return duration if math.isfinite(duration) and duration > 0 else None
