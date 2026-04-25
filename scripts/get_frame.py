import argparse
import socket
import struct
import threading
import time

import cv2
import numpy as np

'''Usage:
1. (simple display, single or multi-cam):
    python get_frame.py --port 9000
2. sync mode (jitter buffer aligning frames by timestamp):
    python get_frame.py --port 9000 --sync --buffer-delay-ms 100

2. (synchronized display with CSV logging):
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


def create_server(host: str, port: int, num_cameras: int):
    """Bind a TCP server socket, accept one connection per camera, and return all."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(num_cameras)
    print(f"[INFO] Listening on {host}:{port}, waiting for {num_cameras} camera connection(s)...")
    connections = []
    for i in range(num_cameras):
        conn, addr = server.accept()
        print(f"[INFO] Connection {i + 1}/{num_cameras} from {addr}")
        connections.append(conn)
    return server, connections


def create_sync_buffer(args):
    """Return a SyncBuffer if --sync was requested, otherwise None."""
    if not args.sync:
        return None
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
    return sync_buf


def start_receive_thread(conn, sync_buf, latest_frames, frame_lock, stop_event, frame_counter):
    """Spawn the background receive thread and return it."""
    t = threading.Thread(
        target=_receive_loop,
        args=(conn, sync_buf, latest_frames, frame_lock, stop_event, frame_counter),
        daemon=True,
    )
    t.start()
    return t


def combine_frames(frames_dict: dict):
    """Stack frames from a {cam_id: frame} dict side-by-side.

    Frames are resized to a common height (the tallest frame) before
    horizontal stacking, so mismatched resolutions don't raise a ValueError.
    """
    tiles = [frames_dict[k] for k in sorted(frames_dict)]
    if len(tiles) == 1:
        return tiles[0]
    target_h = max(f.shape[0] for f in tiles)
    resized = [
        cv2.resize(f, (int(f.shape[1] * target_h / f.shape[0]), target_h))
        if f.shape[0] != target_h else f
        for f in tiles
    ]
    return np.hstack(resized)


def overlay_sync_stats(frame, result: dict, stream_ids: list) -> None:
    """Annotate a combined frame with sync-error and per-stream latency."""
    lat_a = result["latencies"].get(stream_ids[0], 0)
    lat_b = result["latencies"].get(stream_ids[1], 0)
    cv2.putText(
        frame,
        f"sync_err={result['sync_error_ms']}ms  lat=({lat_a},{lat_b})ms",
        (10, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.0,
        (0, 255, 0),
        2,
    )


def run_sync_display(sync_buf, stop_event: threading.Event, stream_ids: list, frame_interval: float) -> None:
    """Display loop for synchronized multi-stream mode."""
    displayed = 0
    while not stop_event.is_set():
        result = sync_buf.try_consume()
        if result:
            combined = combine_frames(result["frames"])
            overlay_sync_stats(combined, result, stream_ids)
            cv2.imshow("Synchronized streams (press q to quit)", combined)
            displayed += 1
        if cv2.waitKey(1) & 0xFF == ord("q"):
            print("[INFO] 'q' pressed — shutting down.")
            break
        time.sleep(frame_interval)
    print(f"[INFO] Displayed {displayed} synchronized frame pairs.")


def run_simple_display(latest_frames: dict, frame_lock: threading.Lock, frame_counter: list,
                       stop_event: threading.Event, frame_interval: float) -> None:
    """Display loop for simple latest-frame mode."""
    while not stop_event.is_set():
        with frame_lock:
            snapshot = dict(latest_frames)
        if snapshot:
            cv2.imshow("get_frame (press q to quit)", combine_frames(snapshot))
        if cv2.waitKey(1) & 0xFF == ord("q"):
            print("[INFO] 'q' pressed — shutting down.")
            break
        time.sleep(frame_interval)
    print(f"[INFO] Received {frame_counter[0]} frames total.")


def main():
    args = parse_args()
    num_cameras = len(args.stream_ids)
    server, connections = create_server(args.host, args.port, num_cameras)
    sync_buf = create_sync_buffer(args)

    stop_event = threading.Event()
    latest_frames: dict = {}
    frame_lock = threading.Lock()
    frame_counter = [0]  # mutable int shared across receive threads
    for conn in connections:
        start_receive_thread(conn, sync_buf, latest_frames, frame_lock, stop_event, frame_counter)

    frame_interval = 1.0 / args.fps
    try:
        if args.sync:
            run_sync_display(sync_buf, stop_event, args.stream_ids, frame_interval)
        else:
            run_simple_display(latest_frames, frame_lock, frame_counter, stop_event, frame_interval)
    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt — shutting down.")
    finally:
        stop_event.set()
        if sync_buf:
            sync_buf.close()
        for conn in connections:
            conn.close()
        server.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
