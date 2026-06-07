"""
Plot the W-vs-(state, hits, efficiency) trade-off as two PNGs.

Inputs: data/window_tradeoff.parquet (produced by window_tradeoff.py)
Outputs:
  data/plot_wam_hits.png       — dual-axis: WAM size and hits per block vs W
  data/plot_efficiency.png     — hits per million WAM items vs W
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def main(in_path, out_dir):
    df = pd.read_parquet(in_path).sort_values("W").reset_index(drop=True)
    # Trim biased tail rows (different fair-subset populations).
    # Keep W up to where fair subset is at least 50% of dataset.
    n_total_max = df["n_fair_blocks"].max()
    df = df[df["n_fair_blocks"] >= 0.5 * n_total_max].copy()
    df["wam_mb"] = df["wam_size_mean"] * 60 / 1e6  # 60 bytes per item

    # === Plot 1: dual-axis benefit vs cost ===
    fig, ax1 = plt.subplots(figsize=(9, 5))
    ax2 = ax1.twinx()
    ax1.plot(df["W"], df["wam_size_mean"], "o-", color="#d62728", label="WAM size (items)")
    ax1.set_xscale("log")
    ax1.set_yscale("log")
    ax1.set_xlabel("Window size W (blocks, log)")
    ax1.set_ylabel("WAM size (distinct items, log)", color="#d62728")
    ax1.tick_params(axis="y", labelcolor="#d62728")
    ax1.grid(True, which="both", alpha=0.3)

    ax2.plot(df["W"], df["hits_mean"], "s-", color="#1f77b4", label="Hits per block")
    ax2.set_ylabel("Cold → warm conversions per block", color="#1f77b4")
    ax2.tick_params(axis="y", labelcolor="#1f77b4")

    # Knee annotations
    for w in (8, 32, 128):
        if w in df["W"].values:
            r = df[df["W"] == w].iloc[0]
            ax1.axvline(w, color="gray", linestyle="--", alpha=0.3)
            ax2.annotate(f"W={w}\n{int(r.hits_mean)} hits\n{int(r.wam_size_mean):,} items",
                         xy=(w, r.hits_mean),
                         xytext=(w * 1.1, r.hits_mean - 200),
                         fontsize=8, color="black",
                         bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.9))

    ax1.set_title("Benefit vs cost: hits per block (right) vs WAM size (left) as W grows")
    fig.tight_layout()
    out1 = os.path.join(out_dir, "plot_wam_hits.png")
    fig.savefig(out1, dpi=130)
    print(f"wrote {out1}")
    plt.close(fig)

    # === Plot 2: efficiency curve ===
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(df["W"], df["hits_per_million_wam"], "o-", color="#2ca02c")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Window size W (blocks, log)")
    ax.set_ylabel("Hits per million items held in WAM (log)")
    ax.grid(True, which="both", alpha=0.3)
    ax.set_title("Efficiency drops ~500× from W=1 to W=4096")

    # Annotate the knee
    for w in (8, 64, 256, 1024):
        if w in df["W"].values:
            r = df[df["W"] == w].iloc[0]
            ax.annotate(f"W={w}: {int(r.hits_per_million_wam):,}",
                         xy=(w, r.hits_per_million_wam),
                         xytext=(w * 1.2, r.hits_per_million_wam * 1.2),
                         fontsize=9,
                         bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.8))
    fig.tight_layout()
    out2 = os.path.join(out_dir, "plot_efficiency.png")
    fig.savefig(out2, dpi=130)
    print(f"wrote {out2}")
    plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--in_path", default="data/window_tradeoff.parquet")
    p.add_argument("--out_dir", default="plots")
    args = p.parse_args()
    main(args.in_path, args.out_dir)
