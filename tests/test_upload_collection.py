from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from showroomrecorder.upload import BilibiliCollectionUploader, BiliupUploader


class BilibiliCollectionUploaderTests(unittest.TestCase):
    def _manager(self) -> BilibiliCollectionUploader:
        manager = BilibiliCollectionUploader.__new__(BilibiliCollectionUploader)
        manager.collection_title = "showroom直播"
        manager.season_id = 4567087
        manager.section_id = 5101377
        manager.section_title = "正片"
        manager.page_wait_seconds = 0
        manager.page_poll_seconds = 1
        manager.request_timeout_seconds = 30
        manager.csrf = "csrf-token"
        manager.session = Mock()
        return manager

    def test_resolves_collection_by_exact_title_and_section(self) -> None:
        manager = self._manager()
        manager.season_id = None
        manager.section_id = None
        manager._request_json = Mock(
            return_value={
                "code": 0,
                "data": {
                    "seasons": [
                        {
                            "season": {"id": 4567087, "title": "showroom直播"},
                            "sections": {
                                "sections": [{"id": 5101377, "title": "正片"}]
                            },
                        }
                    ],
                    "page": {"total": 1},
                },
            }
        )

        section_id = manager._resolve_section_id()

        self.assertEqual(section_id, 5101377)
        self.assertEqual(manager.season_id, 4567087)

    def test_adds_video_with_aid_cid_and_title(self) -> None:
        manager = self._manager()
        manager._resolve_section_id = Mock(return_value=5101377)
        manager._get_video_info = Mock(
            return_value={
                "aid": 123456,
                "title": "SHOWROOM recording",
                "pages": [{"cid": 654321}],
            }
        )
        manager._section_contains_aid = Mock(return_value=False)
        manager._post_episode = Mock()

        result = manager.add("BV1test")

        manager._post_episode.assert_called_once_with(
            5101377,
            {
                "aid": 123456,
                "cid": 654321,
                "title": "SHOWROOM recording",
                "charging_pay": 0,
            },
        )
        self.assertFalse(result["already_present"])

    def test_existing_video_is_not_added_twice(self) -> None:
        manager = self._manager()
        manager._resolve_section_id = Mock(return_value=5101377)
        manager._get_video_info = Mock(
            return_value={
                "aid": 123456,
                "title": "SHOWROOM recording",
                "pages": [{"cid": 654321}],
            }
        )
        manager._section_contains_aid = Mock(return_value=True)
        manager._post_episode = Mock()

        result = manager.add("BV1test")

        manager._post_episode.assert_not_called()
        self.assertTrue(result["already_present"])


class BiliupCollectionIntegrationTests(unittest.TestCase):
    def test_collection_result_is_recorded_in_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                config_path=root / "config.yaml",
                upload=SimpleNamespace(
                    biliup={
                        "collection": {
                            "enabled": True,
                            "title": "showroom直播",
                            "season_id": 4567087,
                            "section_id": 5101377,
                        }
                    }
                ),
            )
            session = SimpleNamespace(metadata={})
            uploader = BiliupUploader(config)

            with patch("showroomrecorder.upload.BilibiliCollectionUploader") as manager:
                manager.return_value.add.return_value = {
                    "season_id": 4567087,
                    "section_id": 5101377,
                    "aid": 123456,
                    "already_present": False,
                }
                uploader._add_to_collection(session, "BV1test", "data/cookies.json")

            self.assertTrue(session.metadata["collection_attempted"])
            self.assertTrue(session.metadata["collection_added"])
            self.assertFalse(session.metadata["collection_already_present"])
            self.assertEqual(session.metadata["collection_section_id"], 5101377)

    def test_collection_failure_is_nonfatal_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                config_path=root / "config.yaml",
                upload=SimpleNamespace(
                    biliup={
                        "collection": {
                            "enabled": True,
                            "title": "showroom直播",
                        }
                    }
                ),
            )
            session = SimpleNamespace(metadata={})
            uploader = BiliupUploader(config)

            with patch("showroomrecorder.upload.BilibiliCollectionUploader") as manager:
                manager.return_value.add.side_effect = RuntimeError("temporary API failure")
                uploader._add_to_collection(session, "BV1test", "data/cookies.json")

            self.assertEqual(session.metadata["collection_error"], "temporary API failure")
            self.assertTrue(session.metadata["collection_attempted"])

    def test_future_append_also_checks_collection_without_scanning_history(self) -> None:
        config = SimpleNamespace(
            config_path=Path("config.yaml").resolve(),
            upload=SimpleNamespace(
                enabled=True,
                uploader="biliup",
                biliup={
                    "mode": "append",
                    "append_vid": "BV1existing",
                    "small_chunk_upload": True,
                    "collection": {"enabled": True},
                },
            ),
            naming=SimpleNamespace(part_title_template="{streamer}"),
        )
        session = SimpleNamespace(
            upload_file=Path("recording.mp4"),
            room=SimpleNamespace(name="test-room", url="https://example.test", room_id="1"),
            started_at=datetime(2026, 8, 8, 10, 0, 0),
            ended_at=datetime(2026, 8, 8, 11, 0, 0),
            live_title="test live",
            job_id="job-1",
            zh_srt_file=None,
            bvid=None,
            metadata={},
        )
        uploader = BiliupUploader(config)
        uploader._append_with_small_chunks = Mock(return_value="BV1existing")
        uploader._add_to_collection = Mock()

        result = uploader.upload(session, [])

        self.assertEqual(result, "BV1existing")
        uploader._add_to_collection.assert_called_once_with(
            session,
            "BV1existing",
            None,
        )


if __name__ == "__main__":
    unittest.main()
