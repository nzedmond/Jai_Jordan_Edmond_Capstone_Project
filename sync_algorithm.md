# Synchronization Algorithm

## Overview

The system has two sides: a **sender** (`transport_tcp.py` or `transport_udp.py`, plus `single_cam.py`) and a **receiver** (`get_frame_tcp.py` or `get_frame_udp.py`, plus `sync.py`). Before any frames flow in the TCP pipeline, the receiver and each sender perform a clock-sync handshake (`clock_sync.py`) to estimate and correct the clock offset between machines. The UDP receiver skips this handshake and relies on NTP-disciplined clocks. Frames then flow with embedded capture timestamps, and the receiver's jitter buffer uses those corrected timestamps to align frames across streams before displaying them.

---

## Step 1: Clock-sync handshake (`clock_sync.py`)

This step runs once per camera connection, immediately after the TCP connection is established and before frame streaming begins. It solves the core distributed problem: cameras on different machines have independent clocks, so their raw `ts_ms` values are not directly comparable.

The protocol is NTP-style. The receiver sends N ping packets and the sender replies to each:

```
Receiver                          Sender
   |                                 |
   |  --- ping: [T1_ms] -----------> |
   |                          T2 = sender.now()
   |                          T3 = sender.now()
   |  <-- pong: [T1, T2, T3] ------- |
T4 = receiver.now()
```

From each round:

```
offset = ((T2 - T1) + (T3 - T4)) / 2
```

`T2 - T1` estimates how far ahead the sender's clock appears when the ping arrives. `T3 - T4` estimates the same from the pong direction. Averaging the two cancels out the one-way network delay under the assumption of symmetric paths. A positive `offset` means the sender's clock is ahead of the receiver's clock.

Eight rounds are run and the **median** offset is returned, making the estimate robust to a single unlucky RTT spike. The estimated offset is stored per connection and passed into the receive thread for that camera.

---

## Step 2: Capture and timestamping (`single_cam.py`)

When `CameraSource.read()` is called in the capture thread:

1. It calls `cap.read()` to pull a raw frame from OpenCV. This call blocks until the sensor delivers a frame.
2. It immediately records `datetime.now(tz=timezone.utc)` in milliseconds as `ts_ms` — the timestamp is taken *after* `cap.read()` returns, which is the closest software approximation to the true capture moment.
3. It writes both `frame` and `ts_ms` together under a lock so any reader always gets an atomically consistent `(frame, ts_ms)` pair via `get_frame()`.

---

## Step 3: Sending with a packet header (`transport_tcp.py` / `transport_udp.py`)

Each camera runs `send_camera_frames()` in its own dedicated thread, connected to the receiver over its own dedicated TCP socket (or UDP datagram socket). Cameras never share a socket, so slow encoding or network congestion on one camera cannot delay another.

For every new frame, the send thread calls `cam.get_frame()` and transmits:

```
[cam_id: 1 byte][ts_ms: 8 bytes][jpeg_length: 4 bytes][JPEG payload: N bytes]
```

Two details worth noting:

- **Deduplication**: The send thread tracks `last_ts` per camera and skips a frame if `ts_ms` hasn't changed. This prevents sending the same frame twice when the send loop runs faster than the camera's capture rate.
- **Artificial delay**: `--base-delay-ms` and `--jitter-ms` inject a controlled per-packet delay to simulate real network conditions during experiments.

---

## Step 4: Receiving, decoding, and clock correction (`get_frame_tcp.py` / `get_frame_udp.py`)

The TCP receiver accepts one connection per expected camera stream and spawns one `_receive_loop` background thread per connection. Each thread knows the clock offset estimated for its connection during Step 1. The UDP receiver binds a single datagram socket and dispatches packets by the `cam_id` in each header; no per-connection offset is applied (clocks assumed NTP-synced).

For each incoming packet the thread:

1. Reads exactly 13 bytes (the header) using `recv_exact`, which loops until all bytes arrive — necessary because TCP is a byte stream and a single `recv()` call may return fewer bytes than requested.
2. Unpacks `cam_id`, `ts_ms`, and `length` from the header.
3. Reads exactly `length` bytes for the JPEG payload.
4. Decodes the JPEG back into a BGR numpy array with `cv2.imdecode`.
5. **Corrects the timestamp**: `corrected_ts = ts_ms - offset_ms`. This converts the sender's clock reading into the receiver's time domain. If the sender's clock is 23ms ahead, every timestamp from that camera is shifted back by 23ms before entering the jitter buffer.
6. Calls `sync_buf.push(cam_id, corrected_ts, frame)`.

After correction, timestamps from all cameras are expressed on a common clock regardless of which machine they came from, so the jitter buffer's alignment logic is valid across a distributed deployment.

---

## Step 5: The jitter buffer (`sync.py`)

The actual synchronization algorithm has two parts.

### `_StreamBuffer`: a min-heap per stream

Each camera stream gets its own min-heap sorted by `ts_ms`. A min-heap is used because frames are not guaranteed to arrive in timestamp order (network reordering, variable encoding time). The heap keeps the earliest-timestamp frame at the top so `pop_up_to(cutoff)` always processes frames in chronological order. A monotonic sequence counter breaks ties so numpy arrays are never compared directly (which would crash).

- `push(ts_ms, frame)` — inserts into the heap. On every push it also updates an **EMA estimate of the stream's inter-frame interval** (`_est_interval_ms`), clamped to the range [5 ms, 500 ms] to exclude encoding noise and genuine stalls. The smoothing factor is α = 0.1.
- `pop_up_to(cutoff_ts_ms)` — drains all frames at or before the cutoff and returns the most recent one among them. Frames older than that most-recent eligible frame are discarded.

### `try_consume()`: producing one aligned frame set

Called by the display loop at `target_fps`. A cutoff is computed each call:

```
cutoff = now_ms - buffer_delay_ms
```

Any frame captured before this moment is "old enough to display." The `buffer_delay_ms` is a deliberate look-behind window — intentionally playing the past so that both streams have had time to accumulate frames before the display loop picks from them.

**Rate-limiting step (heterogeneous frame rates):** Before selecting a frame, `try_consume` checks whether enough time has elapsed since this stream's last displayed frame relative to the slowest stream's estimated interval. Specifically, once every stream has a valid EMA estimate, the maximum interval across all streams (`max_interval_ms`) is computed each call. A stream is only advanced if:

```
(cutoff - last_displayed_ts) >= max_interval_ms * 0.8
```

If this condition is not met, the stream reuses its last displayed frame without calling `pop_up_to`. The 0.8 factor provides a small timing tolerance against display-loop jitter. This mechanism naturally synchronizes a 30 fps stream with a 15 fps stream without discarding valid frames prematurely.

For each stream (after the rate-limit check):
- `pop_up_to(cutoff)` returns the best frame at or before the cutoff.
- If a qualifying frame was found, `buf.last` is updated and that frame is used.
- If no qualifying frame exists (stream temporarily ahead, or network is slow), it **freezes on `buf.last`** — the last good frame — rather than going blank.
- If a stream has **never** delivered a frame, `None` is returned (buffer not ready yet).

After collecting one frame per stream:

```
sync_error_ms = max(ts_vals) - min(ts_vals)
latency        = now_ms - ts_ms   (per stream)
```

- `sync_error_ms` measures how far apart in capture time the two frames being displayed actually are. Because all `ts_ms` values have been clock-corrected in Step 4, this metric now reflects only true capture-time differences, not inter-machine clock skew.
- `latency` measures end-to-end delay: from the moment a frame was captured to the moment it is shown.

The result dict returned to the display loop contains:

| Key | Type | Description |
|---|---|---|
| `frames` | `{cam_id: np.ndarray}` | One BGR frame per stream |
| `timestamps` | `{cam_id: int}` | Corrected capture timestamps (ms) |
| `playback_time_ms` | `int` | Wall-clock time of this `try_consume` call |
| `latencies` | `{cam_id: int}` | `playback_time_ms − ts_ms` per stream |
| `sync_error_ms` | `int` | `max(ts_vals) − min(ts_vals)` across streams |
| `frame_index` | `int` | Monotonic counter; matches CSV row number |

---

## The core tradeoff: `buffer_delay_ms`

| Setting | Effect |
|---|---|
| `0 ms` | Minimal latency, but any stream that lags by even a few ms is frozen on its last frame |
| `100 ms` (default) | Both streams accumulate ~100ms of frames; much more tolerant of jitter |
| `300 ms` | Very robust to network hiccups, but everything displayed is 300ms in the past |

A larger buffer gives the slower stream more time to "catch up" before the display loop picks, so `sync_error_ms` tends to be lower — at the cost of higher latency. The buffer's accuracy benefit is only observable when jitter exceeds the buffer depth. With ±30ms jitter and a 100ms buffer, both settings produce similar `sync_error_ms` because the buffer is already deeper than the worst-case jitter.

---

## Thread model summary

```
Sender (per camera, per machine)            Receiver

[Capture thread]   [Send thread]            [Receive thread]       [Display thread]
capture_loop()     send_camera_frames()     _receive_loop()         run_sync_display()
  cam.read()         cam.get_frame()          recv_exact() (TCP)       try_consume()
  -> frame+ts          -> encode JPEG          sock.recvfrom() (UDP)      -> rate-limit check
     under               -> sock.sendall()       -> correct ts (TCP)        -> pop_up_to(cutoff)
     _frame_lock           (TCP/UDP)             -> sync_buf.push()         -> sync_error_ms
                                                    under SyncBuffer._lock
```

Before the send/receive threads start, the TCP handshake runs synchronously (UDP skips this):
```
[clock_sync.serve_clock_sync()]   <--->   [clock_sync.measure_offset()]
       (sender, after connect)                 (receiver, after accept)
```

**Lock ownership:**
- `CameraSource._frame_lock` — protects the `(frame, ts_ms)` pair between the capture thread (writer) and the send thread (reader).
- `SyncBuffer._lock` — protects the per-stream heaps between receive threads (writers) and the display thread (reader).
