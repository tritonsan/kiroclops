# LightGlue in OpenCV 5: the same inputs on three platforms

Post-freeze check (23 and 24 October 2026) of the two OpenCV 5.0.0 LightGlue failures that `kiroclops/visual/features5.py` works around (technical report §3.2). The same scripts and seeded inputs ran on the Windows engineering machine and on bounded AWS instances (`scripts/upstream_lightglue_5x_check.py`, `scripts/phase4_aws_cool_scaling.py`). No KiroClops result depends on these runs.

| build | platform | 8x8, then 50x200 in one matcher | 8x8 after a 1000x1000 call | no match (2x64, 33x2) |
|---|---|---|---|---|
| opencv-python 5.0.0.93 | Windows x64, AMD Ryzen 5 5600 | process aborts, exit code `0xC0000409` | distances differ from a fresh matcher | `cv2.error: Unknown C++ exception from OpenCV code` |
| opencv-python 5.0.0.93 | Linux x86-64: Intel Granite Rapids (c8i), AMD EPYC 9R45 (c8a), AMD Zen 3 with AVX2 only (c6a) | 6 matches | identical | process killed by SIGFPE (exit code -8) |
| 5.x branch built from source, `3f12ad88` and `4058787c` (23 October 2026) | Linux x86-64: AMD EPYC 9R45 (c8a), AMD Zen 3 (c6a) | 6 matches | identical | SIGFPE in `cv::dnn::GatherNDLayerImpl::forward_impl`, `modules/dnn/src/layers/gatherND.cpp:133` |
| opencv-python 5.0.0.93; COOL 5.1.0-dev; 5.x `3f12ad88` | Linux AArch64, Graviton4 (c8g) | 6 matches | identical | 0 matches (an AArch64 integer division by zero does not trap) |

Reading: when the network finds no match, `GatherND` receives an empty index tensor and divides `out.total()` by `outer_size = 0`. That trap is what Windows reports as an unknown C++ exception, what kills the process on Linux x86-64, and what silently yields an empty result on AArch64; it is still in the 5.x branch. The abort after a smaller first call and the order dependence appear only on Windows, with any CPU tried on Linux unaffected.

Reported upstream: [opencv/opencv#30501](https://github.com/opencv/opencv/issues/30501) (GatherND division by zero) and [opencv/opencv#30502](https://github.com/opencv/opencv/issues/30502) (Windows abort).

| file | content |
|---|---|
| `lightglue_repro.py` | the seven call sequences, each in a fresh process |
| `repro_issue_30501.py`, `repro_windows_abort.py` | the minimal scripts in the two upstream reports |
| `windows-x64-*.txt` | outputs on the Windows machine |
| `linux-x86-64-*.txt`, `linux-aarch64-*.txt` | outputs on the AWS instances; the instance type and build are in the file name |
| `gdb-*.txt` | gdb backtraces of the 5.x build: the division by zero (2x64), and a normal exit for 8x8 then 50x200 |

Internal sources: `artifacts/phase4-aws/cool-scaling-01/*/runs/lightglue-*.txt` and `artifacts/phase4-v6/lightglue-5x-check-01`, `-03` and `-04` (build and cmake logs, commit, all outputs); `lightglue-5x-check-02` was stopped during stack creation, before anything was measured.
