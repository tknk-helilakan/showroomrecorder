from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import TranscodeConfig

LOGGER = logging.getLogger(__name__)


class MediaProcessor:
    def __init__(self, config: TranscodeConfig) -> None:
        self.config = config

    def transcode(self, input_file: Path, output_file: Path) -> Path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        vf = self._video_filter()
        command = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-y",
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
                "-movflags",
                "+faststart",
            ]
        )
        command.extend(self.config.extra_args)
        command.append(str(output_file))
        _run(command, output_file.with_suffix(".ffmpeg.log"))
        return output_file

    def burn_subtitles(self, input_file: Path, subtitle_file: Path, output_file: Path) -> Path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        subtitle_filter = f"subtitles={_escape_subtitle_path(subtitle_file)}"
        command = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-y",
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
            "-movflags",
            "+faststart",
            str(output_file),
        ]
        _run(command, output_file.with_suffix(".hardsub.log"))
        return output_file

    def _video_filter(self) -> str:
        filters: list[str] = []
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

