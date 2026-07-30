from __future__ import annotations

import unittest
from types import SimpleNamespace

from showroomrecorder.danmaku import DanmakuEntry, DanmakuRecorder


class DanmakuTimelineTests(unittest.TestCase):
    def test_comments_are_mapped_to_merged_media_and_gap_comments_are_skipped(self) -> None:
        recorder = object.__new__(DanmakuRecorder)
        session = SimpleNamespace(
            metadata={
                "recording_timeline": [
                    {
                        "wall_start": 0.0,
                        "wall_end": 10.0,
                        "media_start": 0.0,
                        "media_end": 8.0,
                    },
                    {
                        "wall_start": 15.0,
                        "wall_end": 25.0,
                        "media_start": 8.0,
                        "media_end": 18.0,
                    },
                ]
            }
        )
        entries = [
            DanmakuEntry(index=1, offset=5.0, timestamp=5.0, text="first"),
            DanmakuEntry(index=2, offset=12.0, timestamp=12.0, text="gap"),
            DanmakuEntry(index=3, offset=20.0, timestamp=20.0, text="second"),
        ]

        mapped = recorder._entries_for_recording_timeline(session, entries)

        self.assertEqual([entry.index for entry in mapped], [1, 3])
        self.assertEqual([entry.offset for entry in mapped], [4.0, 13.0])
        self.assertEqual(entries[0].offset, 5.0)


if __name__ == "__main__":
    unittest.main()
