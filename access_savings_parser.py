"""
Cross-block storage-warming gas-savings analysis.

Consumes per-block parquet files emitted by ../extract_block.py (each row =
one access opcode with warm_cold already set per EIP-2929).

For each cold access in block N with key (address, storage_key), it records
how many blocks ago the same key was last touched (`block_gap`):

    block_gap = 0          → same block, earlier tx (intra-block warming)
    block_gap = k > 0      → k blocks ago (inter-block warming, window >= k)
    block_gap = ∞          → never seen before, no warming would help

Then for each warming-window size W, we sum the EIP-2929 cold→warm
differential over rows where block_gap <= W. W = 0 isolates the
intra-block (cross-tx) contribution; W > 0 adds inter-block warming.

EIP-2929 differentials applied per opcode:
  SLOAD                                           2000
  SSTORE (cold-access portion only)               2100
  CALL/STATICCALL/DELEGATECALL/CALLCODE           2500
  BALANCE/EXTCODESIZE/EXTCODEHASH/EXTCODECOPY     2500
  SELFDESTRUCT                                    2600   (no warm-default)

Run:
  python3 access_savings_parser.py --dir /tmp/balwarming \
                                   --windows 0,1,2,4,8,16,32,256
"""
import argparse
import glob
import os
import re
import sys

import numpy as np
import pandas as pd

SAVING = {
    "SLOAD": 2000,
    "SSTORE": 2100,
    "CALL": 2500,
    "STATICCALL": 2500,
    "DELEGATECALL": 2500,
    "CALLCODE": 2500,
    "BALANCE": 2500,
    "EXTCODESIZE": 2500,
    "EXTCODEHASH": 2500,
    "EXTCODECOPY": 2500,
    "SELFDESTRUCT": 2600,
}

# Sentinel used to make (address, storage_key) hashable for non-storage opcodes.
NONE_TOKEN = "__none__"


def load_blocks(dirpath: str) -> pd.DataFrame:
    files = sorted(
        glob.glob(os.path.join(dirpath, "block_*.parquet")),
        key=lambda p: int(re.search(r"block_(\d+)", p).group(1)),
    )
    if not files:
        sys.exit(f"no parquet files in {dirpath}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return df


def load_meta(dirpath: str) -> pd.DataFrame:
    files = sorted(
        glob.glob(os.path.join(dirpath, "meta_*.parquet")),
        key=lambda p: int(re.search(r"meta_(\d+)", p).group(1)),
    )
    if not files:
        return pd.DataFrame(columns=["block_number", "gas_used", "gas_limit", "timestamp", "miner", "tx_count"])
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def compute_block_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Add 'block_gap' (uint32; 2**32-1 means 'never seen before')."""
    # Process in temporal order so each row sees only earlier accesses.
    df = df.sort_values(["block_number", "tx_index", "opcode_index"], kind="stable").reset_index(drop=True)

    # Composite key. Use Python-level tuples so hashing is fast and stable.
    addr = df["address"].fillna(NONE_TOKEN).to_numpy()
    slot = df["storage_key"].fillna(NONE_TOKEN).to_numpy()
    bn = df["block_number"].to_numpy()
    NEVER = np.iinfo(np.uint32).max
    gaps = np.full(len(df), NEVER, dtype=np.uint32)

    last_seen: dict = {}
    for i in range(len(df)):
        k = (addr[i], slot[i])
        prev = last_seen.get(k)
        if prev is not None:
            gaps[i] = bn[i] - prev
        last_seen[k] = bn[i]

    df["block_gap"] = gaps
    return df


def warn_on_gaps(df: pd.DataFrame) -> None:
    blocks = sorted(df.block_number.unique().tolist())
    expected = set(range(blocks[0], blocks[-1] + 1))
    missing = expected - set(blocks)
    if missing:
        print(f"WARNING: {len(missing)} block(s) missing in [{blocks[0]}, {blocks[-1]}]; "
              f"savings under non-trivial windows will undercount. First missing: {sorted(missing)[:5]}")


def summarize(df: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
    df["full_saving"] = df["opcode"].map(SAVING).fillna(0).astype(np.int32)
    cold = df[df["warm_cold"] == "cold"]
    total_cold = len(cold)
    total_full = int(cold["full_saving"].sum())

    rows = []
    gap = cold["block_gap"].to_numpy()
    full_saving = cold["full_saving"].to_numpy()
    for W in windows:
        mask = gap <= W
        n = int(mask.sum())
        s = int(full_saving[mask].sum())
        rows.append({
            "window_blocks": W,
            "cold_hits": n,
            "total_cold": total_cold,
            "hit_rate": n / total_cold if total_cold else 0.0,
            "saved_gas": s,
            "max_saving_gas": total_full,
            "saving_rate": s / total_full if total_full else 0.0,
        })
    return pd.DataFrame(rows)


def per_block_savings(df: pd.DataFrame, windows: list, meta: pd.DataFrame) -> pd.DataFrame:
    """One row per block. Columns: block_number, gas_used, timestamp, max_saving,
       saving_W{w}, pct_saving_W{w} for each window w in `windows`."""
    df["full_saving"] = df["opcode"].map(SAVING).fillna(0).astype(np.int32)
    cold_mask = (df["warm_cold"] == "cold").values
    full = df["full_saving"].values
    gap = df["block_gap"].values

    blocks = sorted(df["block_number"].unique().tolist())
    out_rows = []
    # Pre-group row indices by block for speed.
    bn_arr = df["block_number"].values
    block_idx = {bn: np.where(bn_arr == bn)[0] for bn in blocks}
    for bn in blocks:
        idxs = block_idx[bn]
        cold_here = cold_mask[idxs]
        full_here = full[idxs]
        gap_here = gap[idxs]
        max_save = int(full_here[cold_here].sum())
        row = {"block_number": int(bn), "max_saving": max_save}
        for W in windows:
            sel = cold_here & (gap_here <= W)
            row[f"saving_W{W}"] = int(full_here[sel].sum())
        out_rows.append(row)
    out = pd.DataFrame(out_rows)
    if not meta.empty:
        out = out.merge(meta[["block_number", "gas_used", "gas_limit", "timestamp", "tx_count"]],
                        on="block_number", how="left")
        for W in windows:
            out[f"pct_saving_W{W}"] = out[f"saving_W{W}"] / out["gas_used"] * 100.0
        out["pct_max_saving"] = out["max_saving"] / out["gas_used"] * 100.0
    return out


def per_opcode_breakdown(df: pd.DataFrame, W: int) -> pd.DataFrame:
    cold = df[df["warm_cold"] == "cold"].copy()
    if "full_saving" not in cold.columns:
        cold["full_saving"] = cold["opcode"].map(SAVING).fillna(0).astype(np.int32)
    cold["hit_saving"] = np.where(cold["block_gap"] <= W, cold["full_saving"], 0)
    g = cold.groupby("opcode", observed=True)
    out = pd.DataFrame({
        "cold_count": g.size(),
        "saved_gas": g["hit_saving"].sum().astype(int),
        "max_saving": g["full_saving"].sum().astype(int),
    })
    out["saving_rate"] = out["saved_gas"] / out["max_saving"]
    return out.sort_values("max_saving", ascending=False)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default="/tmp/balwarming",
                   help="Directory containing block_*.parquet files from extract_block.py.")
    p.add_argument("--windows", default="0,1,2,4,8,16,32,256",
                   help="Comma-separated warming-window sizes (in blocks).")
    p.add_argument("--out", default=None,
                   help="Optional CSV path to write the window summary.")
    p.add_argument("--per-block-out", default=None,
                   help="Optional path to write per-block savings parquet (consumed by the notebook).")
    args = p.parse_args()
    windows = [int(x) for x in args.windows.split(",")]

    df = load_blocks(args.dir)
    meta = load_meta(args.dir)
    print(f"loaded {len(df):,} rows across {df.block_number.nunique()} blocks "
          f"({df.block_number.min()}..{df.block_number.max()})")
    if meta.empty:
        print("WARNING: no meta_*.parquet files found; per-block percentage-of-gas-used will be unavailable.")
    warn_on_gaps(df)

    df = compute_block_gaps(df)

    summary = summarize(df, windows)
    print("\n=== Saving by warming window ===")
    pd.options.display.float_format = "{:.4f}".format
    print(summary.to_string(index=False))

    Wmax = max(windows)
    print(f"\n=== Per-opcode savings at window={Wmax} ===")
    print(per_opcode_breakdown(df, Wmax).to_string())

    if args.out:
        summary.to_csv(args.out, index=False)
        print(f"\nwrote {args.out}")
    if args.per_block_out:
        pb = per_block_savings(df, windows, meta)
        pb.to_parquet(args.per_block_out, index=False)
        print(f"wrote {args.per_block_out} ({len(pb)} rows, {len(pb.columns)} cols)")


if __name__ == "__main__":
    main()
