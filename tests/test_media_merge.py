from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from showroomrecorder.media import MediaProcessor


class MediaMergeTests(unittest.TestCase):
    def _processor(self) -> MediaProcessor:
        return MediaProcessor(
            SimpleNamespace(
                ffmpeg_bin="ffmpeg",
                ffprobe_bin="ffprobe",
                width=1280,
                height=720,
                fps=None,
                scale_mode="fit",
                video_codec="libx264",
                preset="veryfast",
                crf=20,
                audio_codec="aac",
                audio_bitrate="128k",
                extra_args=[],
                validate_av_sync=True,
                max_av_desync_seconds=3.0,
            )
        )

    def test_merge_normalizes_each_segment_before_concat(self) -> None:
        processor = self._processor()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "segment 1.ts"
            second = root / "segment 2.ts"
            output = root / "recording-merged.mkv"
            first.write_bytes(b"one")
            second.write_bytes(b"two")

            def create_output(command: list[str], _log_file: Path) -> None:
                Path(command[-1]).write_bytes(b"merged")

            with (
                patch("showroomrecorder.media._run", side_effect=create_output) as run,
                patch.object(processor, "validate_av_sync") as validate,
            ):
                result = processor.merge_recording_segments([first, second], output)

            command = run.call_args.args[0]
            validate.assert_called_once_with(output)

        self.assertEqual(result, output)
        self.assertEqual(command[command.index("-fflags") + 1], "+genpts+discardcorrupt")
        self.assertEqual(command[command.index("-avoid_negative_ts") + 1], "make_zero")
        self.assertEqual(command.count("-i"), 2)
        self.assertIn(str(first), command)
        self.assertIn(str(second), command)
        filter_complex = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:v:0]settb=AVTB,setpts=PTS-STARTPTS", filter_complex)
        self.assertIn("[1:a:0]asetpts=PTS-STARTPTS", filter_complex)
        self.assertIn("concat=n=2:v=1:a=1[vout][aout]", filter_complex)
        self.assertEqual(command[command.index("-enc_time_base:v") + 1], "1:60")
        self.assertNotIn("copy", command)

    def test_transcode_rebuilds_video_and_audio_timestamps(self) -> None:
        processor = self._processor()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "merged.mkv"
            output = root / "processed.mp4"
            source.write_bytes(b"source")

            def create_output(command: list[str], _log_file: Path) -> None:
                Path(command[-1]).write_bytes(b"processed")

            with (
                patch("showroomrecorder.media._run", side_effect=create_output) as run,
                patch.object(processor, "validate_av_sync") as validate,
            ):
                processor.transcode(source, output)

            command = run.call_args.args[0]
            validate.assert_called_once_with(output)

        video_filter = command[command.index("-vf") + 1]
        self.assertIn("settb=AVTB,setpts=PTS-STARTPTS", video_filter)
        self.assertNotIn("N/FRAME_RATE", video_filter)
        self.assertEqual(
            command[command.index("-af") + 1],
            "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
        )
        self.assertEqual(command[command.index("-fps_mode") + 1], "vfr")
        self.assertEqual(command[command.index("-enc_time_base:v") + 1], "1:60")

    def test_validate_av_sync_accepts_matroska_duration_tags(self) -> None:
        processor = self._processor()
        payload = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "start_time": "0.083",
                    "tags": {"DURATION": "00:42:09.866000000"},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "start_time": "0.062",
                    "tags": {"DURATION": "00:42:09.878000000"},
                },
            ],
            "format": {"duration": "2529.878"},
        }
        completed = subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            media_file = Path(temp_dir) / "merged.mkv"
            media_file.write_bytes(b"media")
            with patch("showroomrecorder.media.subprocess.run", return_value=completed):
                timing = processor.validate_av_sync(media_file)

        self.assertIsNotNone(timing)
        assert timing is not None
        self.assertLess(timing.av_end_delta, 0.1)

    def test_validate_av_sync_blocks_recent_bad_frame_rate_output(self) -> None:
        processor = self._processor()
        payload = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "start_time": "0.100",
                    "duration": "1663.600",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "start_time": "0.078",
                    "duration": "1109.269333",
                },
            ],
            "format": {"duration": "1663.700"},
        }
        completed = subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            media_file = Path(temp_dir) / "bad-output.mp4"
            media_file.write_bytes(b"media")
            with (
                patch("showroomrecorder.media.subprocess.run", return_value=completed),
                self.assertRaisesRegex(
                    RuntimeError,
                    r"delta 554\.353s.*file was retained and upload was blocked",
                ),
            ):
                processor.validate_av_sync(media_file)

    def test_hard_subtitle_pass_preserves_variable_frame_timestamps(self) -> None:
        processor = self._processor()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "processed.mp4"
            subtitle = root / "translated.srt"
            output = root / "upload.mp4"
            source.write_bytes(b"source")
            subtitle.write_text("subtitle", encoding="utf-8")

            def create_output(command: list[str], _log_file: Path) -> None:
                Path(command[-1]).write_bytes(b"hard-subbed")

            with (
                patch("showroomrecorder.media._run", side_effect=create_output) as run,
                patch.object(processor, "validate_av_sync") as validate,
            ):
                processor.burn_subtitles(source, subtitle, output)

            command = run.call_args.args[0]
            validate.assert_called_once_with(output)

        video_filter = command[command.index("-vf") + 1]
        self.assertIn("settb=AVTB,setpts=PTS-STARTPTS", video_filter)
        self.assertEqual(command[command.index("-fps_mode") + 1], "vfr")
        self.assertEqual(command[command.index("-enc_time_base:v") + 1], "1:60")


if __name__ == "__main__":
    unittest.main()
