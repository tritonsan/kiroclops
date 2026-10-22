# Sample recordings for judges

Two small recordings from the Phase 3B calibration corpus (`artifacts/phase3b/calibration-corpus-02`, whose manifest is copied here as `corpus-manifest.json` with the SHA-256 of every package, selection and contract).

| sample | domain | frames | size | content |
|---|---|---|---|---|
| `game-reference-h03` | game | 240 at 60 Hz, 228×128 | 0.3 MB | Tiny Crate reference jump (public-domain game, see `config/third-party-notices.json`) |
| `svg-three-to-four-w960-r1` | svg | 179 with browser clock brackets, 960×640 | 7 MB | our own SVG diagram fixture, a node being added |

Each folder holds `package/` (PNG frames and `manifest.json` with hashed timestamps), `selection.json` (objects, anchors, connections) and `contract.json` (measurement contract). `scripts/judge_check.py` submits them to the live service; `kiroclops.cloud.execution.measure_direct` measures them locally.
