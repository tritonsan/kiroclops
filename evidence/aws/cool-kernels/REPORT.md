# COOL against the standard wheel on one Graviton4 instance

Post-hoc measurement after the V6 freeze. One c8g.xlarge from `ami-033e481a24f94c8cb`; COOL: OpenCV 5.1.0-dev, NumPy 2.5.1; standard: `opencv-python` 5.0.0, NumPy 2.2.6; Python 3.12.3 for both. Three rounds per engine in alternating order; each value is the median over rounds of the median of seven timed batches. Ratio = standard / COOL (above 1: COOL faster).

| kernel | COOL ms (4 threads) | standard ms (4 threads) | ratio, 4 threads | ratio, 1 thread |
|---|---:|---:|---:|---:|
| `imdecode_png_frame` | 1.950 | 1.969 | 1.01 | 1.01 |
| `cvtColor_bgr2gray_frame` | 0.063 | 0.062 | 0.99 | 0.97 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.826 | 1.965 | 1.08 | 1.05 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 29.214 | 45.501 | 1.56 | 0.99 |
| `resize_cubic_x8_48px_patch` | 0.144 | 0.182 | 1.27 | 1.31 |
| `goodFeaturesToTrack_frame` | 3.060 | 3.867 | 1.26 | 0.98 |
| `calcOpticalFlowPyrLK_100_points` | 0.661 | 0.722 | 1.09 | 1.17 |
| `canny_frame` | 0.459 | 0.458 | 1.00 | 1.04 |
| `findContours_edges` | 0.339 | 0.412 | 1.22 | 1.22 |
| `connectedComponentsWithStats_mask` | 0.176 | 0.832 | 4.72 | 1.11 |
| `gaussianBlur_5x5_frame` | 0.174 | 0.204 | 1.17 | 1.21 |
| `integral_frame` | 0.192 | 0.193 | 1.01 | 1.01 |
| `absdiff_frame` | 0.080 | 0.085 | 1.06 | 0.96 |
| `phaseCorrelate_crop` | 3.508 | 4.684 | 1.34 | 1.14 |

| recording (`measure_direct`, in process, 4 threads) | COOL s | standard s | ratio | OpenCV share (COOL) |
|---|---:|---:|---:|---:|
| panel-calibration-reverse-w960 | 3.758 | 3.934 | 1.047 | 65% |
| game-reference-h03 | 0.168 | 0.168 | 1.001 | 35% |
| svg-three-to-four-w960-r1 | 5.734 | 6.189 | 1.079 | 54% |

Build lines: COOL Baseline:                    SVE NEON FP16 NEON_DOTPROD NEON_FP16 NEON_BF16; Dispatched code generation:; Parallel framework:            pthreads; Custom HAL:                  YES (carotene (ver 0.0.1) KleidiCV (ver 0.7.0)). Standard Baseline:                    NEON FP16; Dispatched code generation:  NEON_DOTPROD NEON_FP16 NEON_BF16; Parallel framework:            pthreads; Custom HAL:                  YES (carotene (ver 0.0.1) KleidiCV (ver 26.03)).
