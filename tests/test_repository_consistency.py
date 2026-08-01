from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from upper_computer.version import DISPLAY_VERSION, MOBILE_DIST_NAME, STANDARD_DIST_NAME


ROOT = Path(__file__).resolve().parents[1]


class RepositoryConsistencyTests(unittest.TestCase):
    def _tracked_files(self) -> list[Path]:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        return [ROOT / item.decode("utf-8") for item in output.split(b"\0") if item]

    def test_release_names_share_one_version(self) -> None:
        self.assertEqual(DISPLAY_VERSION, "v0.4.0")
        self.assertEqual(STANDARD_DIST_NAME, "EchoGuard-v0.4.0-windows")
        self.assertEqual(MOBILE_DIST_NAME, "EchoGuard-Mobile-v0.4.0-windows")

    def test_tracked_text_no_longer_mentions_sht20(self) -> None:
        offenders: list[str] = []
        for path in self._tracked_files():
            if path.suffix.lower() not in {".md", ".py", ".c", ".h", ".txt", ".yml", ".yaml"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if ("SHT" + "20") in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_local_artifacts_are_not_tracked(self) -> None:
        forbidden: list[str] = []
        for path in self._tracked_files():
            relative = path.relative_to(ROOT).as_posix()
            parts = relative.split("/")
            if any(part in {"build", "dist", "releases", "models", "runtime", "exports"} for part in parts):
                forbidden.append(relative)
            if path.name == "sdkconfig":
                forbidden.append(relative)
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
