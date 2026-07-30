from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from showroomrecorder.runner import _link_or_copy


class UploadStagingTests(unittest.TestCase):
    def test_uses_hard_link_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "processed.mp4"
            destination = root / "upload.mp4"
            source.write_bytes(b"video")

            _link_or_copy(source, destination)

            self.assertTrue(os.path.samefile(source, destination))

    def test_falls_back_to_copy_when_hard_link_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "processed.mp4"
            destination = root / "upload.mp4"
            source.write_bytes(b"video")

            with patch("showroomrecorder.runner.os.link", side_effect=OSError("unsupported")):
                _link_or_copy(source, destination)

            self.assertEqual(destination.read_bytes(), b"video")
            self.assertFalse(os.path.samefile(source, destination))


if __name__ == "__main__":
    unittest.main()
