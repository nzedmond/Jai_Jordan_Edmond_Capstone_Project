import argparse
import socket
import struct
import threading
import time

import cv2
import numpy as np

'''Usage (simple display, single or multi-cam):
    python get_frame.py --port 9000

Usage (synchronized display with CSV logging):
    python get_frame.py --port 9000 --sync --buffer-delay-ms 100 --csv logs/run.csv
    python get_frame.py --port 9000 --sync --buffer-delay-ms 0   --csv logs/no_buf.csv
    python get_frame.py --port 9000 --sync --buffer-delay-ms 300 --csv logs/buf300.csv'''

# Must match transport.py exactly.
# Header layout (13 bytes, big-endian):
#   [0:1]  uint8   camera source ID
#   [1:9]  uint64  capture timestamp in milliseconds
#   [9:13] uint32  JPEG payload length in bytes
HEADER_FMT = ">BQI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 13


def recv_exact(conn: socket.socket, n: int) -> bytes:
    """Read exactly n bytes, raising ConnectionError if the sender closes."""
    buf = b""
    while len(buf) < n:
        if not (chunk := conn.recv(n - len(buf))):
            raise ConnectionError("Connection closed by sender")
        buf += chunk
    return buf


def _receive_loop(
    conn: socket.socket,
    sync_buf,           # SyncBuffer | None
    latest_frames: dict,
    lock: threading.Lock,
    stop: threading.Event,
    frame_counter: list,
) -> None:
    """Background thread: read packets and push into sync_buf or latest_frames."""
    while not stop.is_set():
        try:
            header = recv_exact(conn, HEADER_SIZE)
            cam_id, ts_ms, length = struct.unpack(HEADER_FMT, header)
            jpeg_bytes = recv_exact(conn, length)
        except ConnectionError as exc:
            print(f"[INFO] {exc}")
            stop.set()
            return

        frame = cv2.imdecode(
            np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if frame is None:
            print(f"[WARN] cam {cam_id} ts={ts_ms}: JPEG decode failed, skipping.")
            continue

        if sync_buf is not None:
            sync_buf.push(cam_id, ts_ms, frame)
        else:
            with lock:
                latest_frames[cam_id] = frame
                frame_counter[0] += 1


def parse_args():
    parser = argparse.ArgumentParser(description="TCP frame receiver / display")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Enable jitter-buffer synchronization (requires two camera streams)",
    )
    parser.add_argument(
        "--buffer-delay-ms",
        type=int,
        default=100,
        metavar="MS",
        help="Jitter buffer depth in ms (only used with --sync, default: 100)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
        help="Target playback frame rate (default: 30)",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="Path to write per-frame sync metrics CSV (only used with --sync)",
    )
    parser.add_argument(
        "--stream-ids",
        nargs="+",
        type=int,
        default=[0, 1],
        metavar="ID",
        help="Camera IDs to synchronize (default: 0 1)",
    )
    return parser.parse_args()


def main():
    '''run a TCP server that receives one or more JPEG-encoded video streams, decodes them, and displays them in real time. 
    Two modes: 1) simple display of latest frames from each stream, or 2) synchronized display using a jitter buffer to align frames by their capture timestamps.'''
    
    args = parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(1)
    print(f"[INFO] Listening on {args.host}:{args.port} ...")

    conn, addr = server.accept()
    print(f"[INFO] Connection from {addr}")

    sync_buf = None
    if args.sync:
        from sync import SyncBuffer
        sync_buf = SyncBuffer(
            stream_ids=args.stream_ids,
            buffer_delay_ms=args.buffer_delay_ms,
            target_fps=args.fps,
            csv_path=args.csv,
        )
        print(
            f"[INFO] Sync mode ON — buffer_delay={args.buffer_delay_ms}ms  "
            f"fps={args.fps}  streams={args.stream_ids}"
        )
        if args.csv:
            print(f"[INFO] Logging metrics to {args.csv}")

    stop_event = threading.Event()
    latest_frames: dict = {}
    frame_lock = threading.Lock()
    frame_counter = [0]  # mutable int for the receive thread

    recv_thread = threading.Thread(
        target=_receive_loop,
        args=(conn, sync_buf, latest_frames, frame_lock, stop_event, frame_counter),
        daemon=True,
    )
    recv_thread.start()

    frame_interval = 1.0 / args.fps

    try:
        if args.sync:
            # --- Synchronized display ---
            displayed = 0
            while not stop_event.is_set():
                result = sync_buf.try_consume()
                if result:
                    frames_list = [
                        result["frames"][sid]
                        for sid in sorted(result["frames"])
                    ]
                    combined = np.hstack(frames_list)

                    sync_err = result["sync_error_ms"]
                    lat_a = result["latencies"].get(args.stream_ids[0], 0)
                    lat_b = result["latencies"].get(args.stream_ids[1], 0)
                    cv2.putText(
                        combined,
                        f"sync_err={sync_err}ms  lat=({lat_a},{lat_b})ms",
                        (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 255, 0),
                        2,
                    )
                    cv2.imshow("Synchronized streams (press q to quit)", combined)
                    displayed += 1

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("[INFO] 'q' pressed — shutting down.")
                    break
                time.sleep(frame_interval)

            print(f"[INFO] Displayed {displayed} synchronized frame pairs.")

        else:
            # --- Simple display: show each camera's latest frame as it arrives ---
            shown = 0
            while not stop_event.is_set():
                with frame_lock:
                    snapshot = dict(latest_frames)
                    local_count = frame_counter[0]

                if snapshot:
                    tiles = [snapshot[k] for k in sorted(snapshot)]
                    combined = np.hstack(tiles) if len(tiles) > 1 else tiles[0]
                    cv2.imshow("get_frame (press q to quit)", combined)
                    shown = local_count

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("[INFO] 'q' pressed — shutting down.")
                    break
                time.sleep(frame_interval)

            print(f"[INFO] Received {frame_counter[0]} frames total.")

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt — shutting down.")
    finally:
        stop_event.set()
        if sync_buf:
            sync_buf.close()
        conn.close()
        server.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
