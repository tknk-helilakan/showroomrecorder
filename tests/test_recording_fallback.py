from __future__ import annotations

import json
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

    def test_streamlink_worker_arguments_are_forwarded_by_cli_parser(self) -> None:
        args = parse_args(
            [
                "--streamlink-worker",
                "--output",
                "recording.ts",
                "https://cdn.example/live/test.m3u8",
                "best",
            ]
        )

        self.assertEqual(
            args.streamlink_worker,
            [
                "--output",
                "recording.ts",
                "https://cdn.example/live/test.m3u8",
                "best",
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

    @patch("showroomrecorder.recorder.shutil.which", return_value=None)
    def test_frozen_build_uses_internal_streamlink_worker(self, _which: object) -> None:
        recorder = object.__new__(StreamRecorder)

        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\app\showroomrecorder.exe"
        ):
            prefix = recorder._streamlink_command_prefix("streamlink")

        self.assertEqual(
            prefix,
            [r"C:\app\showroomrecorder.exe", "--streamlink-worker"],
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
                    hls_concurrent_fragments=8,
                    hls_fragment_retries=5,
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
            ), patch.object(
                recorder,
                "_run_record_command",
                return_value=60.0,
            ) as run_command, patch.object(
                recorder,
                "_find_recorded_file",
                return_value=recorded_file,
            ), patch.object(
                recorder,
                "_write_capture_health_report",
            ):
                result = recorder._record_with_ytdlp(session, stream_url)

        command = run_command.call_args.args[0]
        self.assertEqual(result, recorded_file)
        self.assertEqual(command[-1], stream_url)
        self.assertNotIn(session.room.url, command)
        self.assertIn("ffmpeg:-t 60", command)
        self.assertIn("Referer: https://www.showroom-live.com/", command)
        self.assertEqual(command[command.index("--fragment-retries") + 1], "5")

    def test_streamlink_command_uses_parallel_segments_and_duration_limit(self) -> None:
        recorder = object.__new__(StreamRecorder)
        recorder.config = SimpleNamespace(
            record=SimpleNamespace(
                streamlink_bin="streamlink",
                streamlink_extra_args=[],
                hls_concurrent_fragments=8,
                hls_fragment_retries=5,
                max_seconds=60,
            ),
        )
        recorder.showroom = SimpleNamespace(session=SimpleNamespace(cookies=[]))

        with patch.object(
            recorder,
            "_streamlink_command_prefix",
            return_value=["showroomrecorder.exe", "--streamlink-worker"],
        ):
            command = recorder._streamlink_record_command(
                "https://cdn.example/live/test.m3u8",
                Path("recording.ts"),
            )

        self.assertEqual(command[command.index("--stream-segment-threads") + 1], "8")
        self.assertEqual(command[command.index("--stream-segment-attempts") + 1], "5")
        self.assertEqual(command[command.index("--stream-segmented-duration") + 1], "60")
        self.assertIn("Referer=https://www.showroom-live.com/", command)
        self.assertEqual(command[-2:], ["https://cdn.example/live/test.m3u8", "best"])

    def test_ffmpeg_failures_fall_back_to_direct_stream_url(self) -> None:
        recorder = object.__new__(StreamRecorder)
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder.config = SimpleNamespace(
                record=SimpleNamespace(
                    max_seconds=None,
                    ffmpeg_fallback_to_ytdlp=True,
                    hls_fragment_retries=5,
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

    def test_ffmpeg_hls_command_enables_parallel_requests_and_segment_retries(self) -> None:
        recorder = object.__new__(StreamRecorder)
        recorder.config = SimpleNamespace(
            record=SimpleNamespace(max_seconds=None, hls_fragment_retries=5),
            transcode=SimpleNamespace(ffmpeg_bin="ffmpeg"),
        )
        recorder.showroom = SimpleNamespace(session=SimpleNamespace(cookies=[]))

        command = recorder._ffmpeg_record_command(
            "https://cdn.example/live/test.m3u8",
            Path("recording.ts"),
        )

        self.assertEqual(command[command.index("-http_persistent") + 1], "1")
        self.assertEqual(command[command.index("-http_multiple") + 1], "1")
        self.assertEqual(command[command.index("-seg_max_retry") + 1], "5")

    def test_capture_health_report_marks_low_realtime_ratio(self) -> None:
        recorder = object.__new__(StreamRecorder)
        recorder.config = SimpleNamespace(
            record=SimpleNamespace(capture_realtime_ratio_warning=0.95),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            media_file = Path(temp_dir) / "recording.ts"
            media_file.write_bytes(b"test")
            with patch.object(recorder, "_probe_duration", return_value=84.7), self.assertLogs(
                "showroomrecorder.recorder",
                level="WARNING",
            ):
                recorder._write_capture_health_report(media_file, 100.0, recorder="streamlink")

            report = json.loads(
                media_file.with_suffix(".ts.capture.json").read_text(encoding="utf-8")
            )

        self.assertTrue(report["degraded"])
        self.assertEqual(report["realtime_ratio"], 0.847)


if __name__ == "__main__":
    unittest.main()
