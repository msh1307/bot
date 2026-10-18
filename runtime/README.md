# Inference patches

Local patches for EVO-X2 64GB / gfx1151.

- `strata/`: startup direct I/O, staging release, on-demand CPU vision and timings.
- `vision/`: projector mmap and bitmap cleanup.
- `power/`: balanced CPU policy on boot, model start and resume; existing power limits are retained.

Use the upstream revisions and hashes in [manifest.json](manifest.json).
Check with `git apply --check` before applying; build and deployment are separate.
The bitmap helper's exact base is unknown and needs source review.
Loader flags: `STRATA_STARTUP_DIRECT=1`, `STRATA_STARTUP_TRIM_STAGING=1`.
CPU helper mmap: `STRATA_VISION_MMAP=1`. Mode requirements are in each patch.

[Upstream licenses](THIRD-PARTY-NOTICES.md).
