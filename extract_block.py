"""
Extract per-opcode access events for a block range and write parquet.

Output schema (one row per access opcode):
  block_number, tx_hash, tx_index, opcode, opcode_index, pc, depth,
  gas_cost (corrected for CALL family), warm_cold, address, storage_key,
  frame_kind

Run: python3 extract_block.py --start 24990200 --end 24990200 --out /tmp/blk
"""
import argparse, json, os, sys, time, asyncio
import aiohttp
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opcode_parser_v2 import parse_trace

RPC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rpc.txt")).read().strip()


async def rpc_one(session, method, params):
    async with session.post(RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}) as r:
        return await r.json()


async def trace_tx(session, tx_hash):
    r = await rpc_one(session, "debug_traceTransaction",
                      [tx_hash, {"disableMemory": True, "disableStack": False, "disableStorage": True}])
    return r.get("result", {}).get("structLogs", []) if "result" in r else []


async def process_block(session, block_number, semaphore):
    async with semaphore:
        blk = (await rpc_one(session, "eth_getBlockByNumber", [hex(block_number), True]))["result"]
    miner = blk["miner"]
    txs = blk["transactions"]
    rows_all = []
    meta = {
        "block_number": block_number,
        "gas_used": int(blk["gasUsed"], 16),
        "gas_limit": int(blk["gasLimit"], 16),
        "timestamp": int(blk["timestamp"], 16),
        "miner": miner,
        "tx_count": len(txs),
    }

    async def one(tx):
        sl = await trace_tx(session, tx["hash"])
        if not sl:
            return []
        return parse_trace(
            sl,
            tx_hash=tx["hash"],
            tx_to=tx.get("to"),
            tx_from=tx.get("from"),
            block_number=block_number,
            coinbase=miner,
            access_list=tx.get("accessList"),
            authorization_list=tx.get("authorizationList"),
        )

    # rate-limit per-block tracing concurrency
    bounded = asyncio.Semaphore(16)
    async def bounded_one(tx):
        async with bounded:
            return await one(tx)

    results = await asyncio.gather(*[bounded_one(tx) for tx in txs])
    for tx, rows in zip(txs, results):
        tx_index = int(tx["transactionIndex"], 16)
        for r in rows:
            r["tx_index"] = tx_index
        rows_all.extend(rows)
    return rows_all, meta


async def main_async(args):
    os.makedirs(args.out, exist_ok=True)
    connector = aiohttp.TCPConnector(limit=64)
    timeout = aiohttp.ClientTimeout(total=600)
    sem = asyncio.Semaphore(args.block_concurrency)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for bn in range(args.start, args.end + 1):
            out = os.path.join(args.out, f"block_{bn}.parquet")
            meta_out = os.path.join(args.out, f"meta_{bn}.parquet")
            if not args.overwrite and os.path.exists(out) and os.path.exists(meta_out):
                continue
            t0 = time.time()
            rows, meta = await process_block(session, bn, sem)
            df = pd.DataFrame(rows)
            df.to_parquet(out, index=False, compression="snappy")
            pd.DataFrame([meta]).to_parquet(meta_out, index=False, compression="snappy")
            cold = (df["warm_cold"] == "cold").sum() if not df.empty else 0
            print(f"block {bn}: {len(df)} rows, {cold} cold, gas_used={meta['gas_used']:,}, "
                  f"{time.time()-t0:.1f}s -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    p.add_argument("--out", default="/tmp/balwarming")
    p.add_argument("--block-concurrency", type=int, default=1)
    p.add_argument("--overwrite", action="store_true",
                   help="Re-extract blocks even if their parquet files already exist.")
    args = p.parse_args()
    asyncio.run(main_async(args))
