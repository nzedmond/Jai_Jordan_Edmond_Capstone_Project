import argparse
import socket
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2

import single_cam
from multicam_handler import capture_loop

'''Usage:
    python transport.py --sources 0 1 --host 192.168.1.10 --port 9000 --jpeg-quality 85'''


# Header layout (12 bytes, big-endian):
#   [0:8]  uint64  capture timestamp in milliseconds
#   [8:12] uint32  JPEG payload length in bytes
HEADER_FMT = ">QI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 12


def encode_jpeg(frame, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


def make_packet(jpeg_bytes: bytes, ts_ms: int) -> bytes:
    header = struct.pack(HEADER_FMT, ts_ms, len(jpeg_bytes))
    return header + jpeg_bytes


def send_frames(cameras: list, sock: socket.socket, jpeg_quality: int) -> None:
    """Read the latest frame from each camera and send over the TCP socket.

    Deduplicates by ts_ms so the same captured frame is never sent twice,
    even if the send loop runs faster than the camera's frame rate.
    """
    last_ts = {id(cam): -1 for cam in cameras}

    while any(cam.running for cam in cameras):
        for cam in cameras:
            frame, ts_ms = cam.get_frame()
            if frame is None or ts_ms == last_ts[id(cam)]:
                continue
            try:
                jpeg = encode_jpeg(frame, jpeg_quality)
                sock.sendall(make_packet(jpeg, ts_ms))
                last_ts[id(cam)] = ts_ms
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                print(f"[ERROR] Connection lost: {exc}")
                for c in cameras:
                    c.running = False
                return


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
    return parser.parse_args()


def main():
    args = parse_args()

    cameras = [
        single_cam.CameraSource(src, args.timestamp_format) for src in args.sources
    ]
    print(f"[INFO] Opened {len(cameras)} camera(s).")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((args.host, args.port))
    print(f"[INFO] Connected to {args.host}:{args.port}")

    try:
        with ThreadPoolExecutor(max_workers=len(cameras)) as executor:
            futures = {executor.submit(capture_loop, cam): cam for cam in cameras}
            send_frames(cameras, sock, args.jpeg_quality)
            for future in as_completed(futures):
                future.result()
    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt — shutting down.")
    finally:
        for cam in cameras:
            cam.running = False
            cam.release()
        sock.close()
        print("[INFO] All cameras released, socket closed.")


if __name__ == "__main__":
    main()
