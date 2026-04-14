import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import List, Optional
import cv2
from numpy import *
import single_cam



def capture_loop(cam: single_cam.CameraSource):
    '''grab frames until the source is exhausted or an error occurs, then signal shutdown by setting cam.running to False.'''
    
    while cam.running:
        frame = cam.read()
        if frame is None:
            cam.running = False
            break
        time.sleep(0.005)  
    return cam


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-cam capture")
    parser.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="List of sources (e.g. '0' for webcam, 'video.mp4' for file, or RTSP URL)",
    )
    parser.add_argument(
        "--timestamp-format",
        choices=["iso", "epoch_ms"],
        default="iso",
        help="Timestamp format: 'iso' (ISO 8601 with ms) or 'epoch_ms' (Unix ms). Default: iso",
    )
    parser.add_argument(
        "--columns",
        type=int,
        default=2,
        help="Number of columns for the grid display",
    )
    return parser.parse_args()


def build_display(frames: List[ndarray], columns: int = 2):
    toShow = []
    for f in frames:
        if f is not None:
            toShow.append(f)

    if not toShow:
        return None

    height = 480
    width = 640
    toShow = [cv2.resize(f, (width, height)) for f in toShow]

    rows = []

    for i in range(0, len(toShow), columns):
        row = toShow[i:i + columns]
        if len(row) < columns:
            blankFrames = zeros((height, width, 3), dtype=uint8)
            row += [blankFrames] * (columns - len(row))
        rows.append(hstack(row))
    return vstack(rows)


def main():
    args = parse_args()
    timestamp_format = args.timestamp_format
    columns = args.columns

    cameras = [single_cam.CameraSource(src, timestamp_format) for src in args.sources]
    print(f"[INFO] Starting {len(cameras)} cameras...")

    try:
        with ThreadPoolExecutor(max_workers=len(cameras)) as executor:
            futures = {executor.submit(capture_loop, cam): cam for cam in cameras}

            while any(cam.running for cam in cameras):
                frames = [cam.frame for cam in cameras]
                grid = build_display(frames, columns)
                if grid is not None:
                    cv2.imshow("Multi-Camera Display (press q to quit)", grid)

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("[INFO] 'q' pressed — shutting down.")
                    break

                time.sleep(0.005)

            for future in as_completed(futures):
                future.result()

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt — shutting down.")

    finally:
        for cam in cameras:
            cam.running = False
            cam.release()
        cv2.destroyAllWindows()
        print("[INFO] All cameras released.")


if __name__ == "__main__":
    main()