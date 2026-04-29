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
    required = {"frame_index", "sync_error_ms"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path}: missing columns {missing}")
    if not get_cam_ids(df):
        raise ValueError(f"{csv_path}: no latency_{{id}}_ms columns found")
    return df


def get_cam_ids(df: pd.DataFrame) -> list:
    """Return sorted camera IDs inferred from latency_{id}_ms column names."""
    ids = []
    for col in df.columns:
        if col.startswith("latency_") and col.endswith("_ms"):
            try:
                ids.append(int(col[len("latency_"):-len("_ms")]))
            except ValueError:
                pass
    return sorted(ids)


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
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for ax, df, label in zip(axes[0], dfs, labels):
        cam_ids = get_cam_ids(df)
        all_vals = [df[f"latency_{cid}_ms"].dropna().values for cid in cam_ids]

        flat = np.concatenate(all_vals)
        bins = np.linspace(flat.min(), flat.max(), 40)

        for i, (vals, cid) in enumerate(zip(all_vals, cam_ids)):
            color = colors[i % len(colors)]
            ax.hist(vals, bins=bins, alpha=0.6, label=f"cam {cid}", color=color)
            med = np.median(vals)
            ax.axvline(med, color=color, linestyle="--", linewidth=1.2)
            ax.text(med, ax.get_ylim()[1] * 0.9, f"med={med:.0f}ms",
                    ha="center", fontsize=8, color=color)

        ax.set_xlabel("Latency (ms)")
        ax.set_ylabel("Count")
        ax.set_title(f"Latency — {label}")
        ax.legend()
        ax.grid(True, alpha=0.3)

    fig.tight_layout()
    out_path = out_dir / "latency_histogram.png"
    fig.savefig(out_path, dpi=150)
    print(f"[INFO] Saved {out_path}")
    if show:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# Plot 4 — Phase 1 box plot
# ---------------------------------------------------------------------------

def plot_phase1_boxplot(dfs: list, labels: list, out_dir: Path, show: bool,
                        phase1_threshold_ms: int = 500) -> None:
    """Box-and-whisker plot of sync error for Phase 1 frames only.

    Phase 1 is approximated as all frames with sync_error_ms <= phase1_threshold_ms,
    which excludes the Phase 2 frozen-frame tail without requiring manual frame boundaries.
    """
    phase1_data = []
    for df in dfs:
        vals = df.loc[df["sync_error_ms"] <= phase1_threshold_ms, "sync_error_ms"].values
        phase1_data.append(vals)

    fig, ax = plt.subplots(figsize=(7, 5))
    bp = ax.boxplot(phase1_data, labels=labels, patch_artist=True, notch=False)

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)

    ax.set_xlabel("Buffer depth (run)")
    ax.set_ylabel("Sync error (ms)")
    ax.set_title(
        f"Phase 1 sync error distribution\n"
        f"(frames with sync error ≤ {phase1_threshold_ms} ms)"
    )
    ax.grid(True, alpha=0.3, axis="y")

    # Annotate median value above each box
    for i, vals in enumerate(phase1_data, start=1):
        med = float(np.median(vals))
        ax.text(i, med + 2, f"{med:.0f} ms", ha="center", va="bottom", fontsize=8)

    fig.tight_layout()
    out_path = out_dir / "phase1_boxplot.png"
    fig.savefig(out_path, dpi=150)
    print(f"[INFO] Saved {out_path}")
    if show:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(dfs: list, labels: list) -> None:
    all_cam_ids = sorted({cid for df in dfs for cid in get_cam_ids(df)})
    lat_headers = "  ".join(f"{'lat_' + str(cid) + '_med':>12}" for cid in all_cam_ids)
    print(f"\n{'Run':<20} {'n':>6} {'sync_p50':>10} {'sync_p95':>10}  {lat_headers}")
    print("-" * (46 + 14 * len(all_cam_ids)))
    for df, label in zip(dfs, labels):
        sync = np.abs(df["sync_error_ms"].dropna().values)
        lat_meds = []
        for cid in all_cam_ids:
            col = f"latency_{cid}_ms"
            val = f"{np.median(df[col].dropna().values):>11.1f}ms" if col in df.columns else f"{'N/A':>12}"
            lat_meds.append(val)
        print(
            f"{label:<20} {len(df):>6} "
            f"{np.percentile(sync, 50):>9.1f}ms "
            f"{np.percentile(sync, 95):>9.1f}ms  "
            + "  ".join(lat_meds)
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
    plot_phase1_boxplot(dfs, labels, out_dir, show)


if __name__ == "__main__":
    main()
