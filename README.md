# Distributed Multi-Camera Video Synchronization over TCP and UDP

**Jai Adams · Jordan Shapiro · Edmond Nzivugira**
COSC 465 — Capstone Project

## Overview

This project investigates the question: *"How can distributed video sources be aligned in time when network conditions cause frames to arrive at different times?"*

In broadcasting and videography, hardware solutions like genlock or post-production techniques are commonly used to synchronize multiple feeds. This project explores a software-based alternative: capturing video streams from multiple sources, timestamping each frame at capture time, transmitting over **TCP or UDP**, and implementing a synchronization algorithm at the receiver using a jitter buffer and NTP-style clock-offset correction.

The system is built in Python using OpenCV and standard library sockets. Experiments were run over a real ad-hoc WiFi network with 2 and 4 cameras, comparing TCP vs. UDP at three buffer depths (0, 150, 400 ms). Key finding: TCP collapses due to head-of-line blocking on WiFi; UDP sustains active synchronization with a best-case p50 sync error of **129 ms** (400 ms buffer). Full analysis is in `New_Report.md`.

---

## Experiments Completed

| Experiment | Setup | Key result |
|---|---|---|
| A — 2 cameras, TCP vs. UDP | 2 MacBooks over ad-hoc WiFi, 3 buffer depths (0/150/400 ms) | TCP stalled within ~1 s; UDP best p50 = 129 ms (400 ms buf) |
| B — 4 cameras, TCP vs. UDP | 2 Tapo IP cams + 2 MacBook webcams, same network | TCP sync error drifted to 30–60 s; UDP showed sawtooth recovery |

---

## Repository Structure

```
scripts/
    capture.py           # Single-camera preview with UTC timestamp overlay (standalone)
    capture_udp.py       # UDP-specific capture helper
    single_cam.py        # CameraSource class — thread-safe frame + timestamp access
    multicam_handler.py  # Multi-camera local display in a grid layout (no network)
    transport_tcp.py     # TCP sender: captures frames, encodes JPEG, sends with 13-byte header
    transport_udp.py     # UDP sender: same interface as transport_tcp.py
    get_frame_tcp.py     # TCP receiver: clock-sync handshake, jitter buffer, display
    get_frame_udp.py     # UDP receiver: jitter buffer, display (no clock-sync handshake)
    clock_sync.py        # NTP-style clock offset estimation (used by TCP pipeline)
    sync.py              # SyncBuffer — jitter-buffer synchronizer for multi-stream alignment
    analyze_sync.py      # Reads experiment CSVs and produces sync error / latency plots
figures/                 # Plots from 4-camera experiments
2camfigures/             # Plots from 2-camera experiments
logs/                    # Per-frame CSV logs from all experiments
2camlogs/                # Per-frame CSV logs from 2-camera distributed experiments
New_Report.md            # Full written report (system design, results, comparison, conclusions)
requirements.txt         # Python dependencies
Jordan_Jai_Edmond_Capstone Project Proposal.pdf
```

---

## Scripts

### `capture.py` — single-camera preview
Reads from one source and overlays a UTC timestamp on every frame. For verifying if a camera works before using the multi-camera pipeline.

```bash
python capture.py                        # webcam (device 0)
python capture.py --source 1             # webcam device 1
python capture.py --source video.mp4
python capture.py --timestamp-format epoch_ms
```

---

### `multicam_handler.py` — local multi-camera display
Opens multiple sources simultaneously (each in its own thread) and displays them in a grid window. No network involved: For local testing and verifying timestamp alignment before introducing transport.

```bash
python multicam_handler.py --sources 0 1
python multicam_handler.py --sources 0 ../videos/test_01.mp4 --columns 2
python multicam_handler.py --sources 0 1 --timestamp-format epoch_ms
```

| Flag | Default | Description |
|---|---|---|
| `--sources` | required | Device indices, file paths, or RTSP URLs |
| `--timestamp-format` | `iso` | `iso` (ISO 8601 ms) or `epoch_ms` (Unix ms) |
| `--columns` | `2` | Grid columns in the display window |

---

### `transport_tcp.py` + `get_frame_tcp.py` — TCP streaming pipeline

Each frame is sent as a 13-byte header followed by JPEG data:
```
[  1 byte   ] uint8   camera ID
[  8 bytes  ] uint64  capture timestamp in milliseconds (big-endian)
[  4 bytes  ] uint32  JPEG payload length in bytes (big-endian)
[  N bytes  ] JPEG frame data
```

Before any frames are sent, each sender and receiver exchange a clock-sync handshake (see `clock_sync.py`) to estimate and correct the clock offset between machines. Each camera gets its own dedicated TCP connection, so slow encoding or network congestion on one camera never delays another.

**Single-machine usage (two sources on one sender):**
```bash
# Terminal 1 — receiver
python scripts/get_frame_tcp.py --port 9000 --sync --buffer-delay-ms 100 --csv logs/run.csv

# Terminal 2 — sender with two sources
python scripts/transport_tcp.py --sources 0 1 --host 127.0.0.1 --port 9000
```

**Distributed usage (one camera per machine):**
```bash
# Receiver machine
python scripts/get_frame_tcp.py --port 9000 --sync --buffer-delay-ms 150 --csv logs/distributed_run.csv --stream-ids 0 1

# Machine A — camera 0  (connect first)
python scripts/transport_tcp.py --sources 0 --host <receiver_ip> --port 9000 --cam-id-start 0

# Machine B — camera 1  (connect second)
python scripts/transport_tcp.py --sources 0 --host <receiver_ip> --port 9000 --cam-id-start 1
```

`transport_tcp.py` flags (identical flags in `transport_udp.py` except JPEG default is 80):

| Flag | Default | Description |
|---|---|---|
| `--sources` | required | Camera sources (device index, file path, or RTSP URL) |
| `--host` | required | Receiver host IP |
| `--port` | required | Receiver port |
| `--cam-id-start` | `0` | First camera ID for this sender's sources. Set to `1` on the second machine so streams have distinct IDs. |
| `--timestamp-format` | `iso` | `iso` (ISO 8601 ms) or `epoch_ms` (Unix ms) |
| `--jpeg-quality` | `90` | JPEG encoding quality (1–100) |
| `--base-delay-ms` | `0` | Fixed artificial delay per packet in ms |
| `--jitter-ms` | `0` | Max extra delay (ms) added during a Markov-chain jitter burst |
| `--burst-prob` | `0.05` | Probability per packet of entering a jitter burst |
| `--burst-duration` | `10` | Expected packets per burst; exit probability = 1/burst-duration |

`get_frame_tcp.py` / `get_frame_udp.py` flags (identical interface; TCP version also runs the clock-sync handshake):

| Flag | Default | Description |
|---|---|---|
| `--port` | required | Port to listen on |
| `--host` | `0.0.0.0` | Bind address |
| `--sync` | off | Enable jitter-buffer synchronization |
| `--buffer-delay-ms` | `100` | Jitter buffer depth in ms — larger = lower sync error, higher latency |
| `--fps` | `30.0` | Target playback frame rate |
| `--csv` | none | Path to write per-frame sync metrics CSV (only used with `--sync`) |
| `--stream-ids` | `0 1` | Camera IDs to synchronize; also sets how many connections the receiver waits for |

Press `q` in the display window or `Ctrl+C` in either terminal to shut down cleanly.

---

### `transport_udp.py` + `get_frame_udp.py` — UDP streaming pipeline

Drop-in UDP alternative to the TCP pipeline. Packets are sent as individual datagrams; if a datagram is lost the receiver simply skips it and reads the next one — no backlog forms. The UDP receiver does **not** run a clock-sync handshake (assumes NTP-disciplined clocks), so the latency metric in CSV logs may reflect inter-machine clock skew.

```bash
# Receiver machine
python scripts/get_frame_udp.py --port 9000 --sync \
  --buffer-delay-ms 150 --csv 2camlogs/udp_buff150.csv --stream-ids 0 1

# Machine A — camera 0
python scripts/transport_udp.py --sources 0 \
  --host <receiver_ip> --port 9000 --cam-id-start 0

# Machine B — camera 1
python scripts/transport_udp.py --sources 0 \
  --host <receiver_ip> --port 9000 --cam-id-start 1
```

Flag interface is identical to `transport_tcp.py` / `get_frame_tcp.py` (see tables above), with the sole difference that `transport_udp.py` defaults `--jpeg-quality` to `80` instead of `90`.

---

### `sync.py` — jitter-buffer synchronizer

`SyncBuffer` aligns frames from N camera streams by capture timestamp. Each stream gets its own min-heap; `try_consume()` is called by the display loop at `target_fps` and picks, per stream, the most recent frame captured at or before `now - buffer_delay_ms`. If a stream has no qualifying frame it freezes on the last good frame. Sync error and per-stream latency are recorded each frame.

```python
from sync import SyncBuffer

buf = SyncBuffer(stream_ids=[0, 1], buffer_delay_ms=100, csv_path="logs/run.csv")
buf.push(cam_id=0, ts_ms=capture_ts, frame=bgr_frame)  # from receive thread
result = buf.try_consume()   # from display thread — returns frames + metrics
buf.close()                  # flushes CSV
```

---

### `analyze_sync.py` — experiment analysis

Reads one or more CSV logs produced by `get_frame.py --sync --csv` and generates three plots saved to `scripts/figures/`:

1. Sync error over time (frame index)
2. CDF of absolute sync error (with p50/p95 markers)
3. Latency histogram for both streams (with median annotations)

```bash
python scripts/analyze_sync.py logs/run.csv
python scripts/analyze_sync.py logs/no_buf.csv logs/buf100.csv logs/buf300.csv
python scripts/analyze_sync.py logs/run.csv --no-show --out figures/
```

| Flag | Default | Description |
|---|---|---|
| `csvs` | required | One or more CSV log files |
| `--out` | `figures/` | Output directory for PNG plots |
| `--no-show` | off | Save plots without displaying them interactively |

---

## Dependencies

```bash
pip install -r requirements.txt
```

Or individually:

```bash
pip install opencv-python numpy matplotlib pandas
```

Python 3.10+ recommended. All other dependencies (`socket`, `struct`, `threading`, `concurrent.futures`) are from the standard library.


## RUNNING WITH DISTRIBUTED CAMERAS (multi-machine)

This is the primary scenario the clock-sync handshake is designed for. Each camera source runs `transport.py` on its own machine. The receiver waits for all expected connections before starting.

**Start the receiver first:**
```bash
# TCP
python scripts/get_frame_tcp.py --port 9000 --sync --buffer-delay-ms 150 --csv logs/distributed_run.csv --stream-ids 0 1

# UDP
python scripts/get_frame_udp.py --port 9000 --sync --buffer-delay-ms 150 --csv logs/distributed_run_udp.csv --stream-ids 0 1
```

**Then start each sender (one per machine, in order):**
```bash
# Machine A — cam 0 (TCP)
python scripts/transport_tcp.py --sources 0 --host <receiver_ip> --port 9000 --cam-id-start 0

# Machine B — cam 1 (TCP)
python scripts/transport_tcp.py --sources 0 --host <receiver_ip> --port 9000 --cam-id-start 1

# (replace transport_tcp.py with transport_udp.py for UDP runs)
```

The receiver will print the estimated clock offset for each incoming connection before frame streaming begins, e.g.:
```
[INFO] Connection 1/2 from ('192.168.1.5', 52341)  clock_offset=+23ms
[INFO] Connection 2/2 from ('192.168.1.8', 49012)  clock_offset=-11ms
```

All incoming timestamps are corrected by their respective offsets before entering the jitter buffer, so `sync_error_ms` in the CSV reflects true capture-time differences rather than inter-machine clock skew.

---

## RUNNING WITH IP CAM

For this experiment we used a Tapo C211 WiFi camera alongside the built-in MacBook webcam, both fed into a single `transport.py` instance on the Mac. Because Colgate WiFi does not support direct device-to-device communication, we set up a local ad-hoc network using the MacBook as a router and assigned the camera a static IP in the Tapo app.

RTSP URL format: `rtsp://USERNAME:PASSWORD@IP_ADDRESS:PORT/stream1`

To verify the camera connection before running the full pipeline:
```bash
python scripts/capture.py --source rtsp://USERNAME:PASSWORD@192.168.2.4:554/stream1
```

To run the full sync experiment (webcam as cam 0, IP cam as cam 1, both on the same Mac):
```bash
# Terminal 1 — receiver (TCP)
python scripts/get_frame_tcp.py --port 9000 --sync --buffer-delay-ms 150 --csv logs/ipcam_run.csv --stream-ids 0 1

# Terminal 2 — sender (both sources on the same machine)
python scripts/transport_tcp.py --sources 0 rtsp://USERNAME:PASSWORD@192.168.2.4:554/stream1 --host 127.0.0.1 --port 9000
```


