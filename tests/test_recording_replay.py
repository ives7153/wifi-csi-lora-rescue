from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from upper_computer.recording import GatewayRecorder, load_recording


class RecordingReplayTests(unittest.TestCase):
    def test_valid_and_invalid_gateway_lines_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            recorder = GatewayRecorder()
            recorder.start(path)
            recorder.append('{"id":1,"presence":80}', recorded_at=10.0)
            recorder.append("boot log", recorded_at=11.0)
            recorder.stop()

            records, invalid = load_recording(path)

        self.assertEqual(invalid, 0)
        self.assertEqual([item.line for item in records], ['{"id":1,"presence":80}', "boot log"])
        self.assertEqual([item.recorded_at for item in records], [10.0, 11.0])

    def test_corrupt_record_rows_are_counted_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.jsonl"
            path.write_text(
                '{"format":"echoguard-jsonl-v1","kind":"gateway_line","recorded_at":1,"line":"{}"}\n'
                "not json\n",
                encoding="utf-8",
            )
            records, invalid = load_recording(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(invalid, 1)


if __name__ == "__main__":
    unittest.main()
