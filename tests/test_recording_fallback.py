from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from showroomrecorder.cli import parse_args
from showroomrecorder.recorder import StreamRecorder


class YtDlpFallbackTests(unittest.TestCase):
    def test_worker_arguments_are_forwarded_by_cli_parser(self) -> None:
        args = parse_args(
            [
                "--yt-dlp-worker",
                "--newline",
                "--no-playlist",
                "https://www.showroom-live.com/r/example",
            ]
        )

        self.assertEqual(
            args.yt_dlp_worker,
            [
                "--newline",
                "--no-playlist",
                "https://www.showroom-live.com/r/example",
            ],
        )

    @patch("showroomrecorder.recorder.shutil.which", return_value=None)
    def test_frozen_build_uses_internal_worker(self, _which: object) -> None:
        recorder = object.__new__(StreamRecorder)

        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\app\showroomrecorder.exe"
        ):
            prefix = recorder._yt_dlp_command_prefix("yt-dlp")

        self.assertEqual(
            prefix,
            [r"C:\app\showroomrecorder.exe", "--yt-dlp-worker"],
        )

    @patch("showroomrecorder.recorder.shutil.which", return_value=None)
    def test_python_build_uses_module_entrypoint(self, _which: object) -> None:
        recorder = object.__new__(StreamRecorder)

        with patch.object(sys, "frozen", False, create=True), patch.object(
            sys, "executable", r"C:\Python\python.exe"
        ):
            prefix = recorder._yt_dlp_command_prefix("yt-dlp")

        self.assertEqual(
            prefix,
            [r"C:\Python\python.exe", "-m", "yt_dlp"],
        )

    def test_ytdlp_uses_direct_stream_url_and_test_duration(self) -> None:
        recorder = object.__new__(StreamRecorder)
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder.config = SimpleNamespace(
                record=SimpleNamespace(
                    yt_dlp_bin="missing-yt-dlp",
                    cookies_file=None,
                    max_seconds=60,
                    extra_args=[],
                ),
                paths=SimpleNamespace(raw_dir=Path(temp_dir)),
            )
            recorder.showroom = SimpleNamespace(
                session=SimpleNamespace(cookies=[]),
            )
            session = SimpleNamespace(
                room=SimpleNamespace(
                    name="test-room",
                    url="https://www.showroom-live.com/r/test-room",
                    cookies_file=None,
                ),
                job_id="test-job",
            )
            stream_url = "https://cdn.example/live/test.m3u8"
            recorded_file = Path(temp_dir) / "recording.ts"

            with patch.object(
                recorder,
                "_yt_dlp_command_prefix",
                return_value=["showroomrecorder.exe", "--yt-dlp-worker"],
            ), patch.object(recorder, "_run_record_command") as run_command, patch.object(
                recorder,
                "_find_recorded_file",
                return_value=recorded_file,
            ):
                result = recorder._record_with_ytdlp(session, stream_url)

        command = run_command.call_args.args[0]
        self.assertEqual(result, recorded_file)
        self.assertEqual(command[-1], stream_url)
        self.assertNotIn(session.room.url, command)
        self.assertIn("ffmpeg:-t 60", command)
        self.assertIn("Referer: https://www.showroom-live.com/", command)

    def test_ffmpeg_failures_fall_back_to_direct_stream_url(self) -> None:
        recorder = object.__new__(StreamRecorder)
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder.config = SimpleNamespace(
                record=SimpleNamespace(
                    max_seconds=None,
                    ffmpeg_fallback_to_ytdlp=True,
                ),
                transcode=SimpleNamespace(ffmpeg_bin="ffmpeg"),
                paths=SimpleNamespace(raw_dir=Path(temp_dir)),
            )
            stream_urls = [
                "https://cdn.example/live/test_main_ll.m3u8",
                "https://cdn.example/live/test_main_mm.m3u8",
            ]
            recorder.showroom = SimpleNamespace(
                get_streaming_urls=lambda _room: stream_urls,
                session=SimpleNamespace(cookies=[]),
            )
            session = SimpleNamespace(
                room=SimpleNamespace(name="test-room"),
                job_id="test-job",
            )
            recorded_file = Path(temp_dir) / "recording.mp4"

            with patch.object(
                recorder,
                "_run_record_command",
                side_effect=RuntimeError("ffmpeg failed"),
            ), patch.object(
                recorder,
                "_record_with_ytdlp",
                return_value=recorded_file,
            ) as ytdlp_record:
                result = recorder._record_with_ffmpeg(session)

        self.assertEqual(result, recorded_file)
        ytdlp_record.assert_called_once_with(
            session,
            "https://cdn.example/live/test_main_mm.m3u8",
        )


if __name__ == "__main__":
    unittest.main()
