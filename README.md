# Distributed Video Synchronization over TCP

**Jai Adams · Jordan Shapiro · Edmond Nzivugira**
COSC 465 — Capstone Project

## Overview

This project investigates the question: *"How can distributed video sources be aligned in time when network conditions cause frames to arrive at different times?"*

In broadcasting and videography, hardware solutions like genlock or post-production techniques are commonly used to synchronize multiple feeds. This project explores a software-based alternative: capturing video streams from multiple sources, timestamping each frame at capture time, transmitting over TCP, and implementing a synchronization algorithm at the receiver using buffering and timestamp alignment.

The system is built in Python using OpenCV and standard library sockets. The end goal is a quantitative analysis of the tradeoff between **synchronization accuracy** and **latency** under simulated network conditions (delay, jitter).

---

## Project Plan

| Milestone | Goal |
|---|---|
| Status Report 1 | Capture, transmit, and display multiple streams; baseline misalignment measurements |
| Status Report 2 | Synchronization algorithm; experiments with simulated network delay; preliminary results |
| Final | Full sync system; quantitative latency vs. accuracy analysis; written report |

---

## Repository Structure

```
scripts/
    capture.py           # Single-camera capture with UTC timestamps (standalone)
    single_cam.py        # CameraSource class — thread-safe frame + timestamp access
    multicam_handler.py  # Multi-camera local display in a grid layout
    transport.py         # TCP sender: captures frames, encodes JPEG, sends with header
    get_frame.py         # TCP receiver: parses header, decodes JPEG, displays frames
    sync.py              # SyncBuffer — jitter-buffer synchronizer for multi-stream alignment
    analyze_sync.py      # Reads experiment CSVs and produces sync error / latency plots
    figures/
        sync_error_over_time.png  # Plot: sync error vs. frame index
        sync_error_cdf.png        # Plot: CDF of absolute sync error
        latency_histogram.png     # Plot: per-stream latency distributions
videos/
    test_01.mp4          # Sample video for testing without a live camera
    test_02.mp4          # Second sample video (simulates a second camera stream)
logs/
    run.csv              # Per-frame sync metrics logged by get_frame.py --sync --csv
notes/
    syncAlgo.md          # Walkthrough of the synchronization algorithm design
tests/
    one_line_recv.txt    # Test fixture for the TCP receive path
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

### `transport.py` + `get_frame.py` — TCP streaming pipeline

Each frame is sent as:
```
[  8 bytes  ] uint64  capture timestamp (ms, big-endian)
[  4 bytes  ] uint32  JPEG payload length (big-endian)
[  N bytes  ] JPEG frame data
```

**Terminal 1 — start the receiver first:**
```bash
python get_frame.py --port 9000
```

**Terminal 2 — start the sender:**
```bash
python transport.py --sources ../videos/test_01.mp4 --host 127.0.0.1 --port 9000
# multiple sources:
python transport.py --sources 0 1 --host 127.0.0.1 --port 9000 --jpeg-quality 85
```

`transport.py` flags:

| Flag | Default | Description |
|---|---|---|
| `--sources` | required | Camera sources (device index, file path, or RTSP URL) |
| `--host` | required | Receiver host |
| `--port` | required | Receiver port |
| `--timestamp-format` | `iso` | `iso` (ISO 8601 ms) or `epoch_ms` (Unix ms) |
| `--jpeg-quality` | `90` | JPEG encoding quality (1–100) |
| `--base-delay-ms` | `0` | Fixed artificial delay per packet in ms (simulates network latency) |
| `--jitter-ms` | `0` | Uniform jitter half-range per packet in ms (simulates network jitter) |

`get_frame.py` flags:

| Flag | Default | Description |
|---|---|---|
| `--port` | required | Port to listen on |
| `--host` | `0.0.0.0` | Bind address |
| `--sync` | off | Enable jitter-buffer synchronization (requires two camera streams) |
| `--buffer-delay-ms` | `100` | Jitter buffer depth in ms — larger values reduce sync error at the cost of latency |
| `--fps` | `30.0` | Target playback frame rate |
| `--csv` | none | Path to write per-frame sync metrics CSV (only used with `--sync`) |
| `--stream-ids` | `0 1` | Camera IDs to synchronize |

Press `q` in the display window or `Ctrl+C` in either terminal to shut down cleanly.

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


## RUNNING MEASUREMENTS WITH PRERECORDED VIDEOS
- **Experiment 01: no buffer**
```bash
terminal 1: `python scripts/get_frame.py --port 9000 --sync --buffer-delay-ms 0 --csv logs/no_buf.csv`
terminal 2: `python scripts/transport.py --sources videos/test_01.mp4 videos/test_02.mp4 --host 127.0.0.1 --port 9000 --base-delay-ms 50 --jitter-ms 30`
```

- **Experiment 02: buffer = 100ms**
```bash
terminal 1: `python scripts/get_frame.py --port 9000 --sync --buffer-delay-ms 100 --csv logs/buf100.csv`
terminal 2: `python scripts/transport.py --sources videos/test_01.mp4 videos/test_02.mp4 --host 127.0.0.1 --port 9000 --base-delay-ms 50 --jitter-ms 30`
```

- **Experiment 03: buffer = 300ms**
```bash
terminal 1: `python scripts/get_frame.py --port 9000 --sync --buffer-delay-ms 300 --csv logs/buf300.csv`
terminal 2: `python scripts/transport.py --sources videos/test_01.mp4 videos/test_02.mp4 --host 127.0.0.1 --port 9000 --base-delay-ms 50 --jitter-ms 30`
```

- **After all the three experiments, analyze them together by running:**
```bash
`python scripts/analyze_sync.py logs/no_buf.csv logs/buf100.csv logs/buf300.csv`
```

## RUNNING WITH IP CAM

For this experiment, we used a tapo C211 WiFi camera. This camera works with rtsp after a quick setup within the tapo app. Because the Colgate WiFi does not support this kind of device to device communication, we had to set up a local ad-hoc network using a Macbook as the "router", which the camera would connect to. The camera was set to have a static IP within the tapo app.

The general formula for an rtsp cam url is rtsp://USERNAME:PASSWORD@IP_address:PORT/path

To test our camera connection, we directly displayed the IP camera video by running:
```bash
python capture.py --sources rtsp://jshapiro@colgate.edu:Orlando0@192.168.2.4:554/stream1 
```

Then, the command with `transport.py` was run like below to connect to the camera:
```bash
python3 scripts/transport.py --sources 1 rtsp://jshapiro@colgate.edu:Orlando0@192.168.2.4:554/stream1 --host 0.0.0.0 --port 9000
```

Here, source 1 is the native Macbook webcam, and the rtsp url is used to connect to the WiFi cam. We ran the same three experiments and analysis above comparing the built-in Macbook webcam to the tapo cam broadcasting over the network.


