"""
Stress test: parse every tx in a block, verify per-row warm/cold classifications
match the gas-cost signature in the raw structLog. Any mismatch indicates an
access-list tracking bug (likely in revert/exception/CREATE/DELEGATECALL paths).
"""
import os, sys, json, time, requests
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opcode_parser_v2 import parse_trace

RPC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rpc.txt")).read().strip()


def rpc(method, params, timeout=180):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=timeout)
    return r.json()


COLD_SLOAD = {2100}
WARM_SLOAD = {100}
COLD_ACCT_PURE = {2600}
WARM_ACCT_PURE = {100}
COLD_SSTORE = {2200, 5000, 22100}
WARM_SSTORE = {100, 2900, 20000}

def check_row(r, sl):
    """Return (ok, message) for a row given the corresponding structLog step."""
    op = r["opcode"]
    gc = sl["gasCost"]
    wc = r["warm_cold"]
    if op == "SLOAD":
        if wc == "cold" and gc not in COLD_SLOAD:
            return False, f"SLOAD cold but cost={gc}"
        if wc == "warm" and gc not in WARM_SLOAD:
            return False, f"SLOAD warm but cost={gc}"
    elif op == "SSTORE":
        if wc == "cold" and gc not in COLD_SSTORE:
            return False, f"SSTORE cold but cost={gc}"
        if wc == "warm" and gc not in WARM_SSTORE:
            return False, f"SSTORE warm but cost={gc}"
    elif op in ("BALANCE", "EXTCODESIZE", "EXTCODEHASH"):
        if wc == "cold" and gc != 2600:
            return False, f"{op} cold but cost={gc}"
        if wc == "warm" and gc != 100:
            return False, f"{op} warm but cost={gc}"
    elif op == "EXTCODECOPY":
        if wc == "cold" and gc < 2600:
            return False, f"EXTCODECOPY cold but cost={gc}"
        if wc == "warm" and gc < 100:
            return False, f"EXTCODECOPY warm but cost={gc}"
    # CALL family / SELFDESTRUCT / CREATE: cost involves more components, skip
    return True, None


def stress_block(block_number, tx_limit=None):
    blk = rpc("eth_getBlockByNumber", [hex(block_number), True])["result"]
    miner = blk["miner"]
    txs = blk["transactions"]
    if tx_limit:
        txs = txs[:tx_limit]
    print(f"block {block_number}: {len(txs)} txs, miner={miner}")
    total_rows = 0
    total_mismatches = []
    op_counter = Counter()
    cold_warm_counter = Counter()
    t0 = time.time()
    for i, tx in enumerate(txs):
        h = tx["hash"]
        try:
            r = rpc("debug_traceTransaction", [h, {"disableMemory": True, "disableStack": False, "disableStorage": True}], timeout=300)
            sl = r["result"]["structLogs"]
        except Exception as e:
            print(f"  tx {i} {h} TRACE ERROR: {e}")
            continue
        rows = parse_trace(sl, tx_hash=h, tx_to=tx.get("to"), tx_from=tx.get("from"),
                           block_number=block_number, coinbase=miner,
                           access_list=tx.get("accessList"),
                           authorization_list=tx.get("authorizationList"))
        # Re-locate the corresponding structLog step for each row by opcode_index
        for row in rows:
            ok, msg = check_row(row, sl[row["opcode_index"]])
            if not ok:
                total_mismatches.append((h, row["opcode_index"], msg))
            op_counter[row["opcode"]] += 1
            cold_warm_counter[(row["opcode"], row["warm_cold"])] += 1
        total_rows += len(rows)
        if (i + 1) % 25 == 0:
            print(f"  processed {i+1}/{len(txs)} txs, {total_rows} rows, {len(total_mismatches)} mismatches, t={time.time()-t0:.1f}s")
    print(f"\nDONE: {total_rows} rows total, {len(total_mismatches)} mismatches")
    print("opcode counts:", dict(op_counter))
    print("cold/warm split:", dict(cold_warm_counter))
    if total_mismatches:
        print(f"\nFirst 20 mismatches:")
        for m in total_mismatches[:20]:
            print(" ", m)
    return total_mismatches


if __name__ == "__main__":
    # Pick a recent fully-traceable block; cap to 50 txs for speed
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--block", type=int, default=24990200)
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()
    stress_block(args.block, args.limit)
