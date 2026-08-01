"""Gateway JSON Lines session recording and deterministic replay helpers."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


RECORDING_FORMAT = "echoguard-jsonl-v1"


@dataclass(frozen=True, slots=True)
class RecordedLine:
    """One received Gateway line with its local receive timestamp."""

    recorded_at: float
    line: str


class GatewayRecorder:
    """Thread-safe JSONL writer that preserves valid and invalid Gateway lines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file = None
        self._path: Path | None = None

    @property
    def active(self) -> bool:
        return self._file is not None

    @property
    def path(self) -> Path | None:
        return self._path

    def start(self, path: Path) -> Path:
        self.stop()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w", encoding="utf-8", newline="\n")
        header = {
            "format": RECORDING_FORMAT,
            "kind": "session_start",
            "recorded_at": time.time(),
        }
        handle.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        with self._lock:
            self._file = handle
            self._path = path
        return path

    def append(self, line: str, recorded_at: float | None = None) -> None:
        raw = str(line).rstrip("\r\n")
        payload = {
            "format": RECORDING_FORMAT,
            "kind": "gateway_line",
            "recorded_at": float(recorded_at if recorded_at is not None else time.time()),
            "line": raw,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            if self._file is None:
                raise RuntimeError("录制尚未启动")
            self._file.write(encoded)
            self._file.flush()

    def append_many(self, lines: Iterable[str], recorded_at: float | None = None) -> None:
        base_time = float(recorded_at if recorded_at is not None else time.time())
        for offset, line in enumerate(lines):
            self.append(line, base_time + offset * 0.000001)

    def stop(self) -> Path | None:
        with self._lock:
            handle = self._file
            path = self._path
            self._file = None
            self._path = None
        if handle is not None:
            handle.close()
        return path


def load_recording(path: Path) -> tuple[list[RecordedLine], int]:
    """Load a recording and return valid Gateway records plus invalid row count."""

    records: list[RecordedLine] = []
    invalid = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(payload, dict):
                invalid += 1
                continue
            if payload.get("kind") == "session_start":
                continue
            if payload.get("format") != RECORDING_FORMAT or payload.get("kind") != "gateway_line":
                invalid += 1
                continue
            try:
                recorded_at = float(payload["recorded_at"])
                line = str(payload["line"])
            except (KeyError, TypeError, ValueError):
                invalid += 1
                continue
            records.append(RecordedLine(recorded_at=recorded_at, line=line))
    records.sort(key=lambda item: item.recorded_at)
    return records, invalid
