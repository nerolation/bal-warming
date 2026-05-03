"""
Tests for access_savings_parser.py.

Builds tiny synthetic block parquet files with hand-crafted access patterns
and verifies the window-savings math is correct.
"""
import os
import shutil
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import access_savings_parser as asp


def make_row(block, tx_index, opcode_index, opcode, address, storage_key, warm_cold):
    return {
        "block_number": block,
        "tx_hash": "0x" + ("aa" * 32),
        "tx_index": tx_index,
        "opcode": opcode,
        "opcode_index": opcode_index,
        "pc": 0,
        "depth": 1,
        "gas_cost": 0,
        "warm_cold": warm_cold,
        "address": address,
        "storage_key": storage_key,
        "frame_kind": "ROOT",
    }


def write_block(tmp, block, rows):
    df = pd.DataFrame(rows)
    df.to_parquet(os.path.join(tmp, f"block_{block}.parquet"), index=False)


def test_intra_block_warming():
    """Two cold SLOADs of the same slot in different txs of the same block.
    With W=0, the second SLOAD should be 'savable' (intra-block warming).
    Expected: 1 hit at W=0; saved = 2000."""
    tmp = tempfile.mkdtemp()
    try:
        slot = "0x" + "0" * 64
        addr = "0x" + "11" * 20
        write_block(tmp, 100, [
            make_row(100, 0, 5,  "SLOAD", addr, slot, "cold"),  # tx0 first access
            make_row(100, 1, 10, "SLOAD", addr, slot, "cold"),  # tx1 first access — same slot
        ])
        df = asp.load_blocks(tmp)
        df = asp.compute_block_gaps(df)
        gaps = df.sort_values(["block_number","tx_index","opcode_index"]).block_gap.tolist()
        assert gaps[0] >= 2**32 - 1, gaps  # never seen before
        assert gaps[1] == 0, gaps         # same block

        s = asp.summarize(df, [0])
        row = s.iloc[0]
        assert row.cold_hits == 1, row.to_dict()
        assert row.saved_gas == 2000, row.to_dict()
        print("test_intra_block_warming: OK")
    finally:
        shutil.rmtree(tmp)


def test_inter_block_window():
    """Same slot accessed cold in three consecutive blocks (1 access per block, no intra-block).
    W=0 → only block 1 saved (intra-block isn't relevant; first access always cold)?
    Wait — block 1 has no prior access so no save. Block 2 has prior in block 1: gap=1.
    Block 3 has prior in block 2: gap=1. So at W=0: 0 saves; W=1: 2 saves; W=∞: 2 saves."""
    tmp = tempfile.mkdtemp()
    try:
        slot = "0x" + "0" * 64
        addr = "0x" + "22" * 20
        write_block(tmp, 200, [make_row(200, 0, 5, "SLOAD", addr, slot, "cold")])
        write_block(tmp, 201, [make_row(201, 0, 5, "SLOAD", addr, slot, "cold")])
        write_block(tmp, 202, [make_row(202, 0, 5, "SLOAD", addr, slot, "cold")])
        df = asp.compute_block_gaps(asp.load_blocks(tmp))
        gaps = df.sort_values(["block_number","tx_index","opcode_index"]).block_gap.tolist()
        assert gaps[0] >= 2**32 - 1, gaps
        assert gaps[1] == 1, gaps
        assert gaps[2] == 1, gaps

        s = asp.summarize(df, [0, 1, 2, 1000])
        d = {r.window_blocks: r for _, r in s.iterrows()}
        assert d[0].cold_hits == 0
        assert d[1].cold_hits == 2 and d[1].saved_gas == 4000
        assert d[2].cold_hits == 2 and d[2].saved_gas == 4000
        assert d[1000].cold_hits == 2 and d[1000].saved_gas == 4000
        print("test_inter_block_window: OK")
    finally:
        shutil.rmtree(tmp)


def test_window_boundary_exactly():
    """Same slot accessed in block 100, then block 105. Gap=5.
    W=4 → 0 hits; W=5 → 1 hit."""
    tmp = tempfile.mkdtemp()
    try:
        slot = "0x" + "1" * 64
        addr = "0x" + "33" * 20
        write_block(tmp, 100, [make_row(100, 0, 0, "SLOAD", addr, slot, "cold")])
        # missing blocks 101-104 -- the script will warn but math is still right since
        # block_gap is measured by block_number difference, not file presence
        write_block(tmp, 105, [make_row(105, 0, 0, "SLOAD", addr, slot, "cold")])
        df = asp.compute_block_gaps(asp.load_blocks(tmp))
        gaps = df.sort_values(["block_number","tx_index","opcode_index"]).block_gap.tolist()
        assert gaps[0] >= 2**32 - 1, gaps
        assert gaps[1] == 5, gaps

        s = asp.summarize(df, [4, 5])
        d = {r.window_blocks: r for _, r in s.iterrows()}
        assert d[4].cold_hits == 0
        assert d[5].cold_hits == 1 and d[5].saved_gas == 2000
        print("test_window_boundary_exactly: OK")
    finally:
        shutil.rmtree(tmp)


def test_per_opcode_savings_constants():
    """Verify the SAVING dict differentials are applied correctly per opcode."""
    tmp = tempfile.mkdtemp()
    try:
        # Block 1 establishes; block 2 re-accesses with various opcodes.
        addr_a = "0x" + "44" * 20
        addr_b = "0x" + "55" * 20
        slot = "0x" + "0" * 64
        write_block(tmp, 300, [
            make_row(300, 0, 0, "SLOAD",        addr_a, slot, "cold"),
            make_row(300, 1, 0, "BALANCE",      addr_b, None, "cold"),
            make_row(300, 2, 0, "SELFDESTRUCT", addr_a, None, "cold"),
        ])
        write_block(tmp, 301, [
            make_row(301, 0, 0, "SLOAD",        addr_a, slot, "cold"),       # gap=1
            make_row(301, 1, 0, "BALANCE",      addr_b, None, "cold"),       # gap=1 (same address-only key)
            make_row(301, 2, 0, "SELFDESTRUCT", addr_a, None, "cold"),       # gap=0 (same block as BALANCE addr_b? no — addr_a) -- wait this hits addr_a, slot=None which differs from SLOAD's (addr_a, slot=full).
        ])
        df = asp.compute_block_gaps(asp.load_blocks(tmp))
        s = asp.summarize(df, [1])
        row = s.iloc[0]
        # SLOAD(addr_a, slot)         hit @W=1 → 2000
        # BALANCE(addr_b, None)       hit @W=1 → 2500
        # SELFDESTRUCT(addr_a, None)  hit @W=1 → 2600 (was set at block 300 too)
        assert row.cold_hits == 3
        assert row.saved_gas == 2000 + 2500 + 2600
        print("test_per_opcode_savings_constants: OK")
    finally:
        shutil.rmtree(tmp)


def test_intra_block_distinguishes_keys():
    """Two cold SLOADs in same block but DIFFERENT slots — neither saved at W=0."""
    tmp = tempfile.mkdtemp()
    try:
        addr = "0x" + "66" * 20
        write_block(tmp, 400, [
            make_row(400, 0, 0, "SLOAD", addr, "0x" + "0" * 63 + "1", "cold"),
            make_row(400, 1, 0, "SLOAD", addr, "0x" + "0" * 63 + "2", "cold"),
        ])
        df = asp.compute_block_gaps(asp.load_blocks(tmp))
        s = asp.summarize(df, [0, 100])
        d = {r.window_blocks: r for _, r in s.iterrows()}
        assert d[0].cold_hits == 0
        assert d[100].cold_hits == 0
        print("test_intra_block_distinguishes_keys: OK")
    finally:
        shutil.rmtree(tmp)


def test_warm_rows_still_extend_last_seen():
    """A WARM access still establishes the key as seen for future blocks.
    Block 1: cold SLOAD K. Block 1, same tx: warm SLOAD K (already in tx access list).
    Block 2: cold SLOAD K. Should hit at W>=1 because K was seen in block 1 (warm row counted)."""
    tmp = tempfile.mkdtemp()
    try:
        addr = "0x" + "77" * 20
        slot = "0x" + "0" * 64
        write_block(tmp, 500, [
            make_row(500, 0, 0, "SLOAD", addr, slot, "cold"),
            make_row(500, 0, 1, "SLOAD", addr, slot, "warm"),  # repeat in same tx
        ])
        write_block(tmp, 501, [
            make_row(501, 0, 0, "SLOAD", addr, slot, "cold"),  # would be saved at W=1
        ])
        df = asp.compute_block_gaps(asp.load_blocks(tmp))
        s = asp.summarize(df, [0, 1])
        d = {r.window_blocks: r for _, r in s.iterrows()}
        # cold rows: 2 (block 500 idx 0 and block 501 idx 0). The warm row isn't cold.
        # block 500 cold: gap=inf → no save
        # block 501 cold: gap=1 → save at W=1
        assert d[0].cold_hits == 0
        assert d[1].cold_hits == 1 and d[1].saved_gas == 2000
        print("test_warm_rows_still_extend_last_seen: OK")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_intra_block_warming()
    test_inter_block_window()
    test_window_boundary_exactly()
    test_per_opcode_savings_constants()
    test_intra_block_distinguishes_keys()
    test_warm_rows_still_extend_last_seen()
    print("\nALL TESTS PASSED")
