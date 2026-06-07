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

Maintain a single chain-state structure, the **warm-access multiset (WAM)**, mapping each `(address, slot?)` item to the number of the last 7200 blocks whose Block Access List (BAL, per EIP-7928) contains that item. Items present in the WAM at the start of a block are treated as already-accessed for EIP-2929 pricing inside every transaction of that block: account-access opcodes pay 100 gas instead of 2600, storage-access opcodes pay 100 instead of 2100. The WAM is updated incrementally each block (+1 for items in the new BAL, −1 for items in the BAL aging out of the window), so additions and removals are O(|BAL|) per block.

## Motivation

EIP-2929's access list resets at every transaction. The same `(contract, slot)` pairs and contract addresses pay the cold cost thousands of times per day across blocks. Empirical analysis of 2 000 mainnet blocks shows ~10 % per-block gas savings already at a 8-block (~96 s) warming horizon, ~15 % at 1024 blocks (~3.4 h), and a per-block-median upper bound of 18.4 %. A 24-hour window (7200 blocks) is well clear of the finality horizon and captures essentially the full recoverable amount.

A naive implementation (store 7200 raw BALs, recompute their union each block) is operationally expensive: every block touches ~3 000 items and the union recomputation has to walk all of them. The multiset-with-refcounts representation specified here makes membership testing O(1) and per-block update O(|BAL_in| + |BAL_out|), independent of the window size.

## Specification

### Constants

```
WARMING_WINDOW = 7200          # blocks (~24 h at 12 s/slot)
```

### Item type

An item is one of:

- `Account(addr)` — a 20-byte Ethereum address;
- `Slot(addr, key)` — a 20-byte address paired with a 32-byte storage key.

The set of items contributed by block `N` is `items(BAL(N))`, defined as the deduplicated union of every address and `(address, slot)` pair appearing in `BAL(N)` (EIP-7928).

### State: the warm-access multiset

A single piece of chain-derived state is added:

```
WAM : Item -> u32
```

Items not present in the map are treated as having count 0.

**An item is *warm* iff `WAM[item] > 0`.**

### Per-block transition

Before transaction execution in block `B`:

```
ADD = items(BAL(B − 1))                       # the most recently sealed BAL
DEL = items(BAL(B − 1 − WARMING_WINDOW))      # the BAL aging out; empty if B ≤ WARMING_WINDOW

for item in ADD:
    WAM[item] += 1
for item in DEL:
    WAM[item] -= 1
    if WAM[item] == 0:
        delete WAM[item]
```

The order is fixed (`ADD` before `DEL`) so that items present in both — i.e., items also touched by the just-aged-out block and by the newest one — do not transiently drop to 0 between the two operations. The final `WAM` is the same regardless of order, but the transient invariant simplifies proofs.

Each block transition mutates the WAM in place; nodes do not need to store any per-block snapshot of the WAM. They do need access to historical `items(BAL(N))` for `N ≥ B − 1 − WARMING_WINDOW` to perform the `DEL` step; EIP-7928 already provides this.

### Commitment

The WAM is committed to by a binary Sparse Merkle Tree (SMT) of depth 256:

```
leaf_key(item) = SHA256(serialize(item))      # 256-bit
leaf_value     = u32 counter (0 if absent)
WAM_ROOT       = SMT root over { leaf_key → leaf_value }
```

SHA-256 is used for both leaf keys and node hashing (the same hash already used by Ethereum's beacon-chain SSZ Merkleization, and the precompile at address `0x02`). `WAM_ROOT` is included in the block header as a new field. The per-block transition above updates the SMT incrementally: one leaf update per item in `ADD ∪ DEL`, yielding the new root. Order does not affect the final root because SMT structure is determined by leaf keys, not insertion order.

An inclusion proof (item is warm, with its counter) or non-inclusion proof (item is absent) consists of 256 sibling hashes — a fixed shape independent of `|WAM|`. If a future EIP introduces a ZK-friendly hash precompile (e.g., Poseidon), this EIP can be upgraded by swapping `SHA256` without changing the tree structure.

### Access-list initialization in transactions

At the start of every transaction in block `B`, the per-tx access list is initialized as today (precompiles, `tx.from`, `tx.to`, coinbase per EIP-3651, EIP-2930 access list, EIP-7702 authority list) **plus** every item with `WAM[item] > 0`.

### Pricing

No new gas constants. For every opcode that consults the access list:

- if the operand is `Account(target)` or `Slot(executing_contract, key)` and the item is warm (either in the WAM or already added during this tx), it pays the warm cost (`100` gas for both account and storage);
- otherwise, the cold cost (`2600` for accounts, `2100` for storage). The item is then added to the per-tx access list as in EIP-2929.

### Revert semantics

Intra-tx revert behaves as in EIP-2929: entries added to the per-tx access list inside a reverted sub-call are removed. The WAM is not modified by transaction execution and is therefore unaffected by transaction revert. The WAM only changes at block boundaries via the transition above.

### Genesis and activation

For the first `WARMING_WINDOW` blocks after activation, `DEL` is empty: there is no aged-out BAL yet. The WAM grows monotonically until block `activation_block + WARMING_WINDOW`, after which steady-state churn begins.

## Rationale

### Why a refcounted multiset

The semantic content of multi-block warming is a sliding-window union of BAL items. The two natural representations are:

| Representation | Membership test | Per-block update | Recompute on reorg |
|---|---|---|---|
| Store 7200 raw BALs, recompute union per query | O(7200) lookups | O(1) — just shift the ring | O(1) |
| Store 7200 raw BALs, materialize union as a set | O(1) | O(union recompute) — expensive | O(union recompute) |
| **Refcounted multiset (this EIP)** | **O(1)** | **O(\|BAL_in\| + \|BAL_out\|)** | **O(WARMING_WINDOW · avg \|BAL\|)** |

The multiset wins on both hot paths (membership during tx execution, and steady-state per-block update). Reorg recomputation is the same cost as full reconstruction in any representation.

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
| Asymptote | – | 18.4 % | 100 % |

Each doubling past W=1024 adds < 0.3 percentage points. 7 200 (24 h) is the smallest natural cadence that recovers ≥ 95 % of the asymptote while remaining well clear of the finality horizon (~12.8 min for two epochs), so reorgs cannot disturb the bulk of the window.

### Reuse of EIP-7928 BALs

EIP-7928 introduces a canonical Block Access List committed to the block header. This proposal reuses that artifact as the per-block "add" set and (via history lookup) as the "delete" set. Without EIP-7928, multi-block warming requires its own access-list commitment mechanism, which is out of scope here.

### Binary SMT (not MPT) and SHA-256 (not Keccak)

A zkEVM prover charges every access opcode with one inclusion or non-inclusion proof against `WAM_ROOT`. For ~3 000 access opcodes per block, the per-proof cost is on the critical path. Two independent choices drive efficiency:

- **SMT over MPT.** A binary SMT keyed by `SHA256(item)` gives a uniform 256-level proof shape and natural non-inclusion proofs (a zero leaf on the deterministic path). MPT proofs are variable-depth and require a divergent-sibling step for non-inclusion, producing non-uniform circuit shapes that need recursive verification — much more expensive in practice even when the raw constraint count looks similar.
- **SHA-256 over Keccak-256.** Both are available on Ethereum L1, but SHA-256 costs ~25 k constraints per hash in standard R1CS/BN254 arithmetization (and 5–10 k in lookup-based proof systems), versus ~150 k for Keccak. SHA-256 is also the hash used by beacon-chain SSZ Merkleization, so the choice fits an existing Ethereum precedent. A future EIP that introduces a ZK-friendly hash precompile (e.g., Poseidon) can replace `SHA256` without changing the tree structure.

The WAM is a new piece of state, so adopting a ZK-friendly commitment for it does not break backwards compatibility with the existing MPT/Keccak state trie.

### No new gas constants

Reusing existing warm/cold costs keeps the gas table small and lets gas estimation, fuzzers, and compilers continue to work without modification.

## Backwards Compatibility

Forward-only: every existing transaction pays the same or less gas, never more. No transaction becomes invalid. EIP-2930 transaction access lists remain valid and are still pre-warmed (idempotent overlap with the WAM).

**State cost.** From the 2 000-block sample, ~3 000 distinct items per block enter the WAM, with substantial overlap across blocks. Empirical multiset size after a 7 200-block window: estimated 5 – 10 million distinct items. At 60 bytes per entry (20-byte address + optional 32-byte slot + 4-byte counter, packed) the WAM occupies roughly 300 – 700 MB of node state, plus the SMT internal nodes (~32 bytes per occupied path level, dominated by the leaf count). This is comparable to the EIP-7928 BAL history that nodes must already retain.

**Block header.** A single new 32-byte field `wam_root` is added to the block header. Validators verify it matches the WAM SMT root after applying the per-block transition.

**Worst-case state.** Bounded by `WARMING_WINDOW × max_distinct_items_per_block`, which is in turn bounded by the block gas limit. A pathological block of nothing but unique cold storage accesses contributes ~16 000 items (at 21 000 gas per item ceiling); the worst-case WAM is therefore ~115 million items ≈ 7 GB. This is an upper bound that no realistic mainnet workload approaches.

## Test Cases

To be added. Reference scenarios:

1. Block `N−1`: SLOAD on `(c, s)`. Cold (2100). `items(BAL(N−1))` now contains `Slot(c, s)`.
2. Block `N`: WAM has `Slot(c, s) → 1`. SLOAD on `(c, s)`. **Warm (100)**.
3. Blocks `N+1 … N+7200`: SLOAD on `(c, s)` once per block. Each block: WAM count for `Slot(c, s)` is incremented to ≤ 7201 then decremented as the oldest contributing block ages out; the item remains warm throughout.
4. Block `N+7201`: assume `(c, s)` was *not* touched in any block after `N`. The transition adds `items(BAL(N+7200))` (which doesn't include `(c, s)`) and removes `items(BAL(N))` (which does). `WAM[Slot(c, s)]` drops to 0 and is deleted. SLOAD on `(c, s)` in block `N+7201`: **Cold (2100)**.

## Reference Implementation

A non-consensus reference implementation (parser, per-block extraction, savings analysis) is available at https://github.com/nerolation/bal-warming.

## Security Considerations

### State growth and DoS

An attacker who tries to inflate the WAM with junk items still has to pay the EIP-2929 cold-access cost (≥ 2100 gas) for each new item, exactly the cost they would pay today. The amplification potential is bounded: an inflated item provides a 2000–2500 gas saving only to *later* legitimate accessors of that exact same item. The attacker cannot redirect that saving to themselves. There is no economic incentive to inflate.

### Memory pressure

At 300–700 MB the WAM is comparable to other long-lived caches that nodes already maintain. The data structure is simple enough (hash map of items to small counters) that practical implementations can hold it in memory; cold-tier storage is not required.

### Reorg behavior

If a block within the 7200-window is reorg'd, the WAM along the reorg'd chain diverges from the WAM along the new canonical chain. Reorgs are already handled by re-execution; the WAM transitions are deterministic functions of `BAL(N−1)` and `BAL(N−1−WARMING_WINDOW)`, so re-executing the new canonical chain rebuilds the correct WAM step-by-step. No explicit refund machinery is required.

In practice, finality (~12.8 min for two epochs) bounds reorg depth to a tiny fraction of the warming window, so the recomputation cost is negligible.

### Light/stateless clients

The WAM is derived deterministically from EIP-7928 BAL history. Clients that maintain BAL history (or accept proofs against an EIP-7928 commitment) can independently verify whether any specific item is warm at any block height by replaying the transition. No new commitment in the block header is strictly required.

## Copyright

Copyright and related rights waived via [CC0](../LICENSE).
