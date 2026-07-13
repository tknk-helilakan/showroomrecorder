from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from showroomrecorder.config import AsrConfig
from showroomrecorder.transcription import FasterWhisperTranscriber


class FasterWhisperTranscriberTests(unittest.TestCase):
    def test_live_transcription_options_are_forwarded(self) -> None:
        config = AsrConfig(
            provider="faster_whisper",
            model="large-v3",
            normalize_audio=False,
            language="ja",
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            temperature=0.0,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            word_timestamps=True,
            hallucination_silence_threshold=2.0,
            initial_prompt="高嶺のなでしこ",
            log_progress=False,
        )
        model = Mock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(start=1.0, end=2.5, text=" テスト ")]),
            SimpleNamespace(language="ja", language_probability=0.99, duration=3.0),
        )
        transcriber = FasterWhisperTranscriber(config)

        with patch.object(transcriber, "_load_model", return_value=model):
            segments = transcriber.transcribe(Path("input.mp4"))

        self.assertEqual(segments[0].text, "テスト")
        model.transcribe.assert_called_once_with(
            "input.mp4",
            language="ja",
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            word_timestamps=True,
            log_progress=False,
            temperature=0.0,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            hallucination_silence_threshold=2.0,
            initial_prompt="高嶺のなでしこ",
            vad_parameters={"min_silence_duration_ms": 500},
        )

    def test_normalized_audio_is_wav_16khz_mono_and_overwritten(self) -> None:
        config = AsrConfig(provider="faster_whisper")
        transcriber = FasterWhisperTranscriber(config, ffmpeg_bin="ffmpeg")
        with tempfile.TemporaryDirectory() as temp_dir:
            media_file = Path(temp_dir) / "recording.mp4"
            completed = subprocess.CompletedProcess([], 0)
            with patch("showroomrecorder.transcription.subprocess.run", return_value=completed) as run:
                audio_file = transcriber._prepare_audio(media_file)

        command = run.call_args.args[0]
        self.assertEqual(audio_file.name, "recording.asr.wav")
        self.assertIn("-y", command)
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ar") + 1], "16000")


if __name__ == "__main__":
    unittest.main()
