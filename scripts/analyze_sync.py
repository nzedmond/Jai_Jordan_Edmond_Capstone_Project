"""Sync experiment analysis — reads a CSV produced by get_frame.py --sync
and produces three plots:

  1. Sync error over time (frame index)
  2. CDF of absolute sync error
  3. Latency histogram for both streams

Usage:
    python analyze_sync.py logs/buf100.csv
    python analyze_sync.py logs/no_buf.csv logs/buf100.csv logs/buf300.csv
    python analyze_sync.py logs/run.csv --no-show --out figures/
"""

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    required = {"frame_index", "sync_error_ms", "latency_a_ms", "latency_b_ms"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path}: missing columns {missing}")
    return df


def label_from_path(path: str) -> str:
    return Path(path).stem


# ---------------------------------------------------------------------------
# Plot 1 — Sync error over time
# ---------------------------------------------------------------------------

def plot_sync_error(dfs: list, labels: list, out_dir: Path, show: bool) -> None:
    fig, ax = plt.subplots(figsize=(10, 4))
    for df, label in zip(dfs, labels):
        ax.plot(df["frame_index"], df["sync_error_ms"], linewidth=0.8, label=label)
    ax.set_xlabel("Frame index")
    ax.set_ylabel("Sync error (ms)")
    ax.set_title("Sync error over time")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path = out_dir / "sync_error_over_time.png"
    fig.savefig(out_path, dpi=150)
    print(f"[INFO] Saved {out_path}")
    if show:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# Plot 2 — CDF of absolute sync error
# ---------------------------------------------------------------------------

def plot_sync_error_cdf(dfs: list, labels: list, out_dir: Path, show: bool) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    for df, label in zip(dfs, labels):
        vals = np.sort(np.abs(df["sync_error_ms"].dropna().values))
        cdf = np.arange(1, len(vals) + 1) / len(vals)
        ax.plot(vals, cdf, linewidth=1.5, label=label)

    ax.set_xlabel("|Sync error| (ms)")
    ax.set_ylabel("CDF")
    ax.set_title("CDF of absolute sync error")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # Mark 50th and 95th percentile lines
    ax.axhline(0.50, color="gray", linestyle="--", linewidth=0.7, alpha=0.6)
    ax.axhline(0.95, color="gray", linestyle=":",  linewidth=0.7, alpha=0.6)
    ax.text(ax.get_xlim()[1] * 0.98, 0.51, "p50", ha="right", fontsize=8, color="gray")
    ax.text(ax.get_xlim()[1] * 0.98, 0.96, "p95", ha="right", fontsize=8, color="gray")

    fig.tight_layout()
    out_path = out_dir / "sync_error_cdf.png"
    fig.savefig(out_path, dpi=150)
    print(f"[INFO] Saved {out_path}")
    if show:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# Plot 3 — Latency histogram
# ---------------------------------------------------------------------------

def plot_latency_histogram(dfs: list, labels: list, out_dir: Path, show: bool) -> None:
    n_runs = len(dfs)
    fig, axes = plt.subplots(1, n_runs, figsize=(6 * n_runs, 4), squeeze=False)

    for ax, df, label in zip(axes[0], dfs, labels):
        lat_a = df["latency_a_ms"].dropna().values
        lat_b = df["latency_b_ms"].dropna().values

        bins = np.linspace(
            min(lat_a.min(), lat_b.min()),
            max(lat_a.max(), lat_b.max()),
            40,
        )
        ax.hist(lat_a, bins=bins, alpha=0.6, label="cam A")
        ax.hist(lat_b, bins=bins, alpha=0.6, label="cam B")
        ax.set_xlabel("Latency (ms)")
        ax.set_ylabel("Count")
        ax.set_title(f"Latency — {label}")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # Annotate medians
        for vals, name, color in [(lat_a, "A", "C0"), (lat_b, "B", "C1")]:
            med = np.median(vals)
            ax.axvline(med, color=color, linestyle="--", linewidth=1.2)
            ax.text(med, ax.get_ylim()[1] * 0.9, f"med={med:.0f}ms",
                    ha="center", fontsize=8, color=color)

    fig.tight_layout()
    out_path = out_dir / "latency_histogram.png"
    fig.savefig(out_path, dpi=150)
    print(f"[INFO] Saved {out_path}")
    if show:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(dfs: list, labels: list) -> None:
    print(f"\n{'Run':<20} {'n':>6} {'sync_p50':>10} {'sync_p95':>10} "
          f"{'lat_a_med':>12} {'lat_b_med':>12}")
    print("-" * 74)
    for df, label in zip(dfs, labels):
        sync = np.abs(df["sync_error_ms"].dropna().values)
        lat_a = df["latency_a_ms"].dropna().values
        lat_b = df["latency_b_ms"].dropna().values
        print(
            f"{label:<20} {len(df):>6} "
            f"{np.percentile(sync, 50):>9.1f}ms "
            f"{np.percentile(sync, 95):>9.1f}ms "
            f"{np.median(lat_a):>11.1f}ms "
            f"{np.median(lat_b):>11.1f}ms"
        )
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Analyze sync experiment CSV logs")
    parser.add_argument("csvs", nargs="+", metavar="CSV", help="One or more CSV log files")
    parser.add_argument(
        "--out",
        default="figures",
        metavar="DIR",
        help="Output directory for PNG files (default: figures/)",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="Save plots without displaying them interactively",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dfs, labels = [], []
    for path in args.csvs:
        try:
            dfs.append(load(path))
            labels.append(label_from_path(path))
            print(f"[INFO] Loaded {path}  ({len(dfs[-1])} rows)")
        except (FileNotFoundError, ValueError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)

    show = not args.no_show

    print_summary(dfs, labels)
    plot_sync_error(dfs, labels, out_dir, show)
    plot_sync_error_cdf(dfs, labels, out_dir, show)
    plot_latency_histogram(dfs, labels, out_dir, show)


if __name__ == "__main__":
    main()
