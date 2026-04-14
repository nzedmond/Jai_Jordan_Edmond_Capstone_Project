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
videos/
    test_01.mp4          # Sample video for testing without a live camera
```

---

## Scripts

### `capture.py` — single-camera preview
Reads from one source and overlays a UTC timestamp on every frame. Useful for verifying a camera works before using the multi-camera pipeline.

```bash
python capture.py                        # webcam (device 0)
python capture.py --source 1             # webcam device 1
python capture.py --source video.mp4
python capture.py --timestamp-format epoch_ms
```

---

### `multicam_handler.py` — local multi-camera display
Opens multiple sources simultaneously (each in its own thread) and displays them in a grid window. No network involved — useful for local testing and verifying timestamp alignment before introducing transport.

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
| `--sources` | required | Camera sources |
| `--host` | required | Receiver host |
| `--port` | required | Receiver port |
| `--timestamp-format` | `iso` | Timestamp format for on-frame label |
| `--jpeg-quality` | `90` | JPEG encoding quality (1–100) |

`get_frame.py` flags:

| Flag | Default | Description |
|---|---|---|
| `--port` | required | Port to listen on |
| `--host` | `0.0.0.0` | Bind address |

Press `q` in the display window or `Ctrl+C` in either terminal to shut down cleanly.

---

## Dependencies

```bash
pip install opencv-python numpy
```

Python 3.10+ recommended. All other dependencies (`socket`, `struct`, `threading`, `concurrent.futures`) are from the standard library.
