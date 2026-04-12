import argparse
import socket
import struct

import cv2
import numpy as np

'''Usage:
    python get_frame.py --port 9000
    python get_frame.py --host 0.0.0.0 --port 9000'''

# Must match transport.py exactly
HEADER_FMT = ">QI"   # big-endian: uint64 ts_ms + uint32 frame length
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 12


def recv_exact(conn: socket.socket, n: int) -> bytes:
    """Read exactly n bytes, raising ConnectionError if the sender closes."""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed by sender")
        buf += chunk
    return buf


def parse_args():
    parser = argparse.ArgumentParser(description="TCP frame receiver / display")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Port to listen on",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(1)
    print(f"[INFO] Listening on {args.host}:{args.port} ...")

    conn, addr = server.accept()
    print(f"[INFO] Connection from {addr}")

    frame_count = 0
    try:
        while True:
            header = recv_exact(conn, HEADER_SIZE)
            ts_ms, length = struct.unpack(HEADER_FMT, header)

            jpeg_bytes = recv_exact(conn, length)

            frame = cv2.imdecode(
                np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if frame is None:
                print(f"[WARN] Frame {frame_count}: JPEG decode failed, skipping.")
                continue

            frame_count += 1
            cv2.imshow("get_frame (press q to quit)", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                print("[INFO] 'q' pressed — shutting down.")
                break

    except ConnectionError as exc:
        print(f"[INFO] {exc}")
    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt — shutting down.")
    finally:
        conn.close()
        server.close()
        cv2.destroyAllWindows()
        print(f"[INFO] Received {frame_count} frames total.")


if __name__ == "__main__":
    main()
