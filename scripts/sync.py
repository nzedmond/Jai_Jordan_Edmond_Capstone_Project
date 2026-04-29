"""Jitter-buffer synchronizer for multi-stream video frames.

Typical usage from get_frame.py (or any receiver):

    from sync import SyncBuffer

    buf = SyncBuffer(stream_ids=[0, 1], buffer_delay_ms=100, csv_path="logs/run.csv")

    # From the receive thread:
    buf.push(cam_id=0, ts_ms=capture_ts, frame=bgr_frame)

    # From the display thread (called at target_fps):
    result = buf.try_consume()
    if result:
        frames   = result["frames"]          # {cam_id: np.ndarray}
        sync_err = result["sync_error_ms"]   # int
        latency  = result["latencies"]       # {cam_id: int}

    buf.close()  # flushes CSV
"""

import csv
import heapq
import itertools
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Internal per-stream buffer
# ---------------------------------------------------------------------------

_EMA_ALPHA = 0.1          # smoothing factor for frame-interval Exponential Moving Average (EMA)
_MIN_INTERVAL_MS = 5.0    # clamp: faster than 200 fps is noise
_MAX_INTERVAL_MS = 500.0  # clamp: slower than 2 fps is a stall, not the capture rate


class _StreamBuffer:
    """Min-heap of (ts_ms, seq, frame) for one camera stream.

    A monotonic sequence counter is used as a tie-breaker so that
    numpy arrays are never compared directly.

    Also tracks an EMA estimate of the stream's native frame interval so
    that SyncBuffer.try_consume() can rate-limit faster streams to the
    pace of the slowest stream.
    """

    def __init__(self) -> None:
        self._heap: list = []
        self._counter = itertools.count()
        self.last: Optional[Tuple[int, np.ndarray]] = None  # (ts_ms, frame)
        self._last_push_ts: Optional[int] = None
        self._est_interval_ms: Optional[float] = None

    def push(self, ts_ms: int, frame: np.ndarray) -> None:
        if self._last_push_ts is not None:
            interval = ts_ms - self._last_push_ts
            if _MIN_INTERVAL_MS <= interval <= _MAX_INTERVAL_MS:
                if self._est_interval_ms is None:
                    self._est_interval_ms = float(interval)
                else:
                    self._est_interval_ms = (
                        _EMA_ALPHA * interval
                        + (1.0 - _EMA_ALPHA) * self._est_interval_ms
                    )
        self._last_push_ts = ts_ms
        heapq.heappush(self._heap, (ts_ms, next(self._counter), frame))

    @property
    def est_interval_ms(self) -> Optional[float]:
        """EMA estimate of this stream's inter-frame interval (ms), or None if unknown."""
        return self._est_interval_ms

    def peek_ts(self) -> Optional[int]:
        return self._heap[0][0] if self._heap else None

    def pop_up_to(self, cutoff_ts_ms: int) -> Optional[Tuple[int, np.ndarray]]:
        """Return the latest frame with ts_ms <= cutoff, discarding older ones.

        Drains all frames up to the cutoff and returns the last one found
        (highest ts_ms <= cutoff).  Returns None if the heap has nothing at
        or before the cutoff.
        """
        result: Optional[Tuple[int, np.ndarray]] = None
        while self._heap and self._heap[0][0] <= cutoff_ts_ms:
            ts_ms, _seq, frame = heapq.heappop(self._heap)
            result = (ts_ms, frame)
        return result

    def __len__(self) -> int:
        return len(self._heap)


class SyncBuffer:
    """Jitter buffer that aligns frames from N camera streams by timestamp.

    For each call to ``try_consume`` the buffer selects, per stream, the
    most recent frame whose capture timestamp is at or before
    ``now_ms - buffer_delay_ms``.  If a stream has no qualifying frame
    it freezes on the last good frame.  Until every stream has produced
    at least one frame ``try_consume`` returns ``None``.

    Thread-safe: ``push`` may be called from a receive thread while
    ``try_consume`` is called from the display/main thread.

    Args:
        stream_ids:      Ordered list of camera IDs expected (e.g. ``[0, 1]``).
        buffer_delay_ms: Jitter budget — how far behind real-time to play.
                         Larger values tolerate more network jitter at the cost
                         of end-to-end latency.
        target_fps:      Intended playback rate (used only for documentation;
                         the caller is responsible for pacing ``try_consume``
                         calls at this rate).
        csv_path:        If given, per-frame metrics are appended to this file.
                         Parent directories are created if they do not exist.
    """

    def __init__(
        self,
        stream_ids: List[int],
        buffer_delay_ms: int = 100,
        target_fps: float = 30.0,
        csv_path: Optional[str] = None,
    ) -> None:
        if len(stream_ids) < 2:
            raise ValueError("stream_ids must contain at least two camera IDs")

        self.stream_ids = stream_ids
        self.buffer_delay_ms = buffer_delay_ms
        self.target_fps = target_fps

        self._bufs: Dict[int, _StreamBuffer] = {
            sid: _StreamBuffer() for sid in stream_ids
        }
        self._lock = threading.Lock()
        self._frame_index = 0

        # Build column names dynamically so any number of cameras are covered.
        self.csv_columns: List[str] = (
            ["frame_index"]
            + [f"cam_{sid}_ts_ms" for sid in stream_ids]
            + ["playback_time_ms"]
            + [f"latency_{sid}_ms" for sid in stream_ids]
            + ["sync_error_ms"]
        )

        # CSV setup
        self._csv_file = None
        self._csv_writer = None
        if csv_path:
            Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
            self._csv_file = open(csv_path, "w", newline="")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(self.csv_columns)


    def push(self, cam_id: int, ts_ms: int, frame: np.ndarray) -> None:
        """Enqueue a received frame into the appropriate stream buffer."""
        if cam_id not in self._bufs:
            return
        with self._lock:
            self._bufs[cam_id].push(ts_ms, frame)

    def try_consume(self) -> Optional[dict]:
        """Attempt to produce one aligned frame set.

        Returns ``None`` when the buffer is not ready (a stream has never
        delivered a frame yet, or all streams' earliest frame is still
        within the jitter window).

        On success returns a dict with keys:

        * ``"frames"``          – ``{cam_id: np.ndarray}``
        * ``"timestamps"``      – ``{cam_id: int}``  capture timestamps (ms)
        * ``"playback_time_ms"``– ``int``  wall-clock time of this call (ms)
        * ``"latencies"``       – ``{cam_id: int}``  playback - capture (ms)
        * ``"sync_error_ms"``   – ``int``  max_ts − min_ts across streams
        * ``"frame_index"``     – ``int``
        """
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - self.buffer_delay_ms

        frames: Dict[int, np.ndarray] = {}
        timestamps: Dict[int, int] = {}

        with self._lock:
            # Compute the slowest stream's interval so faster streams are held back.
            # Only activate rate-limiting once every stream has a valid estimate.
            intervals = [b.est_interval_ms for b in self._bufs.values()]
            max_interval_ms: Optional[float] = (
                max(intervals) if all(iv is not None for iv in intervals) else None
            )

            for sid in self.stream_ids:
                buf = self._bufs[sid]

                # Rate-limit: advance this stream only when enough time has
                # elapsed since its last displayed frame to match the slowest
                # stream's pace.  The 0.8 factor gives a small timing tolerance
                # so a frame is not systematically skipped by display-loop jitter.
                advance = True
                if max_interval_ms is not None and buf.last is not None:
                    elapsed = cutoff - buf.last[0]
                    if elapsed < max_interval_ms * 0.8:
                        advance = False

                if advance:
                    entry = buf.pop_up_to(cutoff)
                    if entry is not None:
                        ts_ms, frame = entry
                        buf.last = (ts_ms, frame)
                        frames[sid] = frame
                        timestamps[sid] = ts_ms
                    elif buf.last is not None:
                        # Freeze on last good frame
                        ts_ms, frame = buf.last
                        frames[sid] = frame
                        timestamps[sid] = ts_ms
                    else:
                        return None  # Stream has never had a frame — not ready yet
                else:
                    # Not time to advance yet — reuse the last displayed frame
                    ts_ms, frame = buf.last  # type: ignore[misc]
                    frames[sid] = frame
                    timestamps[sid] = ts_ms

        ts_vals = list(timestamps.values())
        sync_error_ms = max(ts_vals) - min(ts_vals)
        latencies = {sid: now_ms - timestamps[sid] for sid in self.stream_ids}

        if self._csv_writer:
            row = (
                [self._frame_index]
                + [timestamps.get(sid, "") for sid in self.stream_ids]
                + [now_ms]
                + [latencies.get(sid, "") for sid in self.stream_ids]
                + [sync_error_ms]
            )
            self._csv_writer.writerow(row)
            self._csv_file.flush()

        self._frame_index += 1
        return {
            "frames": frames,
            "timestamps": timestamps,
            "playback_time_ms": now_ms,
            "latencies": latencies,
            "sync_error_ms": sync_error_ms,
            "frame_index": self._frame_index - 1,
        }

    def close(self) -> None:
        """Flush and close the CSV log (if open)."""
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
