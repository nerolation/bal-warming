"""
Cross-block warming savings analysis.

Reads per-block parquet files produced by extract_block.py. For each block,
counts how many of its cold accesses (storage slots and account addresses)
were already accessed within a sliding window of W previous blocks. Reports
hypothetical gas savings if those would have remained warm.

EIP-2929 cold/warm differentials applied:
  SLOAD                      cold->warm saves 2000  (2100 - 100)
  SSTORE (cold portion)      cold->warm saves 2100
  CALL/STATICCALL/DELEG/CC   cold->warm saves 2500  (2600 - 100)
  BALANCE/EXTCODE*           cold->warm saves 2500
  SELFDESTRUCT               cold->warm saves 2600  (no warm-default to subtract)

Run: python3 warming_analysis.py --dir /tmp/balwarming --windows 1,4,16,32
"""
import argparse, glob, os, re
from collections import deque
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


def access_key(row):
    """Composite key per access: (address, slot) for SLOAD/SSTORE, address for the rest."""
    if row.opcode in ("SLOAD", "SSTORE"):
        return (row.address, row.storage_key)
    return (row.address, None)


def main(dirpath, windows):
    files = sorted(glob.glob(os.path.join(dirpath, "block_*.parquet")),
                   key=lambda p: int(re.search(r"block_(\d+)", p).group(1)))
    if not files:
        print(f"no parquet files in {dirpath}")
        return
    print(f"loading {len(files)} block files...")
    dfs = []
    for f in files:
        d = pd.read_parquet(f)
        d["block_number"] = int(re.search(r"block_(\d+)", f).group(1))
        dfs.append(d)
    df = pd.concat(dfs, ignore_index=True)
    print(f"loaded {len(df):,} rows across {df.block_number.nunique()} blocks")

    # Per-block set of access keys (for ALL accesses, both cold and warm — those
    # are what the *next* block could find as already-warmed).
    df["key"] = df.apply(access_key, axis=1)
    block_keys = df.groupby("block_number")["key"].apply(set)

    # For each block, what fraction of *cold* accesses were keys present in the
    # rolling window of W previous blocks?
    cold_rows = df[df["warm_cold"] == "cold"].copy()
    cold_rows["full_saving"] = cold_rows["opcode"].map(SAVING).fillna(0).astype(int)

    blocks_sorted = sorted(block_keys.index)
    summary = []

    for W in windows:
        recent = deque()  # last W blocks' key sets
        union_set = set()
        rolling_match_count = 0
        rolling_match_savings = 0
        for bn in blocks_sorted:
            block_cold = cold_rows[cold_rows.block_number == bn]
            if union_set:
                hit_mask = block_cold["key"].isin(union_set)
                rolling_match_count += int(hit_mask.sum())
                rolling_match_savings += int((block_cold.loc[hit_mask, "full_saving"]).sum())
            # advance window
            recent.append(block_keys[bn])
            if len(recent) > W:
                # Rebuild union when popping is needed (cheap for small W)
                recent.popleft()
                union_set = set().union(*recent)
            else:
                union_set |= block_keys[bn]

        total_cold = len(cold_rows)
        total_full_saving = int(cold_rows["full_saving"].sum())
        summary.append({
            "window_blocks": W,
            "cold_hits": rolling_match_count,
            "total_cold": total_cold,
            "hit_rate": rolling_match_count / total_cold if total_cold else 0,
            "saved_gas": rolling_match_savings,
            "max_possible_saving": total_full_saving,
            "saving_rate": rolling_match_savings / total_full_saving if total_full_saving else 0,
        })

    res = pd.DataFrame(summary)
    print("\n=== Cross-block warming savings ===")
    print(res.to_string(index=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="/tmp/balwarming")
    p.add_argument("--windows", default="1,4,16,32")
    args = p.parse_args()
    main(args.dir, [int(x) for x in args.windows.split(",")])
