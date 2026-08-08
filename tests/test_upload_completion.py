from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from showroomrecorder.baidu_netdisk import BaiduUploadResult
from showroomrecorder.models import LiveSession, SubtitleSegment
from showroomrecorder.runner import ShowroomRecorderService


class UploadCompletionTests(unittest.TestCase):
    def _service(self, root: Path) -> ShowroomRecorderService:
        service = object.__new__(ShowroomRecorderService)
        service.config = SimpleNamespace(
            asr=SimpleNamespace(enabled=True),
            upload=SimpleNamespace(
                enabled=True,
                cleanup_after_success=True,
                keep_latest_upload_per_room=True,
                biliup={"upload_subtitle_draft": True},
            ),
            baidu_netdisk=SimpleNamespace(enabled=True, required_for_cleanup=True),
            paths=SimpleNamespace(
                data_dir=root,
                subtitles_dir=root / "subtitles",
                logs_dir=root / "logs",
            ),
        )
        service.baidu_netdisk = SimpleNamespace(
            state_file_for=lambda session: root / "baidu-state" / f"{session.job_id}.json"
        )
        return service

    def _session(self, root: Path) -> LiveSession:
        room = SimpleNamespace(name="test-room", url="https://example.test", room_id=1)
        job_id = "20260808_120000_test-room"
        raw_dir = root / "raw" / "test-room" / job_id
        raw_dir.mkdir(parents=True)
        raw_file = raw_dir / "recording-merged.mkv"
        raw_segment = raw_dir / "segments" / "0001" / "recording-01.ts"
        raw_segment.parent.mkdir(parents=True)
        raw_file.write_bytes(b"merged")
        raw_segment.write_bytes(b"raw")

        processed = root / "processed" / "test-room" / "current.mp4"
        upload = root / "upload" / "test-room" / "current.mp4"
        subtitle = root / "subtitles" / "test-room" / "current.zh.srt"
        for path in (processed, upload, subtitle):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")
        upload.with_suffix(".danmaku.ass").write_text("ass", encoding="utf-8")
        work_dir = root / "work" / "test-room" / job_id
        work_dir.mkdir(parents=True)
        (work_dir / "biliup-upload.log").write_text("log", encoding="utf-8")

        return LiveSession(
            room=room,
            job_id=job_id,
            started_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc),
            live_title="test",
            work_dir=work_dir,
            raw_file=raw_file,
            raw_segments=[raw_segment],
            mp4_file=processed,
            zh_srt_file=subtitle,
            upload_file=upload,
            metadata={"subtitle_uploaded": True},
        )

    def test_all_required_uploads_must_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            session = self._session(root)
            segments = [SubtitleSegment(index=1, start=0, end=1, text="test")]

            pending = service._upload_completion_requirements(
                session,
                segments=segments,
                bvid="BV1test",
                baidu_result=None,
            )
            complete = service._upload_completion_requirements(
                session,
                segments=segments,
                bvid="BV1test",
                baidu_result=BaiduUploadResult(
                    fs_id=1,
                    path="/apps/test/file.mkv",
                    size=6,
                    md5="md5",
                ),
            )

            self.assertFalse(pending["baidu_recording"])
            self.assertTrue(all(complete.values()))

    def test_cleanup_removes_only_current_job_and_keeps_neighbor_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            session = self._session(root)
            neighbor = root / "upload" / "test-room" / "other-job.mp4"
            neighbor.write_bytes(b"keep")
            state_file = service.baidu_netdisk.state_file_for(session)
            state_file.parent.mkdir(parents=True)
            state_file.write_text("{}", encoding="utf-8")

            service._cleanup_after_success(session, "current")

            self.assertFalse(session.raw_file.exists())
            self.assertFalse(session.upload_file.exists())
            self.assertFalse(session.work_dir.exists())
            self.assertFalse(state_file.exists())
            self.assertTrue(neighbor.exists())


if __name__ == "__main__":
    unittest.main()
