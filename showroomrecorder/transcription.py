from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from .config import AsrConfig
from .models import SubtitleSegment
from .subtitles import clean_subtitle_text

LOGGER = logging.getLogger(__name__)


def create_transcriber(config: AsrConfig, ffmpeg_bin: str):
    if config.provider in {"openai", "openai_compatible"}:
        return OpenAITranscriber(config, ffmpeg_bin=ffmpeg_bin)
    return FasterWhisperTranscriber(config)


class OpenAITranscriber:
    def __init__(self, config: AsrConfig, ffmpeg_bin: str = "ffmpeg") -> None:
        self.config = config
        self.ffmpeg_bin = ffmpeg_bin

    def transcribe(self, media_file: Path) -> list[SubtitleSegment]:
        api_key = os.getenv(self.config.api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"Environment variable {self.config.api_key_env} is required for OpenAI-compatible ASR"
            )

        chunks_dir = media_file.parent / f"{media_file.stem}.openai_audio_chunks"
        chunks = self._split_audio(media_file, chunks_dir)
        segments: list[SubtitleSegment] = []
        offset = 0.0
        for chunk_index, chunk in enumerate(chunks, start=1):
            duration = self._probe_duration(chunk) or float(self.config.chunk_seconds)
            LOGGER.info(
                "OpenAI ASR chunk %d/%d file=%s duration=%.2fs",
                chunk_index,
                len(chunks),
                chunk.name,
                duration,
            )
            response = self._retry(lambda: self._transcribe_chunk(chunk))
            segments.extend(self._segments_from_response(response, offset, duration))
            offset += duration
        for idx, segment in enumerate(segments, start=1):
            segment.index = idx
        LOGGER.info("OpenAI ASR produced %d subtitle segments", len(segments))
        return segments

    def _split_audio(self, media_file: Path, chunks_dir: Path) -> list[Path]:
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        suffix = self.config.audio_format.lower().lstrip(".") or "mp3"
        pattern = chunks_dir / f"chunk_%05d.{suffix}"
        command = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i",
            str(media_file),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame" if suffix == "mp3" else "aac",
            "-b:a",
            self.config.audio_bitrate,
            "-f",
            "segment",
            "-segment_time",
            str(self.config.chunk_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
        ]
        log_file = chunks_dir / "ffmpeg-audio-split.log"
        with log_file.open("w", encoding="utf-8") as log:
            process = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if process.returncode != 0:
            raise RuntimeError(f"Audio split failed with exit code {process.returncode}. See log: {log_file}")
        chunks = sorted(chunks_dir.glob(f"*.{suffix}"))
        if not chunks:
            raise RuntimeError(f"No audio chunks generated in {chunks_dir}")
        max_bytes = int(self.config.max_file_size_mb * 1024 * 1024)
        oversized = [chunk for chunk in chunks if chunk.stat().st_size > max_bytes]
        if oversized:
            names = ", ".join(chunk.name for chunk in oversized[:5])
            raise RuntimeError(
                f"Audio chunk exceeds OpenAI upload budget ({self.config.max_file_size_mb} MB): {names}. "
                "Lower asr.chunk_seconds or asr.audio_bitrate."
            )
        return chunks

    def _transcribe_chunk(self, chunk: Path) -> dict[str, Any]:
        url = f"{self.config.base_url.rstrip('/')}/audio/transcriptions"
        data: dict[str, Any] = {
            "model": self.config.model,
            "language": self.config.language,
            "response_format": self._response_format(),
        }
        if self.config.chunking_strategy and self.config.model.endswith("-diarize"):
            data["chunking_strategy"] = self.config.chunking_strategy
        if self.config.prompt and not self.config.model.endswith("-diarize"):
            data["prompt"] = self.config.prompt

        with chunk.open("rb") as fh:
            session = requests.Session()
            session.trust_env = bool(self.config.trust_env)
            response = session.post(
                url,
                headers=self._headers(),
                data=data,
                files={"file": (chunk.name, fh, self._mime_type(chunk))},
                timeout=self.config.timeout_seconds,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI transcription failed {response.status_code}: {response.text[:1000]}")
        return response.json()

    def _segments_from_response(
        self,
        response: dict[str, Any],
        offset: float,
        chunk_duration: float,
    ) -> list[SubtitleSegment]:
        raw_segments = response.get("segments") or []
        parsed: list[SubtitleSegment] = []
        if raw_segments:
            for raw in raw_segments:
                text = clean_subtitle_text(str(raw.get("text", "")))
                if not text:
                    continue
                start = offset + float(raw.get("start", 0.0))
                end = offset + float(raw.get("end", raw.get("start", 0.0) + 1.0))
                if end <= start:
                    end = start + 1.0
                parsed.append(
                    SubtitleSegment(
                        index=len(parsed) + 1,
                        start=start,
                        end=end,
                        text=text,
                    )
                )
            return parsed

        text = clean_subtitle_text(str(response.get("text", "")))
        if not text:
            return []
        return self._approximate_segments(text, offset, chunk_duration)

    def _approximate_segments(self, text: str, offset: float, duration: float) -> list[SubtitleSegment]:
        pieces = [
            item.strip()
            for item in re.split(r"(?<=[。！？!?])\s*", text)
            if item.strip()
        ]
        if not pieces:
            pieces = [text]
        total_chars = max(1, sum(len(item) for item in pieces))
        cursor = offset
        segments: list[SubtitleSegment] = []
        for piece in pieces:
            piece_duration = max(1.0, duration * len(piece) / total_chars)
            segments.append(
                SubtitleSegment(
                    index=len(segments) + 1,
                    start=cursor,
                    end=min(offset + duration, cursor + piece_duration),
                    text=piece,
                )
            )
            cursor += piece_duration
        return segments

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {os.environ[self.config.api_key_env]}"}
        org = os.getenv(self.config.organization_env, "")
        project = os.getenv(self.config.project_env, "")
        if org:
            headers["OpenAI-Organization"] = org
        if project:
            headers["OpenAI-Project"] = project
        return headers

    def _response_format(self) -> str:
        if self.config.model.endswith("-diarize"):
            return "diarized_json"
        return self.config.response_format or "json"

    def _mime_type(self, chunk: Path) -> str:
        if chunk.suffix.lower() == ".mp3":
            return "audio/mpeg"
        if chunk.suffix.lower() == ".m4a":
            return "audio/mp4"
        return "application/octet-stream"

    def _probe_duration(self, media_file: Path) -> float | None:
        ffprobe = "ffprobe"
        ffmpeg_path = Path(self.ffmpeg_bin)
        if ffmpeg_path.name.lower().startswith("ffmpeg"):
            candidate = ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")
            if candidate.exists():
                ffprobe = str(candidate)
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
        if completed.returncode != 0:
            return None
        try:
            return float(json.loads(completed.stdout)["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _retry(self, func):
        last_exc: Exception | None = None
        for attempt in range(1, int(self.config.retries or 1) + 1):
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= int(self.config.retries or 1):
                    break
                delay = min(60, 2**attempt)
                LOGGER.warning("OpenAI ASR attempt %d failed: %s; retrying in %ss", attempt, exc, delay)
                time.sleep(delay)
        raise last_exc or RuntimeError("OpenAI ASR failed")


class FasterWhisperTranscriber:
    def __init__(self, config: AsrConfig) -> None:
        self.config = config
        self._model = None

    def transcribe(self, media_file: Path) -> list[SubtitleSegment]:
        if self.config.provider != "faster_whisper":
            raise ValueError(f"Unsupported ASR provider: {self.config.provider}")
        model = self._load_model()
        LOGGER.info("Starting ASR with faster-whisper model=%s file=%s", self.config.model, media_file)
        segments_iter, info = model.transcribe(
            str(media_file),
            language=self.config.language,
            beam_size=self.config.beam_size,
            vad_filter=self.config.vad_filter,
        )
        LOGGER.info(
            "ASR detected language=%s probability=%.3f duration=%.2fs",
            getattr(info, "language", "unknown"),
            getattr(info, "language_probability", 0.0),
            getattr(info, "duration", 0.0),
        )
        segments: list[SubtitleSegment] = []
        for idx, segment in enumerate(segments_iter, start=1):
            text = clean_subtitle_text(segment.text)
            if not text:
                continue
            segments.append(
                SubtitleSegment(
                    index=idx,
                    start=float(segment.start),
                    end=float(segment.end),
                    text=text,
                )
            )
        LOGGER.info("ASR produced %d subtitle segments", len(segments))
        return segments

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc

        kwargs = {}
        if self.config.compute_type != "auto":
            kwargs["compute_type"] = self.config.compute_type
        self._model = WhisperModel(
            self.config.model,
            device=self.config.device,
            **kwargs,
        )
        return self._model
