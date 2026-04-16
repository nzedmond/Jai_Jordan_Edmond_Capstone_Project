# Synchronization Algorithm

## Overview

The system has two sides: a **sender** (`transport.py` + `single_cam.py`) and a **receiver** (`get_frame.py` + `sync.py`). Frames flow over TCP with embedded timestamps, and the receiver's jitter buffer uses those timestamps to align frames across streams before displaying them (this would be the clocks difference in different cameras if we were solving the clocks problem, but the alignment logic would much stay the same).

---

## Step 1 — Capture and timestamping (`single_cam.py`)

When `CameraSource.read()` is called:

1. It calls `cap.read()` to pull a raw frame from OpenCV.
2. It immediately records `datetime.now()` in milliseconds as `ts_ms` — this is the **capture timestamp**, the ground truth for when this image was taken.
3. It writes both `frame` and `ts_ms` together under a lock so callers always get an atomically consistent `(frame, ts_ms)` pair via `get_frame()`.

---

## Step 2 — Sending with a packet header (`transport.py`)

`send_frames()` runs on the main thread while capture threads run concurrently. For every camera, it calls `cam.get_frame()` and sends a packet structured as:

```
[cam_id: 1 byte][ts_ms: 8 bytes][jpeg_length: 4 bytes][JPEG payload: N bytes]
```

We noted two things here:

- **Deduplication**: To prevent sending the same packet (frame) twice, which can happen if the send loop is faster than the capture rate, `send_frames()` tracks `last_ts` per camera and skips a frame if `ts_ms` hasn't changed.
- **Artificial delay**: To test how well teh sync buffer handles jitter, we staged packets using `--base-delay-ms` and `--jitter-ms` to simulate a real network with variable latency.

---

## Step 3 — Receiving and decoding (`get_frame.py`)

The background thread `_receive_loop`:

1. Reads exactly 13 bytes (the header) using `recv_exact`, which loops until all bytes arrive.
2. Unpacks `cam_id`, `ts_ms`, and `length` from the header.
3. Reads exactly `length` bytes for the JPEG payload.
4. Decodes the JPEG back into a BGR numpy array with `cv2.imdecode`. (Apparently, OpenCV uses the BGR instead of RGB color channel ordering)
5. Calls `sync_buf.push(cam_id, ts_ms, frame)`: handing off to the jitter buffer.

---

## Step 4 — The jitter buffer (`sync.py`)

The actual synchronization algorithm has two parts:

### `_StreamBuffer` — a min-heap per stream
(We used a min-heap here because frames aren't expected to arrive in the correct order. We need to keep the earliest-timestamp frame at the top so that `pop_up_to(cutoff)` always always processes frames in chronological order.)

Each camera stream gets its own min-heap, sorted by `ts_ms`. A monotonic sequence counter is used as a tie-breaker so numpy arrays are never compared directly (which would crash).

- `push(ts_ms, frame)` — inserts into the heap.
- `pop_up_to(cutoff_ts_ms)` — **drains all frames older than the cutoff** and returns the most recent one among them. Frames that are even older than "the most recent eligible one" are discarded.

### `try_consume()` — producing one aligned frame set

Called by the display loop at `target_fps`. Here is how it works:

```
cutoff = now_ms - buffer_delay_ms
```

 Any frame captured before this moment is "old enough to display." The `buffer_delay_ms` parameter is a deliberate look-behind window. (Intentionally playing back the past so that both streams have had time to accumulate frames before we pick from them).

For each stream:
- `try_consume()` calls `pop_up_to(cutoff)` to get the best frame at or before the cutoff.
- If a qualifying frame was found, it updates `buf.last` and uses it.
- If no qualifying frame exists (the stream is temporarily ahead, or network is slow, or one of the pre-recorded files is shorter than the other), it **freezes on `buf.last`** — the last good frame — rather than going blank.
- If a stream has **never** delivered a frame at all, it returns `None` (the buffer isn't ready yet).

After collecting one frame per stream:

```
sync_error_ms = max(ts_vals) - min(ts_vals)
latency = now_ms - ts_ms  (per stream)
```

- `sync_error_ms` measures how far apart in capture time the two frames w're about to display actually are. This is our alignment quality metric (lower is better).
- `latency` measures end-to-end delay: the time between when a frame was captured and when it's shown.

The result dict with `frames`, `sync_error_ms`, and `latencies` is returned to the display loop, which overlays the stats on screen and writes a row to CSV.

---

## The core tradeoff: `buffer_delay_ms`

| Setting | Effect |
|---|---|
| `0 ms` | Minimal latency, but if one stream lags by even a few ms, it gets frozen |
| `100 ms` (default) | Both streams accumulate ~100ms of frames; much more tolerant of jitter |
| `300 ms` | Very robust to network hiccups, but everything we see on the display is 300ms in the past |

**Key Observation*:* A larger buffer gives the slower stream more time to "catch up" before the display loop picks, so `sync_error_ms` tends to be lower, at the cost of higher latency.

---

## Thread model summary

```
[Camera threads]         [Main/display thread]       [Receive thread]
capture_loop()           run_sync_display()           _receive_loop()
  cam.read()               try_consume()                recv_exact()
  -> stores frame+ts          -> pop_up_to(cutoff)         -> push(cam_id, ts, frame)
    under _frame_lock          -> compute sync_error          -> under SyncBuffer._lock
```

`SyncBuffer._lock` protects the heaps between the receive thread (writer) and the display thread (reader). `CameraSource._frame_lock` protects the `(frame, ts_ms)` pair between the capture thread (writer) and the send loop (reader).
