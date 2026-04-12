import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import cv2


def format_timestamp(fmt: str) -> str:
    now = datetime.now(tz=timezone.utc)
    if fmt == "epoch_ms":
        return str(int(now.timestamp() * 1_000))
    # ISO 8601 with millisecond precision, e.g. 2026-04-06T14:32:01.123Z
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class CameraSource:
    def __init__(self, source: str, timestamp_format: str = "iso"):
        self.source = int(source) if source.isdigit() else source
        self.timestamp_format = timestamp_format
        self.cap = cv2.VideoCapture(self.source)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open source: {source}")

        self._frame: Optional[any] = None
        self._frame_lock = threading.Lock()
        self.running = True
        self.frame_index = 0
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.frame_duration = 1.0 / self.fps

    @property
    def frame(self) -> Optional[any]:
        with self._frame_lock:
            return self._frame

    @frame.setter
    def frame(self, value: Optional[any]) -> None:
        with self._frame_lock:
            self._frame = value

    def read(self) -> Optional[any]:
        ret, frame = self.cap.read()
        if not ret:
            return None

        timestamp = format_timestamp(self.timestamp_format)
        label = f"{timestamp} | frame {self.frame_index}"
        cv2.putText(
            frame,
            label,
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        print(f"[{timestamp}] [{self.source}] frame_index={self.frame_index}")
        self.frame_index += 1
        self.frame = frame  # goes through the lock-protected setter
        return frame

    def release(self):
        self.cap.release()