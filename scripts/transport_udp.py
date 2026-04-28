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

import single_cam
from multicam_handler import capture_loop


"""
UDP sender usage:

python transport_udp.py --sources 0 1 --host 127.0.0.1 --port 9000

With simulated latency/jitter:

python transport_udp.py \
    --sources 0 1 \
    --host 127.0.0.1 \
    --port 9000 \
    --base-delay-ms 50 \
    --jitter-ms 10
"""


# UDP datagram header:
#   uint8   cam_id
#   uint64  capture timestamp ms
#   uint32  frame_seq
#   uint16  chunk_idx
#   uint16  total_chunks
#   uint16  payload_len
UDP_HEADER_FMT = ">BQIHHH"
UDP_HEADER_SIZE = struct.calcsize(UDP_HEADER_FMT)

# Keep below typical Ethernet MTU to reduce IP fragmentation.
MAX_DGRAM_SIZE = 1400
MAX_PAYLOAD_SIZE = MAX_DGRAM_SIZE - UDP_HEADER_SIZE


def encode_jpeg(frame, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


def make_udp_packets(
    jpeg_bytes: bytes,
    ts_ms: int,
    cam_id: int,
    frame_seq: int,
) -> list[bytes]:
    total_chunks = (len(jpeg_bytes) + MAX_PAYLOAD_SIZE - 1) // MAX_PAYLOAD_SIZE

    if total_chunks > 65535:
        raise RuntimeError("Frame too large to chunk into uint16 chunk count")

    packets = []

    for chunk_idx in range(total_chunks):
        start = chunk_idx * MAX_PAYLOAD_SIZE
        end = start + MAX_PAYLOAD_SIZE
        payload = jpeg_bytes[start:end]

        header = struct.pack(
            UDP_HEADER_FMT,
            cam_id,
            ts_ms,
            frame_seq,
            chunk_idx,
            total_chunks,
            len(payload),
        )

        packets.append(header + payload)

    return packets


def _delivery_loop(
    deliver_queue: queue.PriorityQueue,
    sock: socket.socket,
    dest_addr: tuple[str, int],
    cam_id: int,
    cam: single_cam.CameraSource,
) -> None:
    while True:
        deliver_at, _seq, packet_bytes = deliver_queue.get()

        if packet_bytes is None:
            return

        sleep_s = deliver_at - time.time()
        if sleep_s > 0:
            time.sleep(sleep_s)

        try:
            sock.sendto(packet_bytes, dest_addr)
        except OSError as exc:
            print(f"[ERROR] cam {cam_id} UDP send failed: {exc}")
            cam.running = False
            return


def send_camera_frames(
    cam: single_cam.CameraSource,
    cam_id: int,
    sock: socket.socket,
    dest_addr: tuple[str, int],
    jpeg_quality: int,
    base_delay_ms: float = 0.0,
    jitter_ms: float = 0.0,
    burst_prob: float = 0.05,
    burst_duration: float = 10.0,
) -> None:
    deliver_queue: queue.PriorityQueue = queue.PriorityQueue()
    queue_seq = itertools.count()
    frame_seq = itertools.count()

    delivery_t = threading.Thread(
        target=_delivery_loop,
        args=(deliver_queue, sock, dest_addr, cam_id, cam),
        daemon=True,
    )
    delivery_t.start()

    in_burst = False
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

            if jitter_ms > 0:
                if in_burst:
                    jitter = random.uniform(0, jitter_ms)
                    if random.random() < 1.0 / burst_duration:
                        in_burst = False
                else:
                    jitter = 0.0
                    if random.random() < burst_prob:
                        in_burst = True
            else:
                jitter = 0.0

            delay_s = max(0.0, (base_delay_ms + jitter) / 1000.0)
            deliver_at = time.time() + delay_s

            try:
                packets = make_udp_packets(
                    jpeg_bytes=jpeg,
                    ts_ms=ts_ms,
                    cam_id=cam_id,
                    frame_seq=next(frame_seq),
                )
            except RuntimeError as exc:
                print(f"[WARN] cam {cam_id} packetization failed: {exc}")
                continue

            for packet in packets:
                deliver_queue.put((deliver_at, next(queue_seq), packet))

            last_ts = ts_ms

    finally:
        deliver_queue.put((float("inf"), next(queue_seq), None))
        delivery_t.join()


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-cam UDP transport sender")

    parser.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="Camera sources: device index, file path, or RTSP URL",
    )

    parser.add_argument("--host", required=True, help="Receiver host")
    parser.add_argument("--port", type=int, required=True, help="Receiver UDP port")

    parser.add_argument(
        "--timestamp-format",
        choices=["iso", "epoch_ms"],
        default="iso",
        help="Timestamp format for on-frame label",
    )

    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=80,
        metavar="1-100",
        help="JPEG encoding quality",
    )

    parser.add_argument(
        "--base-delay-ms",
        type=float,
        default=0.0,
        metavar="MS",
        help="Simulated base network latency in ms",
    )

    parser.add_argument(
        "--jitter-ms",
        type=float,
        default=0.0,
        metavar="MS",
        help="Maximum extra delay during jitter bursts",
    )

    parser.add_argument(
        "--burst-prob",
        type=float,
        default=0.05,
        metavar="P",
        help="Probability per frame of entering a jitter burst",
    )

    parser.add_argument(
        "--burst-duration",
        type=float,
        default=10.0,
        metavar="N",
        help="Expected number of frames in a jitter burst",
    )

    parser.add_argument(
        "--cam-id-start",
        type=int,
        default=0,
        metavar="N",
        help="First camera ID to use",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    cameras = [
        single_cam.CameraSource(src, args.timestamp_format)
        for src in args.sources
    ]

    print(f"[INFO] Opened {len(cameras)} camera(s).")

    dest_addr = (args.host, args.port)

    # UDP does not require one socket per camera, but keeping one per camera
    # preserves the structure of your original TCP sender.
    sockets = []

    for idx in range(len(cameras)):
        cam_id = args.cam_id_start + idx

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Optional: increase send buffer for bursty traffic.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1_000_000)

        print(f"[INFO] cam {cam_id} ready to send UDP to {args.host}:{args.port}")
        sockets.append(sock)

    try:
        with ThreadPoolExecutor(max_workers=len(cameras) * 2) as executor:
            futures = [
                executor.submit(capture_loop, cam)
                for cam in cameras
            ]

            futures += [
                executor.submit(
                    send_camera_frames,
                    cam,
                    args.cam_id_start + idx,
                    sock,
                    dest_addr,
                    args.jpeg_quality,
                    args.base_delay_ms,
                    args.jitter_ms,
                    args.burst_prob,
                    args.burst_duration,
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

        print("[INFO] All cameras released, UDP sockets closed.")


if __name__ == "__main__":
    main()