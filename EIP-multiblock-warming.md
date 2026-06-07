---
eip: <to be assigned>
title: Multi-block warming via Block Access Lists
description: Pre-warm the EIP-2929 access list at the start of each block using the union of Block Access Lists from the previous 7200 blocks (~24 hours).
author: <TBD>
discussions-to: <TBD>
status: Draft
type: Standards Track
category: Core
created: 2026-05-03
requires: 2929, 2930, 7928
---

## Abstract

Pre-warm the EIP-2929 access list at the start of each block with the union of Block Access Lists (BALs, per EIP-7928) from the most recent 7200 blocks (~24 hours). Storage slots and account addresses that appear in this rolling union pay the warm access cost (100 gas) on first touch instead of the cold cost (2100 for storage, 2600 for accounts).

## Motivation

EIP-2929's access list resets at every transaction. The same `(contract, slot)` and contract addresses are paid for as cold thousands of times per day across blocks. Empirical analysis of 2 000 mainnet blocks (24 988 201 – 24 990 200, ~6.7 h) shows that 80 % of cold-access gas is recoverable with a 1 024-block (≈ 3.4 h) warming horizon. Extending to 24 h captures essentially all of the recoverable amount: median per-block savings of ~16–18 % of `gasUsed`, with the bulk of the win at DELEGATECALL / STATICCALL / EXTCODECOPY targets and high-traffic SLOAD slots (proxies, routers, stablecoins).

## Specification

Let `B` be the block being executed and `BAL(N)` be the Block Access List of block `N` as defined in EIP-7928.

### Pre-warmed sets

Define the warm carry sets for `B` as:

```
warm_accounts(B) = ⋃ { addresses in BAL(N) : max(0, B−7200) ≤ N < B }
warm_slots(B)    = ⋃ { (addr, slot) pairs in BAL(N) : max(0, B−7200) ≤ N < B }
```

The window size is `WARMING_WINDOW = 7200` blocks (≈ 24 hours at 12 s slot time).

### Access list initialization

At the start of every transaction in `B`, the per-tx access list is initialized as today (precompiles, `tx.from`, `tx.to`, coinbase per EIP-3651, EIP-2930 access list, EIP-7702 authority list) **plus**:

- every address in `warm_accounts(B)` is treated as already-accessed for account-access opcodes;
- every `(addr, slot)` in `warm_slots(B)` is treated as already-accessed for storage-access opcodes.

### Pricing

No new gas constants are introduced. The pricing rules of EIP-2929 are reused unchanged; the *only* difference is which entries are pre-loaded into the access list.

For an opcode that consults the access list:

- if its operand is already in the access list (per the initialization above or per intra-tx accumulation), it pays the warm cost: `WARM_STORAGE_READ_COST = 100` for storage, `WARM_ACCOUNT_ACCESS_COST = 100` for accounts;
- otherwise, the cold cost: `COLD_SLOAD_COST = 2100` for storage, `COLD_ACCOUNT_ACCESS_COST = 2600` for accounts.

### Revert semantics

Intra-tx revert continues to behave as in EIP-2929: access-list entries added inside a reverted sub-call are removed, except that entries originating from `warm_accounts(B)` / `warm_slots(B)` are never removed (they were not added by this transaction).

### Genesis and activation

For the first 7200 blocks after activation, the union is taken over `[activation_block, B−1]` (the window simply contains fewer than 7200 blocks). After block `activation_block + 7200` the window is always full size.

## Rationale

### Why 7200 blocks (1 day)

Saving rate vs warming window, from the 2 000-block mainnet sample, median per-block percentage of `gasUsed`:

| W | Time back | Median saving | % of cold gas eliminated |
|---:|---:|---:|---:|
| 0 | 0 s | 4.8 % | 27.6 % |
| 1 | 12 s | 7.0 % | 37.2 % |
| 8 | 96 s | 10.1 % | 54.9 % |
| 64 | 12.8 min | 13.2 % | 69.4 % |
| 256 | 51.2 min | 14.4 % | 75.4 % |
| 1024 | 3.4 h | 15.2 % | 80.4 % |
| Asymptote | – | 18.4 % | 100 % |

Each doubling of `W` past 1024 adds < 0.3 pp. Extrapolating the saturation trend, `W = 7200` recovers ≥ 95 % of the asymptote (~17–18 % median saving). A 24-hour window aligns naturally with operational cycles (snapshot rotation, mempool window, oracle-update cadence) and is well beyond the finality horizon (~12.8 min for two epochs), eliminating reorg complications.

### Why not larger than 7200

Marginal gain past 24 h is below the precision of the measurement (< 0.5 pp). The state cost of the rolling union grows roughly linearly with the window size, with no compensating benefit.

### Reuse of EIP-7928 BALs

EIP-7928 introduces a canonical Block Access List committed to the block header. This proposal consumes that same artifact unchanged. Without EIP-7928 the warm sets are not part of consensus and stateless clients cannot verify gas charges; this EIP requires 7928 as a prerequisite.

### No new gas constants

Reusing the existing warm/cold costs keeps the gas table small and lets all existing static analysis tooling (gas estimation, fuzzers, compilers) continue to work without modification.

## Backwards Compatibility

Forward-only: every existing transaction pays the same or less gas. No transaction becomes invalid. EIP-2930 access lists supplied in transactions remain valid and are still pre-warmed (idempotent overlap with the multi-block warm set).

Block validation requires that nodes maintain the rolling 7200-block BAL union. From the 2 000-block sample, ~8 000 access ops per block produce ~3 000 unique `(addr, slot)` keys per block after dedup; ~16 M new keys per 7200-block day. At 52 bytes per key (20-byte address + 32-byte slot, no overhead) the rolling state is ~830 MB. Practical implementations should use a deduplicated structure (e.g., a per-block delta plus a global multiset with refcounts) so removal of expired blocks is O(|BAL|) per block.

## Test Cases

To be added. The reference scenario is:

1. Block `N−7200`: SLOAD on `(c, s)`. Cold (2100).
2. Block `N−7199`: SLOAD on `(c, s)`. Cold (2100). (Window has only the activation block; no carry.)
3. Block `N`: SLOAD on `(c, s)`. Warm (100), saving 2000 gas, because `(c, s) ∈ BAL(N−1)` (or any block in `[N−7200, N−1]`).
4. Block `N+7201`: SLOAD on `(c, s)`, assuming `(c, s)` was last touched in block `N`. Cold (2100), because `N` is now outside the rolling window.

## Reference Implementation

A non-consensus reference implementation that reads EIP-7928 BALs from chain history and computes the warm sets per block is available alongside the empirical analysis at https://github.com/nerolation/bal-warming.

## Security Considerations

### State growth and DoS

A malicious actor could attempt to inflate the warm set by accessing many distinct `(address, slot)` pairs cheaply (one per tx, paying the cold price once). The cost per inflated entry is at least 2 100 gas — equivalent to the price they'd pay anyway under EIP-2929. The amplification factor for the attacker is at most `7200 × (saving_recipient / cost_attacker)`, but the saving only goes to *legitimate* repeat accessors of the same key; an attacker cannot redirect savings to themselves. Net: the cost-benefit is unfavorable for an attacker.

State growth at the implementation layer is bounded by `7200 × max(BAL_per_block)`, which is in turn bounded by the block gas limit. Worst case (full blocks of unique-key cold accesses) is ~830 MB per 24-hour rolling window.

### Reorg behavior

If a block within the 7200-window is reorg'd, the warm set must be recomputed for the new canonical chain. Standard chain-reorg handling already re-executes transactions in the new chain; gas charges are recomputed naturally. No explicit refund machinery is required.

### Light/stateless clients

Clients that do not store BAL history cannot independently verify gas charges. They must either trust an EIP-7928-aware full node, or rely on the BAL commitment in the block header (per EIP-7928) plus a Merkle proof of inclusion for the keys consulted by the transactions they care about.

## Copyright

Copyright and related rights waived via [CC0](../LICENSE).
