# Inference patches

Local patches for EVO-X2 64GB / gfx1151.

- `strata/`: startup direct I/O, staging release, on-demand CPU vision, timings and host wait backoff.
- `vision/`: projector mmap and bitmap cleanup.
- `power/`: balanced CPU policy on boot, model start and resume; `cpu_max_mhz: 3500` in `/etc/evo-llm/power.json` caps CPU boost. Set `0` to restore the hardware maximum. APU power limits are retained.
- `webui/`: stream recovery and automatic context summaries near 80%; original browser history stays intact.
- `api/`: request-scoped Responses tool alias repair for nested Code Mode helpers.
- `gufo/`: experimental reserved-VRAM weight placement, exact-value F16 projector loading and 2048-token temporary workspaces; not the deployed engine.

Use the upstream revisions and hashes in [manifest.json](manifest.json).
Check with `git apply --check` before applying; build and deployment are separate.
The bitmap helper's exact base is unknown and needs source review.
Loader flags: `STRATA_STARTUP_DIRECT=1`, `STRATA_STARTUP_TRIM_STAGING=1`.
CPU helper mmap: `STRATA_VISION_MMAP=1`. Mode requirements are in each patch.
Host wait: `STRATA_HOST_WAIT_US=100`, `STRATA_HOST_SPIN_US=0`; unset or wait `0` disables it.
Flash fused prefill: install `strata/fused-prefill.conf` in `/etc/systemd/system/evo-flash.service.d/`; measured with the 3500MHz CPU ceiling.
Speed comes first; only negligible measured slowdowns may be accepted for RAM savings. Model weights, KV precision and context limits stay unchanged. Flash uses `--prefill 4096`; the slower 2048 profile was reverted.

Experiments and rejected candidates are recorded in [benchmarks/](benchmarks/) and [manifest.json](manifest.json). Neither a stable 20 tok/s floor for 27B nor the requested +5% gain in every slow workload was demonstrated. Qwen experimental changes remain held.

[Qwen timing probes](nathan/sampler-profile/) measured CPU sampling below 0.7% and draft generation around 9.3% of decode on two fixed requests. [Results and reproduction](benchmarks/20261026-sampler-profile.json). Diagnostic only; no speed or RAM gain claimed.

[Guarded block verification](benchmarks/20261026-block-guarded.json): 32 requests, geometric mean decode +5.9% on code / +2.3% on Korean thinking; 5 of 16 pairs regressed. Held, not deployed; full-context qualification remains untested.

**Flash scratch reuse is deployed.** Buffers with non-overlapping lifetimes share storage. [55 diagnostic requests](benchmarks/20261026-prefill-hc-scratch-parity.json) matched output, first logits and live state exactly. [32 timed requests](benchmarks/20261026-prefill-hc-scratch-performance.json): decode -0.28%/+0.08%, prefill about -0.65%; all 16 output pairs matched. Anonymous RAM fell about 224–248MiB. [44 qualification requests](benchmarks/20261026-prefill-hc-scratch-qualification.json) passed tools, JSON, cancellation recovery, 129K context, append cache and images; image-phase available-RAM minima improved 176–202MiB. One 63.45s image-preparation outlier was not reproduced in [six follow-up images](benchmarks/20261026-prefill-hc-scratch-vision.json); candidate image latency averaged +1.1%. Its cause remains unresolved. This is a RAM saving with a small measured speed cost, not demonstrated acceleration.

Apply `prefill-hc-scratch-experimental.patch` after `evo-7dd5cf1.patch`, build for HIP/gfx1151, then set `STRATA_PREFILL_HC_ALIAS=1` and `STRATA_PREFILL_HC_ALIAS_ACCOUNT=1`. The filename is retained for traceability. Do not combine unrelated experimental patches. [Deployment and rollback](benchmarks/20261026-prefill-hc-scratch-deployment.json) records executable/config hashes, retained backup and raw evidence. No new swap-out or OOM during these tests/deployment; the Discord process and its RAM session were preserved. Image/browser concurrency beyond the tested workload is unverified.

Apply `host-wait-backoff.patch` after `evo-v0.1.43.patch`. Linux/HIP build only verified.
For the 7dd5cf1 engine, use `evo-7dd5cf1.patch` instead of those two engine patches; settings are in `strata/runtime-20261025.json`. Keep the existing patched Python wrapper.
`final-output-budget.patch` adds opt-in `final_output_max_tokens`, excluding reasoning; the total context limit still applies.
For WebUI compaction, apply `stream-context.patch`, copy `webui/auto-compact.js` to `tools/ui/src/lib/services/`, then apply `auto-compact.patch`. Rebuild/deploy the UI separately. This does not compact external API histories.
For Responses aliases, copy `api/evo_responses_tools.py` beside `evo_responses_display.py`, then apply `api/responses-tools.patch`. Restart only the router after requests drain.

[Upstream licenses](THIRD-PARTY-NOTICES.md).
