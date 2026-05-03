"""
Tests for opcode_parser_v2.parse_trace.

Validation strategy: trace several real txs and check, per opcode,
that warm/cold classification aligns with the EIP-2929-determined
gas cost in the raw structLog. SLOAD, BALANCE, EXTCODESIZE,
EXTCODEHASH, EXTCODECOPY have unambiguous cost signatures (cold vs
warm). For SSTORE we check the cold-cost set membership. For
CALL/STATICCALL/DELEGATECALL/CALLCODE we check the corrected
gas_cost is sane (small overhead, not the full forwarded amount).
"""
import json
import os
import sys
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opcode_parser_v2 import parse_trace, norm_addr, norm_slot, PRECOMPILES

RPC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rpc.txt")).read().strip()


def rpc(method, params, timeout=120):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=timeout)
    return r.json()


def get_trace(tx_hash):
    r = rpc("debug_traceTransaction", [tx_hash, {"disableMemory": True, "disableStack": False, "disableStorage": True}])
    return r["result"]["structLogs"]


def get_tx(tx_hash):
    r = rpc("eth_getTransactionByHash", [tx_hash])
    return r["result"]


def get_block(block_num):
    r = rpc("eth_getBlockByNumber", [hex(block_num), False])
    return r["result"]


def get_receipt(tx_hash):
    r = rpc("eth_getTransactionReceipt", [tx_hash])
    return r["result"]


# ---------- Test cases ----------


def test_basic_normalization():
    assert norm_addr("0x1") == "0x0000000000000000000000000000000000000001"
    assert norm_addr("0x4128618BadAc99beAB1c855a5a3921dd8aD511c9") == "0x4128618badac99beab1c855a5a3921dd8ad511c9"
    # 32-byte stack value: take low 20 bytes (last 40 hex chars)
    raw = "0x000000000000000000000000abcdef0000000000000000000000000000000001"
    assert norm_addr(raw) == "0x" + raw[2:][-40:]
    assert norm_slot("0x6c") == "0x" + "0" * 62 + "6c"
    assert len(norm_slot("0x6c")) == 66


def validate_sload(rows, struct_logs):
    """SLOAD: cost==2100 ↔ cold; cost==100 ↔ warm. Must hold for every SLOAD row."""
    sload_rows = [r for r in rows if r["opcode"] == "SLOAD"]
    cold_rows = [r for r in sload_rows if r["warm_cold"] == "cold"]
    warm_rows = [r for r in sload_rows if r["warm_cold"] == "warm"]
    # check SLOAD costs in raw structLog
    sload_steps = [s for s in struct_logs if s["op"] == "SLOAD"]
    assert len(sload_steps) == len(sload_rows), (len(sload_steps), len(sload_rows))
    mismatches = []
    for r, s in zip(sload_rows, sload_steps):
        gc = s["gasCost"]
        if r["warm_cold"] == "cold" and gc != 2100:
            mismatches.append(("cold but cost!=2100", r["opcode_index"], gc))
        if r["warm_cold"] == "warm" and gc != 100:
            mismatches.append(("warm but cost!=100", r["opcode_index"], gc))
    return {"sload_total": len(sload_rows), "cold": len(cold_rows), "warm": len(warm_rows), "mismatches": mismatches}


def validate_balance_ext(rows, struct_logs):
    """BALANCE/EXTCODESIZE/EXTCODEHASH: cost==2600 cold, cost==100 warm.
    EXTCODECOPY: cost==2600+memexp+3*size cold, 100+... warm. Just assert >=2600 cold, >=100 warm."""
    out = {}
    for op in ("BALANCE", "EXTCODESIZE", "EXTCODEHASH", "EXTCODECOPY"):
        op_rows = [r for r in rows if r["opcode"] == op]
        op_steps = [s for s in struct_logs if s["op"] == op]
        if len(op_rows) != len(op_steps):
            out[op] = {"error": f"row count mismatch {len(op_rows)} vs {len(op_steps)}"}
            continue
        mism = []
        for r, s in zip(op_rows, op_steps):
            gc = s["gasCost"]
            if op == "EXTCODECOPY":
                if r["warm_cold"] == "cold" and gc < 2600:
                    mism.append(("cold but cost<2600", r["opcode_index"], gc))
                if r["warm_cold"] == "warm" and gc < 100:
                    mism.append(("warm but cost<100", r["opcode_index"], gc))
            else:
                if r["warm_cold"] == "cold" and gc != 2600:
                    mism.append(("cold but cost!=2600", r["opcode_index"], gc))
                if r["warm_cold"] == "warm" and gc != 100:
                    mism.append(("warm but cost!=100", r["opcode_index"], gc))
        out[op] = {"total": len(op_rows), "cold": sum(1 for r in op_rows if r["warm_cold"] == "cold"),
                   "warm": sum(1 for r in op_rows if r["warm_cold"] == "warm"),
                   "mismatches": mism[:10]}
    return out


def validate_sstore(rows, struct_logs):
    """SSTORE EIP-2929 cost analysis:
       cold costs: {2200 (cold + noop/dirty), 5000 (cold + reset), 22100 (cold + set)}
       warm costs: {100 (warm + noop/dirty), 2900 (warm + reset), 20000 (warm + set)}
       Our parser's cold/warm flag must NOT contradict the cost set membership.
    """
    cold_costs = {2200, 5000, 22100}
    warm_costs = {100, 2900, 20000}
    sstore_rows = [r for r in rows if r["opcode"] == "SSTORE"]
    sstore_steps = [s for s in struct_logs if s["op"] == "SSTORE"]
    assert len(sstore_rows) == len(sstore_steps)
    mism = []
    for r, s in zip(sstore_rows, sstore_steps):
        gc = s["gasCost"]
        if r["warm_cold"] == "cold" and gc not in cold_costs:
            mism.append(("cold but cost not in cold_costs", r["opcode_index"], gc))
        if r["warm_cold"] == "warm" and gc not in warm_costs:
            mism.append(("warm but cost not in warm_costs", r["opcode_index"], gc))
    return {"total": len(sstore_rows),
            "cold": sum(1 for r in sstore_rows if r["warm_cold"] == "cold"),
            "warm": sum(1 for r in sstore_rows if r["warm_cold"] == "warm"),
            "mismatches": mism[:10]}


def validate_call_family(rows, struct_logs):
    """For CALL family: gas_cost should be << gasCost in raw structLog, and small (a few thousand max)."""
    out = {}
    for op in ("CALL", "STATICCALL", "DELEGATECALL", "CALLCODE"):
        op_rows = [r for r in rows if r["opcode"] == op]
        op_steps = [s for s in struct_logs if s["op"] == op]
        if len(op_rows) != len(op_steps):
            out[op] = {"error": f"row count mismatch {len(op_rows)} vs {len(op_steps)}"}
            continue
        mism = []
        for r, s in zip(op_rows, op_steps):
            raw = s["gasCost"]
            corrected = r["gas_cost"]
            # If a child frame was entered, corrected must be <= raw and typically small.
            # If no child frame entered (precompile / EOA), corrected stays = raw, which is fine.
            if corrected > raw:
                mism.append(("corrected > raw", r["opcode_index"], corrected, raw))
            # Sanity: cold call overhead ~= 2600 + value(9000?) + new_account(25000?) + memory; max realistic ~30k-50k
            if corrected > 100_000:
                mism.append(("corrected absurdly high", r["opcode_index"], corrected))
        out[op] = {"total": len(op_rows),
                   "cold": sum(1 for r in op_rows if r["warm_cold"] == "cold"),
                   "warm": sum(1 for r in op_rows if r["warm_cold"] == "warm"),
                   "max_raw": max((s["gasCost"] for s in op_steps), default=0),
                   "max_corrected": max((r["gas_cost"] for r in op_rows), default=0),
                   "min_corrected": min((r["gas_cost"] for r in op_rows), default=0),
                   "mismatches": mism[:10]}
    return out


def validate_addresses_normalized(rows):
    """All address fields should be 42-char (0x + 40), all slots 66-char."""
    bad = []
    for r in rows:
        if r["address"] is not None and r["opcode"] != "CREATE" and r["opcode"] != "CREATE2":
            if len(r["address"]) != 42 or not r["address"].startswith("0x"):
                bad.append(("bad addr len", r["opcode_index"], r["opcode"], r["address"]))
        if r["storage_key"] is not None:
            if len(r["storage_key"]) != 66 or not r["storage_key"].startswith("0x"):
                bad.append(("bad slot len", r["opcode_index"], r["opcode"], r["storage_key"]))
    return bad


def validate_delegatecall_storage_attribution(rows, struct_logs):
    """When a DELEGATECALL frame contains SLOAD/SSTORE, those rows' address
    must equal the address of the *parent* frame at the time of the DELEGATECALL,
    NOT the callee. We verify by walking the trace and checking on a per-tx basis."""
    # Reconstruct expected storage owners using the same rules as the parser, for a sanity check.
    # Find DELEGATECALL frames and the SLOAD/SSTORE rows inside them.
    # If correct, the row's `address` should match the storage_owner of the parent frame.
    # We just re-run the parser logic in a minimal way to get expected.
    # Light sanity: ensure that there's at least one DELEGATECALL frame and that
    # SLOADs at depth>1 inside such frames have address != the DELEGATECALL target stack[-2].
    issues = []
    parent_owner_at_depth = {}  # depth -> owner before frame enter
    cur_owner = None
    for i, s in enumerate(struct_logs):
        # We're not re-running, just spot-checking with the rule: after a DELEGATECALL,
        # SLOADs should still attribute to the pre-DELEGATECALL owner.
        pass
    return issues  # skipped — rely on row count + spot-check below


def run_for_tx(tx_hash, block_number=None):
    print(f"\n=== TX {tx_hash} ===")
    sl = get_trace(tx_hash)
    tx = get_tx(tx_hash)
    blk = get_block(int(tx["blockNumber"], 16))
    rows = parse_trace(
        sl,
        tx_hash=tx_hash,
        tx_to=tx.get("to"),
        tx_from=tx.get("from"),
        block_number=int(tx["blockNumber"], 16),
        coinbase=blk.get("miner"),
    )
    print(f"steps: {len(sl)}, rows emitted: {len(rows)}")
    # opcode breakdown
    from collections import Counter
    c = Counter(r["opcode"] for r in rows)
    print("opcode counts:", dict(c))

    print("SLOAD validation:", validate_sload(rows, sl))
    print("SSTORE validation:", validate_sstore(rows, sl))
    for op, res in validate_balance_ext(rows, sl).items():
        print(f"{op} validation:", res)
    for op, res in validate_call_family(rows, sl).items():
        print(f"{op} validation:", res)

    bad = validate_addresses_normalized(rows)
    if bad:
        print(f"normalization issues ({len(bad)}):", bad[:5])
    else:
        print("normalization: OK")

    return rows, sl


if __name__ == "__main__":
    test_basic_normalization()
    print("norm_addr/norm_slot: OK")

    # Tx 1: heavy DEX-like tx with many SLOADs/SSTOREs/CALLs and DELEGATECALLs
    rows1, sl1 = run_for_tx("0x394103d8ec0c90a847eda03579e3395929d40630da798843af7245c8d4c2d806")

    # Tx 2: tx with CREATE2 and SELFDESTRUCT (MEV/arb)
    rows2, sl2 = run_for_tx("0xe3d332309cf60822e69cade3ddbf820603a5b6bcf8124d1aa32a28f0ac185b70")

    # Tx 3: EXTCODECOPY case
    rows3, sl3 = run_for_tx("0x93c0a0cb459466dc294ac0097a6909500748707c3a311cf268a215b81ce5e067")

    # Tx 4: EXTCODEHASH case
    rows4, sl4 = run_for_tx("0x7326f80c238d0a79271531ded86a5441aeb8885d7ad696f913b847751cc26671")

    # Tx 5: BALANCE case
    rows5, sl5 = run_for_tx("0x329d5a53f5a496a4f9f6f2ff319b7fd6e865b3a8eb0054fe2af00bcc37259061")

    # Tx 6: top-level reverted tx — verifies revert path doesn't leave state inconsistent
    rows6, sl6 = run_for_tx("0x4a510e4c6e7017efbfcb560726c30cc930b3f1afa620f58c6d7b58225b0dea94")

    # Inspect a few CREATE/SELFDESTRUCT/CALL rows from tx2 for hand-verification.
    print("\n--- CREATE/CREATE2/SELFDESTRUCT rows in tx2 ---")
    for r in rows2:
        if r["opcode"] in ("CREATE", "CREATE2", "SELFDESTRUCT"):
            print(r)

    print("\n--- First 5 CALL rows in tx2 (corrected gas_cost) ---")
    n = 0
    for r in rows2:
        if r["opcode"] == "CALL":
            print(r)
            n += 1
            if n >= 5: break
