"""
Per-opcode access parser for EIP-2929 cold/warm analysis.

Builds rows: (block_number, tx_hash, opcode, opcode_index, pc, depth,
              gas_cost, warm_cold, address, storage_key, frame_kind)

Fixes vs the reference parser in nerolation/blk-lvl-warming-analysis:
  - Normalizes addresses/slots to canonical hex (40-char addr, 64-char slot)
  - For CALL/STATICCALL/DELEGATECALL/CALLCODE, gas_cost is the actual call
    overhead (cost paid by the calling frame to enter the call), not the
    inflated structLog gasCost which includes gas forwarded to the callee.
  - DELEGATECALL/CALLCODE preserve caller's storage context for SLOAD/SSTORE.
  - CREATE/CREATE2: tracks the new contract's address (read on return) and
    back-fills storage rows produced inside the constructor frame.
  - SELFDESTRUCT captures the beneficiary address.
  - Reverted/exceptionally-halted frames have their per-frame access-list
    additions undone, matching EIP-2929 semantics.
  - Pre-warms the per-tx access list with tx.from, tx.to, coinbase, and
    EIP-2929 precompiles 0x01..0x0a (extend if needed for new precompiles).
"""

from typing import List, Dict, Any, Optional, Tuple, Set


PRECOMPILES = {f"0x{i:040x}" for i in range(1, 0x0b)}  # 0x01..0x0a


def norm_addr(s: str) -> str:
    """Canonicalize an address: lowercase, 0x-prefixed, zero-padded to 40 hex chars."""
    if s is None:
        return None
    s = s.lower()
    if s.startswith("0x"):
        s = s[2:]
    s = s.zfill(40)[-40:]  # take the low 20 bytes
    return "0x" + s


def norm_slot(s: str) -> str:
    """Canonicalize a 256-bit slot key: lowercase, 0x-prefixed, zero-padded to 64 hex chars."""
    if s is None:
        return None
    s = s.lower()
    if s.startswith("0x"):
        s = s[2:]
    s = s.zfill(64)
    return "0x" + s


def parse_trace(struct_logs: List[Dict[str, Any]],
                tx_hash: str,
                tx_to: Optional[str],
                tx_from: Optional[str],
                block_number: int,
                coinbase: Optional[str] = None,
                eip3651_warm_coinbase: bool = True,
                access_list: Optional[List[Dict[str, Any]]] = None,
                authorization_list: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Parse a single transaction's debug_traceTransaction structLogs.

    access_list: optional EIP-2930 access list (list of {"address", "storageKeys"})
                 — pre-warms those (address, slot) pairs as required by EIP-2930.
    authorization_list: optional EIP-7702 list (list of {"address", ...}) —
                 pre-warms the authority addresses as required by EIP-7702.
    """
    if not struct_logs:
        return []

    rows: List[Dict[str, Any]] = []

    # EIP-2929 access list, per-tx, with per-frame deltas so we can revert.
    # Each frame_stack entry tracks: (kind, depth, addr_delta, slot_delta,
    #                                 storage_owner, pending_create_row_idx,
    #                                 constructor_row_idxs)
    # storage_owner = address whose storage SLOAD/SSTORE in this frame mutate.
    # pending_create_row_idx = index in `rows` of the CREATE/CREATE2 row that
    # spawned this frame (None for non-CREATE frames). constructor_row_idxs =
    # row indices for SLOAD/SSTORE that need their address back-filled when
    # we learn the created contract's actual address.

    # Initial root frame
    root_to = norm_addr(tx_to) if tx_to else None  # may be None for contract-creation tx
    root_from = norm_addr(tx_from) if tx_from else None
    root_coinbase = norm_addr(coinbase) if coinbase else None

    # Pre-warm access list per EIP-2929 (and EIP-3651 for coinbase).
    warm_addrs: Set[str] = set(PRECOMPILES)
    if root_to is not None:
        warm_addrs.add(root_to)
    if root_from is not None:
        warm_addrs.add(root_from)
    if eip3651_warm_coinbase and root_coinbase is not None:
        warm_addrs.add(root_coinbase)
    warm_slots: Set[Tuple[str, str]] = set()

    # EIP-2930 access list: pre-warm addresses and (address, slot) pairs.
    if access_list:
        for entry in access_list:
            addr = norm_addr(entry.get("address"))
            if addr:
                warm_addrs.add(addr)
            for sk in entry.get("storageKeys") or []:
                warm_slots.add((addr, norm_slot(sk)))
    # EIP-7702 authorization list: pre-warm authority addresses.
    if authorization_list:
        for entry in authorization_list:
            # The "address" field is the delegated-to contract; the authority is
            # recovered from the signature. Most JSON encodings include both;
            # prefer "authority" if present, fall back to "address".
            for k in ("authority", "address"):
                v = entry.get(k)
                if v:
                    warm_addrs.add(norm_addr(v))

    PLACEHOLDER_CREATE = "__pending_create__"

    # Root frame: storage owner is tx.to (for normal txs) or unknown for
    # creation txs; for creation txs, the trace's first frame is itself the
    # constructor of the new contract, so we'll patch its rows after we see
    # the receipt's contract_address (caller can pass tx_to=receipt.contractAddress).
    root_owner = root_to if root_to is not None else PLACEHOLDER_CREATE
    frame_stack: List[Dict[str, Any]] = [{
        "kind": "ROOT",
        "depth": 1,
        "addr_delta": set(),
        "slot_delta": set(),
        "storage_owner": root_owner,
        "create_row_idx": None,
        "constructor_row_idxs": [],
    }]

    # Pending CALL: when a CALL/STATICCALL/DELEGATECALL/CALLCODE is observed,
    # we wait for the next step to determine if a child frame was actually
    # entered (depth went up) or not (precompile / EOA / failed pre-checks
    # like static-write or insufficient balance), and to compute the exact
    # call overhead. Two cases:
    #   - child frame entered (next.depth == call.depth + 1):
    #       corrected = call.gasCost - next.gas
    #     (gasCost includes the forwarded gas; next.gas is the callee's
    #      starting gas = forwarded.)
    #   - no child frame (next.depth == call.depth):
    #       corrected = call.gas - next.gas
    #     (the actual gas drop in the parent frame is the true cost; geth's
    #      gasCost in this case still accounts for a phantom forward that
    #      gets immediately credited back, so it's misleading.)
    pending_call: Optional[Dict[str, Any]] = None  # keys: row_idx, op, gasCost, gas, depth, target

    n = len(struct_logs)
    for i in range(n):
        log = struct_logs[i]
        op = log["op"]
        depth = log["depth"]
        gas_cost = log["gasCost"]
        gas = log["gas"]
        stack = log.get("stack") or []

        # ---- Resolve any pending state from the previous step ----

        # Resolve frame transitions: if depth is < top frame depth, the top
        # frame ended. If depth is > top frame depth, a new frame began
        # (driven by the previous CALL/STATICCALL/DELEGATECALL/CALLCODE/CREATE/CREATE2).
        # Note: depth changes by +/-1 between consecutive structLog steps.
        prev = struct_logs[i - 1] if i > 0 else None

        # Frame ENTER: depth increased
        if prev is not None and depth > prev["depth"]:
            pop = prev["op"]
            if pop in ("CALL", "STATICCALL"):
                target = norm_addr(prev["stack"][-2])
                new_owner = target
                kind = pop
                create_row = None
            elif pop in ("DELEGATECALL", "CALLCODE"):
                # Storage context unchanged; inherit parent's storage_owner.
                new_owner = frame_stack[-1]["storage_owner"]
                kind = pop
                create_row = None
            elif pop in ("CREATE", "CREATE2"):
                new_owner = PLACEHOLDER_CREATE
                kind = pop
                # The CREATE/CREATE2 row was just appended (pending_create_row).
                create_row = pending_call["create_row_idx"] if pending_call and pending_call.get("create_row_idx") is not None else None
                # Actually we won't reuse pending_call for creates; we set create_row directly below.
            else:
                # Shouldn't happen (only those ops can spawn frames in normal EVM).
                new_owner = frame_stack[-1]["storage_owner"]
                kind = pop
                create_row = None

            # If this entered frame is a CREATE/CREATE2, find the row we appended.
            if pop in ("CREATE", "CREATE2"):
                # The most recently appended row should be the CREATE/CREATE2 row.
                create_row = len(rows) - 1

            frame_stack.append({
                "kind": kind,
                "depth": depth,
                "addr_delta": set(),
                "slot_delta": set(),
                "storage_owner": new_owner,
                "create_row_idx": create_row,
                "constructor_row_idxs": [],
            })

            # Resolve pending_call gas cost when child frame entered.
            # CALL family: corrected = gasCost - callee_starting_gas (= log.gas here).
            # CREATE/CREATE2: gasCost in geth's structLog already excludes the gas
            # forwarded to the constructor (it's just base 32000 + memory + init-code-word
            # cost). So leave it untouched.
            if pending_call is not None and pending_call["op"] in ("CALL", "STATICCALL", "DELEGATECALL", "CALLCODE"):
                rows[pending_call["row_idx"]]["gas_cost"] = pending_call["gasCost"] - gas
                pending_call = None
            elif pending_call is not None and pending_call["op"] in ("CREATE", "CREATE2"):
                # No correction needed; pending_call already had gas_cost set to gasCost.
                pending_call = None

        # Frame EXIT: depth decreased
        if prev is not None and depth < prev["depth"]:
            # The frame that was at prev["depth"] ended on prev["op"].
            # Determine success vs revert based on prev op.
            terminating_op = prev["op"]
            success = terminating_op in ("RETURN", "STOP", "SELFDESTRUCT")
            # For exceptional halts (out-of-gas, INVALID, stack errors, static-write
            # violation), the terminating step is whatever opcode was executing —
            # it's NOT one of RETURN/STOP/REVERT/SELFDESTRUCT. We treat those as revert.
            # REVERT explicitly: success = False.
            ended = frame_stack.pop()
            if success:
                # Merge deltas into the parent frame so that on parent's revert later,
                # they can still be undone correctly.
                parent = frame_stack[-1]
                parent["addr_delta"].update(ended["addr_delta"])
                parent["slot_delta"].update(ended["slot_delta"])
                # If the ended frame is a CREATE/CREATE2, read the new contract
                # address from current stack[-1] and patch the create row + any
                # constructor SLOAD/SSTORE rows.
                if ended["kind"] in ("CREATE", "CREATE2"):
                    new_addr = norm_addr(stack[-1]) if stack else norm_addr("0x0")
                    if ended["create_row_idx"] is not None:
                        rows[ended["create_row_idx"]]["address"] = new_addr
                    for rix in ended["constructor_row_idxs"]:
                        rows[rix]["address"] = new_addr
                    # EIP-2929: a successfully created account becomes warm.
                    warm_addrs.add(new_addr)
                    parent["addr_delta"].add(new_addr)
            else:
                # Revert: undo this frame's access-list additions.
                for a in ended["addr_delta"]:
                    warm_addrs.discard(a)
                for s in ended["slot_delta"]:
                    warm_slots.discard(s)
                if ended["kind"] in ("CREATE", "CREATE2"):
                    # Failed create: address is 0x0 on stack[-1]. Patch rows.
                    if ended["create_row_idx"] is not None:
                        rows[ended["create_row_idx"]]["address"] = norm_addr("0x0")
                    for rix in ended["constructor_row_idxs"]:
                        rows[rix]["address"] = norm_addr("0x0")

        # If we had a pending_call but depth did NOT go up, the call did not
        # spawn a child frame (precompile, EOA, value-only transfer, or
        # pre-execution failure like static-write violation / insufficient
        # balance / cold + insufficient stipend). geth's structLog gasCost in
        # this case is misleading because it accounts for a phantom forward
        # that gets immediately credited back. Use the actual gas drop instead.
        if pending_call is not None and prev is not None and depth <= prev["depth"]:
            if pending_call["op"] in ("CALL", "STATICCALL", "DELEGATECALL", "CALLCODE"):
                rows[pending_call["row_idx"]]["gas_cost"] = pending_call["gas"] - gas
            # CREATE/CREATE2 with no constructor frame (empty init code):
            # gasCost is already the correct base cost; nothing to do.
            pending_call = None

        # ---- Now process this opcode and emit a row if interesting ----

        cur_frame = frame_stack[-1]
        out_addr = None
        out_slot = None
        warm_flag: Optional[bool] = None

        if op in ("SLOAD", "SSTORE"):
            slot = norm_slot(stack[-1])
            owner = cur_frame["storage_owner"]
            out_addr = owner
            out_slot = slot
            key = (owner, slot)
            warm_flag = key in warm_slots
            if not warm_flag:
                warm_slots.add(key)
                cur_frame["slot_delta"].add(key)
            # The contract whose storage we touch is also implicitly warm in
            # most analyses, but EIP-2929 tracks address vs slot accesses
            # separately; SLOAD/SSTORE do NOT add the contract address to the
            # account access-list (it's already implicitly warmed if we got
            # here via CALL or it's the tx.to). Don't double-warm here.

        elif op in ("CALL", "STATICCALL", "DELEGATECALL", "CALLCODE"):
            target = norm_addr(stack[-2])
            out_addr = target
            warm_flag = target in warm_addrs
            if not warm_flag:
                warm_addrs.add(target)
                cur_frame["addr_delta"].add(target)
            # We will fix gas_cost on next step (when we see the callee's gas
            # or the parent's continuation gas).
            pending_call = {"row_idx": len(rows), "op": op, "gasCost": gas_cost, "gas": gas, "depth": depth, "target": target}

        elif op in ("BALANCE", "EXTCODESIZE", "EXTCODECOPY", "EXTCODEHASH"):
            target = norm_addr(stack[-1])
            out_addr = target
            warm_flag = target in warm_addrs
            if not warm_flag:
                warm_addrs.add(target)
                cur_frame["addr_delta"].add(target)

        elif op == "SELFDESTRUCT":
            beneficiary = norm_addr(stack[-1])
            out_addr = beneficiary
            warm_flag = beneficiary in warm_addrs
            if not warm_flag:
                warm_addrs.add(beneficiary)
                cur_frame["addr_delta"].add(beneficiary)

        elif op in ("CREATE", "CREATE2"):
            # The new address is unknown until the constructor returns. Emit a
            # row with placeholder address; patched on frame exit. Also queue
            # this as a pending_call to fix gas_cost.
            out_addr = None  # patched later
            warm_flag = None  # not an EIP-2929 cold/warm question for the create itself
            pending_call = {"row_idx": len(rows), "op": op, "gasCost": gas_cost, "gas": gas, "depth": depth, "target": None, "create_row_idx": len(rows)}

        else:
            continue  # not an interesting opcode for this analysis

        rows.append({
            "block_number": block_number,
            "tx_hash": tx_hash,
            "opcode": op,
            "opcode_index": i,
            "pc": log["pc"],
            "depth": depth,
            "gas_cost": gas_cost,  # may be overwritten on next step for CALL/CREATE
            "warm_cold": ("warm" if warm_flag else "cold") if warm_flag is not None else "n/a",
            "address": out_addr,
            "storage_key": out_slot,
            "frame_kind": cur_frame["kind"],
        })

        # If we're inside a CREATE/CREATE2 frame and this row is SLOAD/SSTORE,
        # remember to patch its address when we learn the new contract addr.
        if op in ("SLOAD", "SSTORE") and cur_frame["storage_owner"] == PLACEHOLDER_CREATE:
            cur_frame["constructor_row_idxs"].append(len(rows) - 1)

    return rows
