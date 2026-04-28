import argparse
import itertools
import queue
import random
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2

import clock_sync
import single_cam
from multicam_handler import capture_loop

'''Usage:
1. simple (both streams shown b=side-by-side, latest frame per camera):
    python transport.py --sources 0 1 --host 0.0.0.0 --port 9000
2. sync mode (add artificial jitter to test transport delay effects):
    python transport.py --sources 0 1 --host 0.0.0.0 --port 9000 --base-delay-ms 50 --jitter-ms 10
3. adjust JPEG quality (tradeoff between latency and image quality):
    python transport.py --sources 0 1 --host 192.168.1.10 --port 9000 --jpeg-quality 85
 '''


# Header layout (13 bytes, big-endian):
#   [0:1]  uint8   camera source ID (0-indexed position in --sources list)
#   [1:9]  uint64  capture timestamp in milliseconds
#   [9:13] uint32  JPEG payload length in bytes
HEADER_FMT = ">BQI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 13


def encode_jpeg(frame, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


def make_packet(jpeg_bytes: bytes, ts_ms: int, cam_id: int) -> bytes:
    header = struct.pack(HEADER_FMT, cam_id, ts_ms, len(jpeg_bytes))
    return header + jpeg_bytes


def _delivery_loop(
    deliver_queue: queue.PriorityQueue,
    sock: socket.socket,
    cam_id: int,
    cam: single_cam.CameraSource,
) -> None:
    """Send packets at their scheduled delivery time.

    Runs in a dedicated thread so a packet with a shorter delay can overtake
    one with a longer delay, matching real network out-of-order arrival.
    Exits when it dequeues the sentinel (packet_bytes=None).
    """
    while True:
        deliver_at, _seq, packet_bytes = deliver_queue.get()
        if packet_bytes is None:  # sentinel — all frames have been queued
            return
        sleep_s = deliver_at - time.time()
        if sleep_s > 0:
            time.sleep(sleep_s)
        try:
            sock.sendall(packet_bytes)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            print(f"[ERROR] cam {cam_id} connection lost: {exc}")
            cam.running = False
            return


def send_camera_frames(
    cam: single_cam.CameraSource,
    cam_id: int,
    sock: socket.socket,
    jpeg_quality: int,
    base_delay_ms: float = 0.0,
    jitter_ms: float = 0.0,
) -> None:
    """Encode frames and schedule them for delivery at the correct simulated arrival time.

    Each packet's delivery time is computed independently as:
        deliver_at = time.time() + base_delay_ms + uniform(-jitter_ms, +jitter_ms)

    Packets are placed in a priority queue sorted by deliver_at, so a packet
    with a shorter delay overtakes one with a longer delay — simulating true
    end-to-end network latency and out-of-order arrival rather than just
    varying inter-packet transmission gaps.
    """
    deliver_queue: queue.PriorityQueue = queue.PriorityQueue()
    seq = itertools.count()

    delivery_t = threading.Thread(
        target=_delivery_loop,
        args=(deliver_queue, sock, cam_id, cam),
        daemon=True,
    )
    delivery_t.start()

    last_ts = -1
    try:
        while cam.running:
            frame, ts_ms = cam.get_frame()
            if frame is None or ts_ms == last_ts:
                continue
            try:
                jpeg = encode_jpeg(frame, jpeg_quality)
            except RuntimeError as exc:
                print(f"[WARN] cam {cam_id} encode failed: {exc}")
                continue
            jitter = random.uniform(-jitter_ms, jitter_ms) if jitter_ms > 0 else 0.0
            delay_s = max(0.0, (base_delay_ms + jitter) / 1000.0)
            deliver_queue.put((time.time() + delay_s, next(seq), make_packet(jpeg, ts_ms, cam_id)))
            last_ts = ts_ms
    finally:
        deliver_queue.put((float("inf"), next(seq), None))  # sentinel, always dequeued last
        delivery_t.join()

def parse_args():
    parser = argparse.ArgumentParser(description="Multi-cam TCP transport sender")
    parser.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="Camera sources (device index, file path, or RTSP URL)",
    )
    parser.add_argument("--host", required=True, help="Receiver host")
    parser.add_argument("--port", type=int, required=True, help="Receiver port")
    parser.add_argument(
        "--timestamp-format",
        choices=["iso", "epoch_ms"],
        default="iso",
        help="Timestamp format for on-frame label (default: iso)",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=90,
        metavar="1-100",
        help="JPEG encoding quality (default: 90)",
    )
    parser.add_argument(
        "--base-delay-ms",
        type=float,
        default=0.0,
        metavar="MS",
        help="Simulated end-to-end network latency in ms: each packet is delivered "
             "base_delay_ms after it is captured (default: 0)",
    )
    parser.add_argument(
        "--jitter-ms",
        type=float,
        default=0.0,
        metavar="MS",
        help="Uniform jitter half-range in ms added on top of base-delay-ms (default: 0). "
             "Each packet's total delay = base_delay + uniform(-jitter, +jitter), clamped >= 0.",
    )
    parser.add_argument(
        "--cam-id-start",
        type=int,
        default=0,
        metavar="N",
        help="First camera ID to use when labeling streams (default: 0). "
             "Set to 1 on the second machine so its stream is cam_id=1.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    cameras = [
        single_cam.CameraSource(src, args.timestamp_format) for src in args.sources
    ]
    print(f"[INFO] Opened {len(cameras)} camera(s).")

    # One independent socket per camera so encoding/sending never blocks across cameras.
    sockets = []
    for idx in range(len(cameras)):
        cam_id = args.cam_id_start + idx
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((args.host, args.port))
        clock_sync.serve_clock_sync(sock)
        print(f"[INFO] cam {cam_id} connected and clock-synced to {args.host}:{args.port}")
        sockets.append(sock)

    try:
        with ThreadPoolExecutor(max_workers=len(cameras) * 2) as executor:
            futures = [executor.submit(capture_loop, cam) for cam in cameras]
            futures += [
                executor.submit(
                    send_camera_frames, cam, args.cam_id_start + idx, sock,
                    args.jpeg_quality, args.base_delay_ms, args.jitter_ms,
                )
                for idx, (cam, sock) in enumerate(zip(cameras, sockets))
            ]
            for future in as_completed(futures):
                future.result()
    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt — shutting down.")
    finally:
        for cam in cameras:
            cam.running = False
            cam.release()
        for sock in sockets:
            sock.close()
        print("[INFO] All cameras released, sockets closed.")


if __name__ == "__main__":
    main()
