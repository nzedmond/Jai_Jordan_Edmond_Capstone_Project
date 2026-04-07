"""
OpenCV frame capture with high-resolution timestamps.

Run:
    python capture.py                  # webcam (device 0)
    python capture.py --source 1       # webcam device 1
    python capture.py --source video.mp4
"""

import argparse
import sys
import time
from datetime import datetime, timezone

import cv2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture frames with timestamps.")
    parser.add_argument(
        "--source",
        default="0",
        help="Webcam device index (int) or path to a video file (default: 0)",
    )
    parser.add_argument(
        "--timestamp-format",
        choices=["iso", "epoch_ms"],
        default="iso",
        help="Timestamp format: 'iso' (ISO 8601 with ms) or 'epoch_ms' (Unix ms). Default: iso",
    )
    return parser.parse_args()


def open_source(source: str) -> cv2.VideoCapture:
    """Open webcam index or video file path."""
    # Treat purely numeric strings as device indices
    cap_source = int(source) if source.isdigit() else source
    cap = cv2.VideoCapture(cap_source)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open source: {source!r}")
    return cap


def get_target_fps(cap: cv2.VideoCapture) -> float:
    """Return the stream FPS, falling back to 30 if unavailable."""
    fps = cap.get(cv2.CAP_PROP_FPS)
    return fps if fps and fps > 0 else 30.0


def format_timestamp(fmt: str) -> str:
    now = datetime.now(tz=timezone.utc)
    if fmt == "epoch_ms":
        return str(int(now.timestamp() * 1_000))
    # ISO 8601 with millisecond precision, e.g. 2026-04-06T14:32:01.123Z
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def run(source: str, timestamp_format: str) -> None:
    cap = open_source(source)
    fps = get_target_fps(cap)
    frame_duration = 1.0 / fps  # seconds between frames

    print(f"Source       : {source}")
    print(f"Target FPS   : {fps:.2f}")
    print(f"Timestamp fmt: {timestamp_format}")
    print("Press Ctrl+C or 'q' in the preview window to stop.\n")

    frame_index = 0
    try:
        while True:
            loop_start = time.monotonic()  # Start time of the loop iteration

            ret, frame = cap.read()
            if not ret:
                # End of video file or camera disconnected
                print("[INFO] No more frames — stream ended.")
                break

            timestamp = format_timestamp(timestamp_format)

            # Overlay timestamp on frame
            label = f"{timestamp}  |  frame {frame_index}"
            cv2.putText(
                frame,
                label,
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

            print(f"[{timestamp}] frame_index={frame_index}")

            cv2.imshow("capture.py: press q to quit", frame)

            frame_index += 1

            #  target frame rate for live webcams
            elapsed = time.monotonic() - loop_start
            wait_ms = max(1, int((frame_duration - elapsed) * 1_000))

            # cv2.waitKey also processes window events; 'q' triggers shutdown
            if cv2.waitKey(wait_ms) & 0xFF == ord("q"):
                print("[INFO] 'q' pressed — shutting down.")
                break

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt — shutting down.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        print(f"[INFO] Released resources. Total frames captured: {frame_index}")


def main() -> None:
    args = parse_args()
    try:
        run(args.source, args.timestamp_format)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
