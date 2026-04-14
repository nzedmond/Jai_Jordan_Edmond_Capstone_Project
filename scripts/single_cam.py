import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import cv2


def format_timestamp(fmt: str, now: Optional[datetime] = None) -> str:
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if fmt == "epoch_ms":
        return str(int(now.timestamp() * 1_000))
    # ISO 8601 with millisecond precision, e.g. 2026-04-06T14:32:01.123Z
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class CameraSource:
    '''Captures frames from a webcam or video file, annotates them with timestamps, and provides thread-safe access to the latest frame and its timestamp.
    Args:
    - source: Camera index or path/URL to a video source, given as a string.
    - timestamp_format: Format for timestamp labels (iso or epoch_ms)'''
    def __init__(self, source: str, timestamp_format: str = "iso"):
        self.source = int(source) if source.isdigit() else source
        self.timestamp_format = timestamp_format
        self.cap = cv2.VideoCapture(self.source)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open source: {source}")

        self._frame: Optional[any] = None
        self._frame_ts_ms: int = 0
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

    def get_frame(self) -> tuple:
        """Return (frame, ts_ms) atomically to avoid race conditions between frame updates and reads."""
        with self._frame_lock:
            return self._frame, self._frame_ts_ms

    def read(self) -> Optional[any]:
        '''Read the next frame from the video source, annotate it with a timestamp label, and store it along with its timestamp in milliseconds. Returns the annotated frame or None if reading fails.'''
        
        ret, frame = self.cap.read()
        if not ret:
            return None

        now = datetime.now(tz=timezone.utc)
        ts_ms = int(now.timestamp() * 1_000)
        label_ts = format_timestamp(self.timestamp_format, now)
        label = f"{label_ts} | frame {self.frame_index}"
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
        print(f"[{label_ts}] [{self.source}] frame_index={self.frame_index}")
        self.frame_index += 1
        with self._frame_lock:  # write frame + timestamp atomically
            self._frame = frame
            self._frame_ts_ms = ts_ms
        return frame

    def release(self):
        self.cap.release()