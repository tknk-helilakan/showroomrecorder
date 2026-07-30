from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from showroomrecorder.config import load_config
from showroomrecorder.models import LiveSession, SubtitleSegment
from showroomrecorder.runner import ShowroomRecorderService


class _FakeMedia:
    def __init__(self) -> None:
        self.transcoded: list[Path] = []
        self.burned: list[Path] = []

    def transcode(self, _source: Path, output: Path) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mp4")
        self.transcoded.append(output)
        return output

    def burn_subtitles(
        self,
        _source: Path,
        _subtitle: Path,
        output: Path,
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"subtitled")
        self.burned.append(output)
        return output


class _FakeTranscriber:
    def transcribe(self, _media_file: Path) -> list[SubtitleSegment]:
        return [
            SubtitleSegment(
                index=1,
                start=0.0,
                end=1.5,
                text="こんにちは",
            )
        ]


class _FakeTranslator:
    def translate(
        self,
        segments: list[SubtitleSegment],
    ) -> list[SubtitleSegment]:
        segments[0].translation = "你好"
        return segments


class LitePipelineTests(unittest.TestCase):
    def test_full_config_legacy_sections_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            config_path.write_text(
                """
service:
  data_dir: data
  upload_recovery_enabled: true
  upload_recovery_time: "03:00"
  upload_recovery_stale_minutes: 120
rooms:
  - name: test-room
    url: https://www.showroom-live.com/r/test
    enabled: true
naming:
  filename_template: "{streamer}_{job_id}"
  part_title_template: "legacy upload title"
danmaku:
  enabled: true
upload:
  enabled: true
  uploader: biliup
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertFalse(hasattr(config, "danmaku"))
            self.assertFalse(hasattr(config, "upload"))
            self.assertFalse(hasattr(config.paths, "danmaku_dir"))
            self.assertFalse(hasattr(config.paths, "upload_dir"))
            self.assertEqual(
                config.naming.filename_template,
                "{streamer}_{job_id}",
            )
            self.assertFalse((root / "data" / "danmaku").exists())
            self.assertFalse((root / "data" / "upload").exists())

    def test_processing_finishes_with_local_outputs_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_file = root / "raw" / "recording.mkv"
            raw_file.parent.mkdir(parents=True)
            raw_file.write_bytes(b"raw")

            service = object.__new__(ShowroomRecorderService)
            service.config = SimpleNamespace(
                naming=SimpleNamespace(
                    filename_template="{streamer}_{job_id}",
                ),
                transcode=SimpleNamespace(enabled=True),
                asr=SimpleNamespace(enabled=True),
                subtitles=SimpleNamespace(
                    max_line_chars=24,
                    bilingual=False,
                    burn_in=True,
                ),
                paths=SimpleNamespace(
                    processed_dir=root / "processed",
                    subtitles_dir=root / "subtitles",
                    jobs_log=root / "jobs.jsonl",
                ),
            )
            service.tz = timezone.utc
            service.media = _FakeMedia()
            service.transcriber = _FakeTranscriber()
            service.translator = _FakeTranslator()
            session = LiveSession(
                room=SimpleNamespace(
                    name="test-room",
                    url="https://example.test/room",
                    room_id=123,
                ),
                job_id="20260730_120000_test-room",
                started_at=datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 30, 13, tzinfo=timezone.utc),
                live_title="test live",
                work_dir=root / "work",
                raw_file=raw_file,
            )

            service._process_recording(session)

            events = [
                json.loads(line)
                for line in service.config.paths.jobs_log.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

            self.assertTrue(session.mp4_file and session.mp4_file.exists())
            self.assertTrue(session.mp4_file.name.endswith(".subtitled.mp4"))
            self.assertTrue(session.ja_srt_file and session.ja_srt_file.exists())
            self.assertTrue(session.zh_srt_file and session.zh_srt_file.exists())
            self.assertEqual(events[-1]["event"], "processing_done")
            self.assertNotIn("upload_file", events[-1])
            self.assertNotIn("bvid", events[-1])
            self.assertNotIn("danmaku_ass_file", events[-1])


if __name__ == "__main__":
    unittest.main()
