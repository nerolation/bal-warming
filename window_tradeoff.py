"""
Window-size trade-off analysis.

For each candidate window size W, measure:
  - WAM size (# distinct items in the rolling refcounted multiset)
  - hits per block (# cold ops that would flip to warm)
  - efficiency: hits per million items of WAM held in memory

Output: per-W summary table + parquet for plotting.

Run: python3 window_tradeoff.py --dir data --out data/window_tradeoff.parquet
"""
import argparse
import glob
import os
import re
import time
from collections import defaultdict, deque

import numpy as np
import pandas as pd


def main(dirpath, out_path, windows):
    files = sorted(
        glob.glob(os.path.join(dirpath, "block_*.parquet")),
        key=lambda p: int(re.search(r"block_(\d+)", p).group(1)),
    )
    if not files:
        raise SystemExit(f"no parquet files in {dirpath}")

    pbs = pd.read_parquet(os.path.join(dirpath, "per_block_savings.parquet"))
    pbs = pbs.set_index("block_number")

    print(f"loading {len(files)} block parquets, measuring WAM at W = {windows}")

    # Per-W rolling state:
    # refcount: item -> # of last W blocks that contain it
    # queue: deque of (block_number, frozenset_of_items_in_that_block)
    per_W = {W: {"refcount": defaultdict(int), "queue": deque()} for W in windows}

    # Output traces (per W, per block): wam_size, hit count (from pbs)
    traces = {W: {"block": [], "wam_size": [], "wam_size_running_max": []} for W in windows}

    t0 = time.time()
    for fi, f in enumerate(files):
        bn = int(re.search(r"block_(\d+)", f).group(1))
        df = pd.read_parquet(f)
        if df.empty:
            continue
        # The set of items contributed by this block to the BAL = unique (addr, storage_key) pairs.
        addr = df["address"].fillna("").to_numpy()
        slot = df["storage_key"].fillna("").to_numpy()
        items = frozenset(zip(addr, slot))
        if not items:
            continue

        for W in windows:
            state = per_W[W]
            rc = state["refcount"]
            q = state["queue"]
            for it in items:
                rc[it] += 1
            q.append((bn, items))
            while len(q) > W:
                _, old_items = q.popleft()
                for it in old_items:
                    rc[it] -= 1
                    if rc[it] == 0:
                        del rc[it]
            traces[W]["block"].append(bn)
            traces[W]["wam_size"].append(len(rc))

        if (fi + 1) % 250 == 0:
            print(f"  {fi+1}/{len(files)} blocks, t={time.time()-t0:.0f}s, "
                  f"WAM[W={max(windows)}]: {len(per_W[max(windows)]['refcount']):,} items")

    print(f"done in {time.time()-t0:.0f}s; building summary...")

    # For each W, summarize over the "fair" subset (blocks at position >= W in the data).
    first_block = int(min(traces[windows[0]]["block"]))
    rows = []
    for W in windows:
        bn_arr = np.array(traces[W]["block"])
        wam_arr = np.array(traces[W]["wam_size"])
        fair_mask = bn_arr - first_block >= W
        wam_fair = wam_arr[fair_mask]
        # Pull hits/cold_count from per_block_savings, restricting to blocks that are in BOTH
        # fair_bns AND pbs.index (pbs may lag a few blocks behind the parquet files if extraction is ongoing).
        fair_bns_raw = bn_arr[fair_mask]
        fair_bns = np.intersect1d(fair_bns_raw, pbs.index.to_numpy())
        if f"hits_W{W}" in pbs.columns and len(fair_bns) > 0:
            hits = pbs.loc[fair_bns, f"hits_W{W}"].to_numpy()
        else:
            hits = None
        cold = pbs.loc[fair_bns, "cold_count"].to_numpy() if len(fair_bns) > 0 else None
        gas_saved = pbs.loc[fair_bns, f"saving_W{W}"].to_numpy() if (f"saving_W{W}" in pbs.columns and len(fair_bns) > 0) else None
        rows.append({
            "W": W,
            "n_fair_blocks": int(fair_mask.sum()),
            "wam_size_median": int(np.median(wam_fair)) if len(wam_fair) else 0,
            "wam_size_mean":   int(np.mean(wam_fair))   if len(wam_fair) else 0,
            "wam_size_p95":    int(np.quantile(wam_fair, 0.95)) if len(wam_fair) else 0,
            "wam_mb_60":       int(np.mean(wam_fair) * 60 / 1e6) if len(wam_fair) else 0,
            "hits_median":     int(np.median(hits)) if hits is not None and len(hits) else 0,
            "hits_mean":       float(np.mean(hits)) if hits is not None and len(hits) else 0.0,
            "cold_median":     int(np.median(cold)) if cold is not None and len(cold) else 0,
            "gas_saved_mean":  float(np.mean(gas_saved)) if gas_saved is not None and len(gas_saved) else 0.0,
        })
    summary = pd.DataFrame(rows)
    # Efficiency metrics
    summary["hits_per_million_wam"] = summary["hits_mean"] / (summary["wam_size_mean"] / 1e6).replace(0, np.nan)
    summary["gas_per_mb_wam"] = summary["gas_saved_mean"] / summary["wam_mb_60"].replace(0, np.nan)

    print("\n=== per-window summary (fair subset) ===")
    pd.options.display.float_format = "{:.2f}".format
    print(summary.to_string(index=False))

    if out_path:
        summary.to_parquet(out_path, index=False)
        print(f"\nwrote {out_path}")

    # Also save the per-W per-block traces for plotting
    if out_path:
        trace_rows = []
        for W in windows:
            for bn, ws in zip(traces[W]["block"], traces[W]["wam_size"]):
                trace_rows.append({"W": W, "block_number": bn, "wam_size": ws})
        traces_df = pd.DataFrame(trace_rows)
        traces_path = out_path.replace(".parquet", "_traces.parquet")
        traces_df.to_parquet(traces_path, index=False)
        print(f"wrote {traces_path} ({len(traces_df):,} rows)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="data")
    p.add_argument("--out", default="data/window_tradeoff.parquet")
    p.add_argument("--windows", default="1,2,4,8,16,32,64,128,256,512,1024,2048,4096")
    args = p.parse_args()
    ws = [int(x) for x in args.windows.split(",")]
    main(args.dir, args.out, ws)
