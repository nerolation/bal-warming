"""
Plot W-vs-(benefit, cost) trade-off as simple percentage curves.

Inputs:
  data/window_tradeoff.parquet     (cost side: WAM size)
  data/per_block_savings.parquet   (benefit side: gas % and op-count %)

Outputs (in plots/):
  plot_saving_curve.png   - % gas saved and % cold ops flipped vs W
  plot_state_cost.png     - state held in memory (MB) vs W
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def main(out_dir):
    tradeoff = pd.read_parquet("data/window_tradeoff.parquet").sort_values("W").reset_index(drop=True)
    pbs = pd.read_parquet("data/per_block_savings.parquet").sort_values("block_number").reset_index(drop=True)

    # Use the same fair-comparison subset across all W: blocks with full Wmax-block lookback.
    first_block = pbs["block_number"].min()
    Wmax = max(int(c[len("saving_W"):]) for c in pbs.columns if c.startswith("saving_W"))
    fair = pbs[pbs["block_number"] - first_block >= Wmax]
    ws = sorted(int(c[len("saving_W"):]) for c in pbs.columns if c.startswith("saving_W") and int(c[len("saving_W"):]) >= 1)

    # Median percentages on the fair subset.
    pct_gas = [fair[f"pct_saving_W{w}"].median() for w in ws]
    pct_ops = [fair[f"hit_rate_W{w}"].median() * 100 for w in ws]
    asymptote_gas = fair["pct_max_saving"].median()

    # State cost from tradeoff parquet (mean across fair subset already computed there).
    cost_by_w = dict(zip(tradeoff["W"], tradeoff["wam_size_mean"]))
    ws_cost = [w for w in ws if w in cost_by_w]
    state_mb = [cost_by_w[w] * 60 / 1e6 for w in ws_cost]  # 60 B per entry

    os.makedirs(out_dir, exist_ok=True)

    # ===== Plot 1: benefit curve =====
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(ws, pct_ops, "o-", color="#1f77b4", linewidth=2, label="% of cold opcodes flipped to warm")
    ax.plot(ws, pct_gas, "s-", color="#2ca02c", linewidth=2, label="% of block gas saved")
    ax.axhline(asymptote_gas, color="gray", linestyle="--", alpha=0.6,
               label=f"Asymptote: {asymptote_gas:.1f}% gas (if everything warm)")
    ax.set_xscale("log")
    ax.set_xlabel("Window size W (blocks, log scale)")
    ax.set_ylabel("Per-block median (%)")
    ax.set_title("Multi-block warming: benefit vs window size")
    ax.grid(True, which="both", alpha=0.3)
    ax.set_xlim(ws[0] / 1.3, ws[-1] * 1.3)
    ax.set_ylim(0, max(asymptote_gas + 5, max(pct_ops) + 5))
    # Legend centered both horizontally and vertically (in the empty band between the
    # green gas-saved line at the bottom and the blue cold-ops line, which by W~50 is
    # already past 70 %).
    ax.legend(loc="center", bbox_to_anchor=(0.5, 0.42),
              framealpha=0.92, fontsize=9)

    # Annotate key Ws. In the saturated right region the curve barely separates the
    # labels, so stagger 256/512 below the line at different depths and 128/1024 above.
    y_offsets = {8: +4, 32: +4, 128: +4, 256: -12, 512: -25, 1024: +4}
    for w in (8, 32, 128, 256, 512, 1024):
        if w in ws:
            i = ws.index(w)
            y_off = y_offsets[w]
            ax.annotate(f"W={w}\n{pct_ops[i]:.0f}% ops, {pct_gas[i]:.1f}% gas",
                         xy=(w, pct_ops[i]), xytext=(w, pct_ops[i] + y_off),
                         ha="center", fontsize=9,
                         bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.9),
                         arrowprops=dict(arrowstyle="-", color="gray", alpha=0.4, lw=0.6) if y_off < 0 else None)
    fig.tight_layout()
    out1 = os.path.join(out_dir, "plot_saving_curve.png")
    fig.savefig(out1, dpi=130)
    plt.close(fig)
    print(f"wrote {out1}")

    # ===== Plot 2: state cost =====
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(ws_cost, state_mb, "o-", color="#d62728", linewidth=2)
    ax.set_xscale("log")
    ax.set_xlabel("Window size W (blocks, log scale)")
    ax.set_ylabel("WAM state in memory (MB)")
    ax.set_title("Multi-block warming: in-memory state cost vs window size")
    ax.grid(True, which="both", alpha=0.3)
    ax.set_xlim(ws_cost[0] / 1.3, ws_cost[-1] * 1.3)

    for w in (8, 32, 128, 256, 512, 1024):
        if w in ws_cost:
            i = ws_cost.index(w)
            ax.annotate(f"W={w}: {state_mb[i]:.1f} MB",
                         xy=(w, state_mb[i]), xytext=(w * 1.2, state_mb[i] + max(state_mb) * 0.05),
                         fontsize=9,
                         bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.9))
    fig.tight_layout()
    out2 = os.path.join(out_dir, "plot_state_cost.png")
    fig.savefig(out2, dpi=130)
    plt.close(fig)
    print(f"wrote {out2}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out_dir", default="plots")
    args = p.parse_args()
    main(args.out_dir)
