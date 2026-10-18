# Inference patches

EVO-X2 64GB / gfx1151 / 75W. These are local source changes and configuration
experiments; O_DIRECT and mmap use existing platform facilities.

| Patch | Purpose | Status |
|---|---|---|
| `strata/evo-v0.1.43.patch` | Startup direct I/O, staging release, on-demand CPU vision, live timings | Recorded deployed source |
| `vision/llama-mmap.patch` | Read-only CPU projector mapping | Recorded deployed helper |
| `vision/strata-bitmap-lifetime.patch` | Release bitmap after preprocessing | Recorded deployed helper |
| `qwen/experiments/adaptive-dflash.patch` | Acceptance-based draft length | Rejected: slower |

## Apply

Versions and SHA-256: [manifest.json](manifest.json).
Strata base: v0.1.43 (`7695859aa13afb248bdb4d36f9b2e95707db079d`).
The mmap patch targets llama.cpp `3cf03257f219afbe7334045ff7c6a06ac68c627d`.
The bitmap helper's exact historical base commit is unknown; review its target
source/lifetime before applying. Adaptive targets historical commit `a043d38`;
compatibility with the later Nathan v0.7.8.1 runtime is unverified.

Set `PATCH_ROOT` to this directory and `STRATA_SRC` to a clean upstream checkout:

```sh
git -C "$STRATA_SRC" checkout --detach 7695859aa13afb248bdb4d36f9b2e95707db079d
git -C "$STRATA_SRC" apply --check "$PATCH_ROOT/strata/evo-v0.1.43.patch"
git -C "$STRATA_SRC" apply "$PATCH_ROOT/strata/evo-v0.1.43.patch"
```

Enable with `STRATA_STARTUP_DIRECT=1`, `STRATA_STARTUP_TRIM_STAGING=1`, and
`STRATA_VISION_MMAP=1` for the CPU helper. Loader mode must satisfy the patch's
resident-expert conditions. CPU on-demand vision uses server configuration.
Keep diagnostic copy/trim checks off during normal operation.
Other revisions require a separate port. Applying patches does not build/deploy;
weights, expert pack, SDK and upstream sources are external dependencies.

## Historical results

| Change | Recorded result |
|---|---|
| Startup direct | Loader peak 28.848 -> 20.949GiB; load 34.574 -> 28.449s; extra system swap-out 409.8125MiB -> 48KiB (swappiness 60) |
| Staging release | 566.53MiB temporary buffer freed; three normal loads added 4/4/0KiB swap-out |
| Trim, 120K input + 128 output | Prefill 430.4tok/s, decode 38.1tok/s; minimum available RAM 8.636GiB; extra swap-out 0 |
| Projector mmap | 334 weight tensors / 865.48MiB mapped; separate anonymous weight copy skipped |
| External Nathan runtime + batch 2048 | 16K English-code median 17.23 -> 18.78tok/s; prefill 65.3-65.7 -> 53.5-53.6s; two samples per fixture |

Loader peak includes reclaimable file cache; it is not permanent free RAM.
Mapped pages and encoding buffers still consume RAM. Zero swap is not zero total
SSD writes. Short historical samples establish neither accuracy parity nor a
minimum 20tok/s. Loader/trim measurements predate the v0.1.43 port.

Adaptive draft regressed (code 17.24 -> 16.44/16.73tok/s). PLE 2M saved only
0.265% read traffic and no throughput. Fused prefill increased prefill speed but
reduced decode and raised temperature. None was adopted. EngramHalo/Gufo had no
matched execution A/B. PCIe mode, checkpoint and batch tuning use existing features.

The Strata patch matched the server candidate diff on 2026-10-18. No new build,
inference or benchmark ran during packaging/docs cleanup. Credentials, network
maps, raw logs, model files and binaries are excluded. [Upstream notices](THIRD-PARTY-NOTICES.md).
