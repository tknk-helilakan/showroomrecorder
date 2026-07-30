from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from showroomrecorder.runner import ShowroomRecorderService
from showroomrecorder.showroom import LiveStatus


class _FakeRecorder:
    def __init__(self, outcomes: list[Path | Exception], durations: dict[str, float]) -> None:
        self.outcomes = list(outcomes)
        self.durations = durations
        self.calls: list[int] = []

    def record(self, _session, *, segment_index: int) -> Path:
        self.calls.append(segment_index)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        outcome.parent.mkdir(parents=True, exist_ok=True)
        outcome.write_bytes(f"segment-{segment_index}".encode("ascii"))
        return outcome

    def probe_duration(self, media_file: Path) -> float | None:
        return self.durations.get(media_file.name)

    def read_capture_health_report(self, _media_file: Path) -> dict:
        return {}


class _FakeMedia:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Path], Path]] = []

    def merge_recording_segments(self, input_files: list[Path], output_file: Path) -> Path:
        self.calls.append((list(input_files), output_file))
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(b"merged")
        return output_file


class LiveSessionTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        temp_dir: str,
        recorder: _FakeRecorder,
        *,
        confirmations: int,
    ) -> ShowroomRecorderService:
        root = Path(temp_dir)
        service = object.__new__(ShowroomRecorderService)
        service.config = SimpleNamespace(
            service=SimpleNamespace(record_retry_cooldown_seconds=180),
            record=SimpleNamespace(
                max_seconds=None,
                reconnect_delay_seconds=0,
                live_end_confirmations=confirmations,
                live_end_check_interval_seconds=0,
            ),
            danmaku=SimpleNamespace(enabled=False),
            paths=SimpleNamespace(
                raw_dir=root / "raw",
                work_dir=root / "work",
                jobs_log=root / "jobs.jsonl",
            ),
        )
        service.tz = timezone.utc
        service.recorder = recorder
        service.media = _FakeMedia()
        service._stop = asyncio.Event()
        service._record_retry_after = {}
        service._processing_tasks = set()
        service._schedule_processing = Mock()
        service._set_record_retry_cooldown = Mock()
        return service

    def _events(self, service: ShowroomRecorderService) -> list[dict]:
        return [
            json.loads(line)
            for line in service.config.paths.jobs_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def test_reconnects_segments_in_one_job_and_processes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "capture" / "first.ts"
            second = root / "capture" / "second.ts"
            recorder = _FakeRecorder(
                [first, second],
                {"first.ts": 8.0, "second.ts": 10.0, "recording-merged.mkv": 18.0},
            )
            service = self._service(temp_dir, recorder, confirmations=3)
            service._get_live_status = AsyncMock(
                side_effect=[
                    LiveStatus(is_live=True, raw={"is_onlive": 1}),
                    LiveStatus(is_live=False, raw={"is_onlive": 0}),
                    LiveStatus(is_live=False, raw={"is_onlive": 0}),
                    LiveStatus(is_live=False, raw={"is_onlive": 0}),
                ]
            )
            room = SimpleNamespace(name="test-room", room_id=123, url="https://example.test/room")

            await service._handle_live(
                room,
                LiveStatus(is_live=True, title="test live", raw={"is_onlive": 1}),
            )

            events = self._events(service)
            scheduled_session = service._schedule_processing.call_args.args[0]

        self.assertEqual(recorder.calls, [1, 2])
        self.assertEqual(len(service.media.calls), 1)
        self.assertEqual(service.media.calls[0][0], [first, second])
        service._schedule_processing.assert_called_once()
        self.assertEqual(scheduled_session.raw_segments, [first, second])
        self.assertEqual(scheduled_session.raw_file.name, "recording-merged.mkv")
        self.assertEqual(len(scheduled_session.metadata["recording_timeline"]), 2)
        self.assertEqual(
            [item["event"] for item in events].count("recorded"),
            1,
        )
        self.assertEqual(
            [item["event"] for item in events].count("processing_queued"),
            1,
        )
        self.assertEqual({item["job_id"] for item in events}, {events[0]["job_id"]})

    async def test_failed_segment_reconnects_without_starting_a_new_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recovered = root / "capture" / "recovered.ts"
            recorder = _FakeRecorder(
                [RuntimeError("temporary CDN failure"), recovered],
                {"recovered.ts": 12.0, "recording-merged.mkv": 12.0},
            )
            service = self._service(temp_dir, recorder, confirmations=2)
            service._get_live_status = AsyncMock(
                side_effect=[
                    LiveStatus(is_live=True, raw={"is_onlive": 1}),
                    LiveStatus(is_live=False, raw={"is_onlive": 0}),
                    LiveStatus(is_live=False, raw={"is_onlive": 0}),
                ]
            )
            room = SimpleNamespace(name="test-room", room_id=123, url="https://example.test/room")

            await service._handle_live(
                room,
                LiveStatus(is_live=True, title="test live", raw={"is_onlive": 1}),
            )

            events = self._events(service)

        self.assertEqual(recorder.calls, [1, 2])
        self.assertIn("recording_segment_failed", [item["event"] for item in events])
        self.assertEqual([item["event"] for item in events].count("recorded"), 1)
        service._schedule_processing.assert_called_once()
        service._set_record_retry_cooldown.assert_not_called()

    async def test_max_seconds_keeps_short_test_mode_to_one_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            test_segment = root / "capture" / "test.ts"
            recorder = _FakeRecorder(
                [test_segment],
                {"test.ts": 10.0, "recording-merged.mkv": 10.0},
            )
            service = self._service(temp_dir, recorder, confirmations=2)
            service.config.record.max_seconds = 10
            service._get_live_status = AsyncMock()
            room = SimpleNamespace(name="test-room", room_id=123, url="https://example.test/room")

            await service._handle_live(
                room,
                LiveStatus(is_live=True, title="test live", raw={"is_onlive": 1}),
            )

        self.assertEqual(recorder.calls, [1])
        service._get_live_status.assert_not_awaited()
        service._schedule_processing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
