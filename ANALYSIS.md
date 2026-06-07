# Multi-block storage warming on Ethereum mainnet

How much gas would EVM execution save if EIP-2929's per-tx access list persisted across blocks?

## Headline

| Warming horizon | Time back | Median saving | % of cold gas eliminated |
|---|---:|---:|---:|
| Intra-block (W=0) | 0 s | 4.8 % | 27.6 % |
| W=1 | 12 s | 7.0 % | 37.2 % |
| **W=8** | **96 s** | **10.1 %** | **54.9 %** |
| W=32 | 6.4 min | 12.5 % | 65.7 % |
| W=128 | 25.6 min | 13.9 % | 73.2 % |
| W=1024 | 3.4 hr | 15.2 % | 80.4 % |
| Asymptote (if every cold access were warm) | – | 18.4 % | 100 % |

*"Median saving" = median per-block percentage of `block.gasUsed` that would not have been spent. Asymptote = median `pct_max_saving` = upper bound bounded by EIP-2929 cold-warm differentials.*

The curve plateaus quickly: W=8 captures **55 %** of the maximum possible gain, W=128 captures **80 %**. Most of the value sits inside a 1–2 minute warming horizon.

## Data

| | |
|---|---|
| Source | `debug_traceTransaction` against an Erigon mainnet node |
| Block range | 24988201 – 24990200 (2000 blocks) |
| Wall time | 2026-04-29 20:41 → 2026-04-30 03:22 UTC (~6.7 h) |
| Access opcodes captured | 15 949 908 rows |
| Transactions | 258 135 |
| Empty blocks (no traceable access ops) | 1 (24988865) |

Captured opcodes: `SLOAD`, `SSTORE`, `CALL`, `STATICCALL`, `DELEGATECALL`, `CALLCODE`, `BALANCE`, `EXTCODESIZE`, `EXTCODEHASH`, `EXTCODECOPY`, `SELFDESTRUCT`, `CREATE`, `CREATE2`. `TLOAD`/`TSTORE` excluded (transient storage, not in EIP-2929 access list).

## Methodology

**Parser (`opcode_parser_v2.py`).** Per-trace EIP-2929 access-list simulation:

- Stack-derived address/slot operands, canonicalized to 40-hex addresses and 64-hex slots.
- DELEGATECALL/CALLCODE preserve caller's storage context (not the callee's).
- CREATE/CREATE2: new contract address resolved from the post-return stack and back-filled into rows produced by the constructor frame.
- Reverted frames undo their access-list additions (explicit REVERT and exceptional halts both treated as revert).
- Pre-warming: `tx.from`, `tx.to`, coinbase (EIP-3651), precompiles `0x01..0x0a`, EIP-2930 access list, EIP-7702 authority list.
- CALL-family `gas_cost` is the call's own base overhead (not the forwarded gas to the callee).

Validated by asserting, on 9 100+ rows across two full mainnet blocks, that the parser's warm/cold flag agrees with the deterministic gas-cost signature in the raw structLog (`SLOAD` cold ↔ 2100, warm ↔ 100; same shape for the BALANCE / EXTCODE* family at 2600/100; SSTORE cold-cost set `{2200, 5000, 22100}` vs warm `{100, 2900, 20000}`). Zero mismatches.

**Saving differentials (EIP-2929 cold → warm):**

| Opcode | Saving / hit |
|---|---:|
| SLOAD | 2 000 |
| SSTORE (cold portion) | 2 100 |
| CALL / STATICCALL / DELEGATECALL / CALLCODE | 2 500 |
| BALANCE / EXTCODESIZE / EXTCODEHASH / EXTCODECOPY | 2 500 |
| SELFDESTRUCT | 2 600 (no warm-default to subtract) |

**Window definition.** For each access row, `block_gap = current_block − last_block_where_(address, slot)_was_seen`. A row is "saved at window W" iff `block_gap ≤ W`. W=0 captures intra-block cross-tx warming only; W=k>0 also includes any of the previous k blocks.

**Fairness filter.** For window W, a block at position P within the dataset has at most P blocks of prior history. If P<W, its `block_gap` is bounded by P, not W — its measured `pct_saving_W` is biased low. All reported numbers above use `warmup_blocks = max(windows) = 1024`, restricting the aggregation to the last 975 blocks (each with full 1024-block lookback). All windows are then evaluated on the same population.

## Per-opcode breakdown (W=1024)

| Opcode | Cold count | Saved gas | Max saving | Saving rate |
|---|---:|---:|---:|---:|
| SLOAD | 1 943 104 | 3.07 B | 3.89 B | **79 %** |
| CALL | 156 612 | 336 M | 392 M | **86 %** |
| DELEGATECALL | 89 278 | 221 M | 223 M | **99 %** |
| STATICCALL | 74 814 | 180 M | 187 M | **96 %** |
| EXTCODESIZE | 74 526 | 150 M | 186 M | 81 % |
| SSTORE | 47 349 | 37.4 M | 99.4 M | 38 % |
| EXTCODEHASH | 1 803 | 4.2 M | 4.5 M | 93 % |
| BALANCE | 253 | 120 k | 633 k | 19 % |
| EXTCODECOPY | 232 | 570 k | 580 k | 98 % |
| SELFDESTRUCT | 5 | 10 k | 13 k | 80 % |

**Where the savings come from:**

- **SLOAD dominates absolute gas** (3.07 B of 4.00 B saved). Storage reads to the same contract+slot recur often.
- **DELEGATECALL (99 %), STATICCALL (96 %), EXTCODECOPY (98 %)** are near-perfectly reusable. Proxy patterns, multicall routers, and code introspection target the same handful of contracts across nearly every block.
- **SSTORE (38 %)** is the worst — writes are usually to slots that change identity (new nonces, new positions, new IDs), so warming the writeable slot pre-emptively rarely matches.
- **BALANCE (19 %)** rare and one-shot — querying arbitrary addresses doesn't recur.

## Caveats

1. **Sample window is 6.7 hours** of mainnet activity (May 2026). DEX/MEV pressure was moderate. Heavy-volume periods (e.g., USD-stablecoin depegs, NFT mints) would shift the mix toward more unique slots → lower saving rate. Heavy-block-builder periods → higher.
2. **The 18.4 % asymptote is itself an upper bound** assuming the access set could be pre-known per block. It does not include warm-cost savings (warm SLOAD still costs 100 gas).
3. **Cross-block warming is not currently part of EVM.** Implementing it raises reorg-safety questions: if you commit warm-state changes from block N before N is finalised and N gets reorg'd, you have to refund the gas — or design the warming scheme to apply only after finalisation.
4. **One block (24988865) has no traceable access ops** in our extraction — likely all txs failed pre-execution. Excluded from per-block aggregation; affects 1 / 1999 of the sample (negligible).

## Practical interpretation

If a future EIP made the EIP-2929 access list persist across N blocks:

- **N = 8 blocks (~96 s)** gives ~**10 % gas savings** to a typical block — over half of the theoretical maximum, with a horizon short enough that reorg-safety is straightforward (finalisation is now 2 epochs ≈ 12 min, longer than the warming horizon).
- **N = 32 blocks (1 epoch, ~6.4 min)** captures ~**12.5 %** — a clean fit with epoch boundaries.
- **N > 128 blocks (>25 min)** offers diminishing returns — each doubling of N adds <0.5 percentage points.

## Reproduce

```bash
# 1. Extract per-block access rows + meta
python3 extract_block.py --start 24988201 --end 24990200 --out data

# 2. Compute saving rates and per-block aggregate
python3 access_savings_parser.py --dir data \
    --windows 0,1,2,4,8,16,32,64,128,256,512,1024 \
    --per-block-out data/per_block_savings.parquet

# 3. Plot
BALWARMING_DIR=data jupyter nbconvert --to notebook --execute \
    block_warming_analysis.ipynb --output executed.ipynb
```
