from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from showroomrecorder.upload import BiliupUploader, SubtitleDraftUploader


class UploadSubtitleTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.uploader = BiliupUploader(SimpleNamespace())
        self.session = SimpleNamespace(
            upload_file=Path("20260718_showroom_直播_5.mp4"),
        )

    def test_small_chunk_upload_uses_only_actual_uploaded_part_title(self) -> None:
        candidates = self.uploader._subtitle_part_title_candidates(
            self.session,
            "20260718 showroom 直播",
            use_upload_file_title_only=True,
        )

        self.assertEqual(candidates, ["20260718_showroom_直播_5"])

    def test_non_small_chunk_upload_keeps_legacy_title_candidates(self) -> None:
        candidates = self.uploader._subtitle_part_title_candidates(
            self.session,
            "20260718 showroom 直播",
        )

        self.assertEqual(
            candidates,
            [
                "20260718_showroom_直播_5",
                "20260718 showroom 直播",
                "20260718_showroom_直播",
            ],
        )

    def test_subtitle_save_retries_transient_request_error(self) -> None:
        uploader = SubtitleDraftUploader.__new__(SubtitleDraftUploader)
        uploader.session = Mock()
        uploader.save_attempts = 3
        uploader.save_retry_seconds = 5
        response = Mock()
        response.json.return_value = {"code": 0}
        uploader.session.post.side_effect = [
            requests.ReadTimeout("temporary timeout"),
            response,
        ]

        with patch("showroomrecorder.upload.time.sleep") as sleep:
            uploader._save_draft({"bvid": "BV1test"})

        self.assertEqual(uploader.session.post.call_count, 2)
        sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
