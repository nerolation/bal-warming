---
eip: <to be assigned>
title: Multi-block warming via a rolling warm-access multiset
description: Maintain a rolling 7200-block (~24 h) warm-access multiset derived from Block Access Lists; treat its items as already-accessed at the start of every transaction.
author: <TBD>
discussions-to: <TBD>
status: Draft
type: Standards Track
category: Core
created: 2026-05-03
requires: 2929, 2930, 7928
---

## Abstract

A new chain-state structure, the **warm-access multiset (WAM)**, maps each `(address, slot?)` item to the number of the last 7200 blocks whose Block Access List (BAL, per EIP-7928) contains that item. Items present in the WAM at the start of a block are treated as already-accessed for EIP-2929 pricing in every transaction of that block: account-access opcodes pay 100 gas instead of 2600, storage-access opcodes 100 instead of 2100. The WAM updates incrementally each block (+1 for items in the new BAL, −1 for items in the BAL aging out), so additions and removals are O(|BAL|) per block.

## Motivation

EIP-2929's access list resets at every transaction. The same `(contract, slot)` pairs and contract addresses pay the cold cost thousands of times per day across blocks. Empirical analysis of 2 000 mainnet blocks shows ~10 % per-block gas savings at an 8-block (~96 s) warming horizon, ~15 % at 1024 blocks (~3.4 h), with a per-block-median upper bound of 18.4 %. A 24-hour window (7200 blocks) is well clear of the finality horizon and captures essentially the full recoverable amount.

A naive implementation (store 7200 raw BALs, recompute their union each block) walks ~3 000 items per block of work. The multiset-with-refcounts representation specified here makes membership testing O(1) and per-block update O(|BAL_in| + |BAL_out|), independent of the window size.

## Specification

### Constants

```
WARMING_WINDOW = 7200          # blocks (~24 h at 12 s/slot)
```

### Item type

An item is one of:

- `Account(addr)`: a 20-byte Ethereum address;
- `Slot(addr, key)`: a 20-byte address paired with a 32-byte storage key.

`items(BAL(N))` is the deduplicated set of every address and `(address, slot)` pair in `BAL(N)` (EIP-7928).

### State: the warm-access multiset

Add one piece of chain-derived state:

```
WAM : Item -> u32
```

Items not present have count 0. **An item is *warm* iff `WAM[item] > 0`.**

### Per-block transition

Before transaction execution in block `B`:

```
ADD = items(BAL(B − 1))                       # most recently sealed BAL
DEL = items(BAL(B − 1 − WARMING_WINDOW))      # BAL aging out; empty if B ≤ WARMING_WINDOW

for item in ADD:
    WAM[item] += 1
for item in DEL:
    WAM[item] -= 1
    if WAM[item] == 0:
        delete WAM[item]
```

Order is fixed (`ADD` before `DEL`) so items present in both do not transiently drop to 0. The final `WAM` is order-independent; the fixed order simplifies proofs.

The transition mutates the WAM in place; no per-block snapshot is required. Historical `items(BAL(N))` for `N ≥ B − 1 − WARMING_WINDOW` are needed for `DEL`, which EIP-7928 already provides.

### Commitment

The WAM is committed to by a binary Sparse Merkle Tree (SMT) of depth 256:

```
leaf_key(item) = SHA256(serialize(item))      # 256-bit
leaf_value     = u32 counter (0 if absent)
WAM_ROOT       = SMT root over { leaf_key → leaf_value }
```

SHA-256 is used for both leaf keys and node hashing (matching beacon-chain SSZ Merkleization; precompile `0x02`). `WAM_ROOT` is added to the block header as a new field. The per-block transition updates the SMT incrementally: one leaf update per item in `ADD ∪ DEL`. Order does not affect the root since SMT structure depends only on leaf keys.

Inclusion or non-inclusion proofs consist of 256 sibling hashes (fixed shape, independent of `|WAM|`). A future ZK-friendly hash precompile (e.g., Poseidon) can replace `SHA256` without changing the structure.

### Access-list initialization in transactions

At the start of every transaction in block `B`, the per-tx access list is initialized as today (precompiles, `tx.from`, `tx.to`, coinbase per EIP-3651, EIP-2930 access list, EIP-7702 authority list) **plus** every item with `WAM[item] > 0`.

### Pricing

No new gas constants. For every opcode that consults the access list:

- if the operand is `Account(target)` or `Slot(executing_contract, key)` and the item is warm (in the WAM or already added during this tx), it pays the warm cost (`100` gas for both account and storage);
- otherwise, the cold cost (`2600` for accounts, `2100` for storage). The item is then added to the per-tx access list as in EIP-2929.

### Revert semantics

Intra-tx revert behaves as in EIP-2929: entries added to the per-tx access list inside a reverted sub-call are removed. The WAM only changes at block boundaries and is not affected by transaction revert.

### Genesis and activation

For the first `WARMING_WINDOW` blocks after activation, `DEL` is empty. The WAM grows monotonically until block `activation_block + WARMING_WINDOW`, then enters steady-state.

## Rationale

### Why a refcounted multiset

Multi-block warming is a sliding-window union of BAL items. Three representations:

| Representation | Membership test | Per-block update | Recompute on reorg |
|---|---|---|---|
| Store 7200 raw BALs, recompute union per query | O(7200) lookups | O(1), shift the ring | O(1) |
| Store 7200 raw BALs, materialize union as a set | O(1) | O(union recompute), expensive | O(union recompute) |
| **Refcounted multiset (this EIP)** | **O(1)** | **O(\|BAL_in\| + \|BAL_out\|)** | **O(WARMING_WINDOW · avg \|BAL\|)** |

The multiset wins on both hot paths: membership during tx execution and steady-state per-block update. Reorg cost is identical across representations.

### Why 7200 blocks

Saving rate vs warming window, from a 2 000-block mainnet sample (median per-block percentage of `gasUsed`):

| W | Time back | Median saving | % of cold gas eliminated |
|---:|---:|---:|---:|
| 0 | 0 s | 4.8 % | 27.6 % |
| 1 | 12 s | 7.0 % | 37.2 % |
| 8 | 96 s | 10.1 % | 54.9 % |
| 64 | 12.8 min | 13.2 % | 69.4 % |
| 256 | 51.2 min | 14.4 % | 75.4 % |
| 1024 | 3.4 h | 15.2 % | 80.4 % |
| Asymptote | n/a | 18.4 % | 100 % |

Each doubling past W=1024 adds < 0.3 percentage points. 7 200 (24 h) is the smallest natural cadence that recovers ≥ 95 % of the asymptote while remaining well clear of the finality horizon (~12.8 min for two epochs), so reorgs cannot disturb the bulk of the window.

### Reuse of EIP-7928 BALs

EIP-7928 introduces a canonical Block Access List committed to the block header. This proposal reuses that artifact as the per-block "add" set and (via history lookup) as the "delete" set. Without EIP-7928, this EIP would need its own access-list commitment, which is out of scope.

### Binary SMT (not MPT) and SHA-256 (not Keccak)

A zkEVM prover charges every access opcode with one inclusion or non-inclusion proof against `WAM_ROOT`. For ~3 000 access opcodes per block, per-proof cost is on the critical path. Two independent choices drive efficiency:

- **SMT over MPT.** A binary SMT keyed by `SHA256(item)` gives a uniform 256-level proof shape and natural non-inclusion proofs (a zero leaf on the deterministic path). MPT proofs are variable-depth and need a divergent-sibling step for non-inclusion, producing non-uniform circuits that need recursive verification.
- **SHA-256 over Keccak-256.** Both are available on Ethereum L1, but SHA-256 costs ~25 k constraints per hash in standard R1CS/BN254 arithmetization (5–10 k in lookup-based proof systems), versus ~150 k for Keccak. SHA-256 is also the hash used by beacon-chain SSZ Merkleization. A future EIP introducing a ZK-friendly hash precompile (e.g., Poseidon) can replace `SHA256` without changing the structure.

The WAM is new state, so a ZK-friendly commitment does not break compatibility with the existing MPT/Keccak state trie.

### No new gas constants

Reusing existing warm/cold costs keeps the gas table small and lets gas estimation, fuzzers, and compilers continue to work without modification.

## Backwards Compatibility

Forward-only: every existing transaction pays the same or less gas. No transaction becomes invalid. EIP-2930 access lists remain valid and are pre-warmed (idempotent overlap with the WAM).

**State cost.** From the 2 000-block sample, ~3 000 distinct items per block enter the WAM with heavy overlap across blocks. Empirical WAM size after 7 200 blocks: 5–10 million distinct items, ~300–700 MB at 60 bytes per entry, plus SMT internal nodes. Comparable to the EIP-7928 BAL history nodes already retain.

**Block header.** One new 32-byte field `wam_root`. Validators verify it matches the SMT root after the per-block transition.

**Worst-case state.** Bounded by `WARMING_WINDOW × max_distinct_items_per_block`, which is bounded by the block gas limit. A pathological block of only unique cold storage accesses contributes ~16 000 items; the worst-case WAM is ~115 million items ≈ 7 GB. No realistic mainnet workload approaches this.

## Test Cases

To be added. Reference scenarios:

1. Block `N−1`: SLOAD on `(c, s)`. Cold (2100). `items(BAL(N−1))` now contains `Slot(c, s)`.
2. Block `N`: WAM has `Slot(c, s) → 1`. SLOAD on `(c, s)`. **Warm (100)**.
3. Blocks `N+1 … N+7200`: SLOAD on `(c, s)` once per block. WAM count increments to ≤ 7201 then decrements as the oldest contributing block ages out; the item stays warm.
4. Block `N+7201`: assume `(c, s)` was not touched after `N`. The transition adds `items(BAL(N+7200))` (no `(c, s)`) and removes `items(BAL(N))` (contains `(c, s)`). `WAM[Slot(c, s)]` drops to 0 and is deleted. SLOAD on `(c, s)`: **Cold (2100)**.

## Reference Implementation

Non-consensus reference implementation (parser, per-block extraction, savings analysis): https://github.com/nerolation/bal-warming.

## Security Considerations

### State growth and DoS

Inflating the WAM costs the attacker ≥ 2100 gas per item, the same as today. The 2000–2500 gas saving accrues only to later legitimate accessors of that exact item, not to the attacker. No economic incentive to inflate.

### Memory pressure

At 300–700 MB the WAM fits in memory alongside other node caches. A hash map of items to small counters does not require cold-tier storage.

### Reorg behavior

WAM transitions are deterministic functions of `BAL(N−1)` and `BAL(N−1−WARMING_WINDOW)`. Re-executing the new canonical chain rebuilds the correct WAM. No explicit refund machinery is required. Finality (~12.8 min for two epochs) bounds reorg depth to a tiny fraction of the warming window.

### Light/stateless clients

The WAM is derived deterministically from EIP-7928 BAL history. Light clients can either compute `WAM_ROOT` from BAL history or verify (non-)inclusion proofs directly against the `wam_root` in the block header.

## Copyright

Copyright and related rights waived via [CC0](../LICENSE).
