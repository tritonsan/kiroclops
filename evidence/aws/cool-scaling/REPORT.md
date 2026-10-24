# COOL scaling: image size, threads, throughput and cost per analysis

Post-hoc measurement after the V6 freeze; changes no recorded result. Each instance ran three rounds per engine in alternating order on identical inputs (input.zip SHA-256 in each state.json). Kernels: median of seven timed batches; 1080p and 2160p frames are tiled from the 960 x 640 panel frames. Prices: AWS Price List API, us-east-1 on-demand Linux, effective 2026-10-01; COOL fee from its Marketplace listing.

## Instances

| instance | CPU | vCPUs | threads per core | engine | OpenCV | rounds | engine check | USD/h |
|---|---|---:|---:|---|---|---:|---|---:|
| c8a.xlarge | AMD EPYC 9R45 | 4 | 1 | stock | 5.0.0 | 3 | pass | 0.2155 |
| c8g.4xlarge | Neoverse-V2 | 16 | 1 | cool | 5.1.0-dev | 3 | pass | 0.6781 |
| c8g.4xlarge | Neoverse-V2 | 16 | 1 | stock | 5.0.0 | 3 | pass | 0.6381 |
| c8g.xlarge | Neoverse-V2 | 4 | 1 | cool | 5.1.0-dev | 3 | pass | 0.1695 |
| c8g.xlarge | Neoverse-V2 | 4 | 1 | stock | 5.0.0 | 3 | pass | 0.1595 |
| c8i.xlarge | Intel(R) Xeon(R) 6975P-C | 4 | 2 | stock | 5.0.0 | 3 | pass | 0.1874 |

## Throughput and cost (compute only)

One single-threaded process per vCPU, each measuring the panel, game and SVG recordings in turn; median over rounds.

| instance | engine | analyses per hour, median (range of rounds) | USD per 1,000 analyses | one analysis at a time, all vCPUs as threads: analyses per hour |
|---|---|---:|---:|---:|
| c8a.xlarge | stock | 5016 (4970–5102) | 0.043 | 1398 (4 threads) |
| c8g.4xlarge | cool | 14126 (13167–14252) | 0.048 | 1089 (16 threads) |
| c8g.4xlarge | stock | 14269 (13335–14334) | 0.045 | 977 (16 threads) |
| c8g.xlarge | cool | 3737 (2907–3744) | 0.045 | 1117 (4 threads) |
| c8g.xlarge | stock | 3101 (3047–3825) | 0.051 | 1047 (4 threads) |
| c8i.xlarge | stock | 2597 (2470–2601) | 0.072 | 1014 (4 threads) |

## COOL against the standard wheel on c8g.4xlarge (ratio = standard time / COOL time)

| kernel | native, 1 thr | native, 2 thr | native, 4 thr | native, 8 thr | native, 16 thr | 1080p, 1 thr | 1080p, 4 thr | 1080p, 16 thr | 2160p, 1 thr | 2160p, 4 thr | 2160p, 16 thr |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `imdecode_png_frame` | 1.02 | 1.00 | 1.01 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.01 | 1.00 | 1.01 |
| `cvtColor_bgr2gray_frame` | 0.97 | 1.00 | 1.01 | 1.01 | 1.00 | 1.01 | 0.99 | 0.98 | 1.00 | 1.02 | 0.96 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 0.98 | 1.73 | 1.76 | 1.80 | 1.80 | 0.98 | 2.11 | 2.95 | 0.97 | 2.19 | 3.17 |
| `goodFeaturesToTrack_frame` | 1.00 | 1.25 | 1.49 | 1.66 | 1.66 | 0.98 | 1.83 | 2.62 | 0.81 | 1.33 | 1.57 |
| `calcOpticalFlowPyrLK_100_points` | 1.19 | 1.14 | 1.09 | 1.08 | 1.04 | 1.04 | 1.05 | 1.03 | 1.02 | 1.06 | 1.09 |
| `canny_frame` | 1.04 | 1.08 | 1.07 | 1.01 | 0.98 | 1.05 | 1.02 | 0.99 | 1.07 | 0.97 | 0.98 |
| `findContours_edges` | 1.25 | 1.21 | 1.19 | 1.19 | 1.20 | 1.18 | 1.17 | 1.19 | 1.14 | 1.15 | 1.18 |
| `connectedComponentsWithStats_mask` | 1.11 | 5.26 | 3.40 | 2.09 | 1.07 | 1.11 | 4.18 | 1.56 | 1.11 | 3.91 | 1.75 |
| `gaussianBlur_5x5_frame` | 1.19 | 1.20 | 1.18 | 1.15 | 1.07 | 1.00 | 1.14 | 1.12 | 1.16 | 1.06 | 1.08 |
| `integral_frame` | 1.00 | 1.00 | 0.99 | 0.99 | 0.98 | 0.98 | 1.01 | 0.98 | 0.98 | 0.97 | 0.96 |
| `absdiff_frame` | 1.03 | 1.03 | 1.04 | 1.07 | 1.08 | 1.04 | 0.91 | 1.04 | 1.06 | 0.99 | 1.01 |
| `phaseCorrelate_crop` | 1.12 | 1.26 | 1.36 | 1.38 | 1.39 | 1.15 | 1.40 | 1.47 | 1.11 | 1.31 | 1.32 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.05 | 1.06 | 1.08 | 1.10 | 1.10 | — | — | — | — | — | — |
| `resize_cubic_x8_48px_patch` | 1.35 | 1.29 | 1.31 | 1.29 | 1.32 | — | — | — | — | — | — |

| recording (`measure_direct`) | 1 threads | 4 threads | 16 threads |
|---|---:|---:|---:|
| panel-calibration-reverse-w960 | 0.981 | 1.047 | 1.076 |
| game-reference-h03 | 1.002 | 1.001 | 1.006 |
| svg-three-to-four-w960-r1 | 0.983 | 1.114 | 1.145 |

Whole-instance throughput, COOL over standard (medians): 0.990; the per-round ranges overlap, so the difference is not resolved.

## COOL against the standard wheel on c8g.xlarge (ratio = standard time / COOL time)

| kernel | native, 1 thr | native, 2 thr | native, 4 thr | 1080p, 1 thr | 1080p, 4 thr | 2160p, 1 thr | 2160p, 4 thr |
|---|---:|---:|---:|---:|---:|---:|---:|
| `imdecode_png_frame` | 1.01 | 1.01 | 1.01 | 1.00 | 1.00 | 1.00 | 1.00 |
| `cvtColor_bgr2gray_frame` | 0.97 | 1.00 | 1.01 | 1.02 | 0.73 | 1.04 | 0.74 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 0.99 | 1.71 | 1.72 | 0.97 | 1.83 | 0.97 | 1.95 |
| `goodFeaturesToTrack_frame` | 0.99 | 1.25 | 1.28 | 0.95 | 1.34 | 0.81 | 1.06 |
| `calcOpticalFlowPyrLK_100_points` | 1.17 | 1.13 | 0.92 | 1.03 | 0.83 | 1.02 | 0.86 |
| `canny_frame` | 1.04 | 1.04 | 0.88 | 1.03 | 0.58 | 1.05 | 0.79 |
| `findContours_edges` | 1.22 | 1.21 | 1.22 | 1.18 | 1.18 | 1.17 | 1.14 |
| `connectedComponentsWithStats_mask` | 1.10 | 5.32 | 3.18 | 1.11 | 3.79 | 1.10 | 3.78 |
| `gaussianBlur_5x5_frame` | 1.20 | 1.20 | 0.93 | 1.08 | 0.91 | 1.04 | 0.84 |
| `integral_frame` | 1.01 | 1.01 | 1.01 | 0.98 | 0.98 | 0.97 | 0.97 |
| `absdiff_frame` | 1.01 | 1.03 | 1.04 | 1.01 | 0.98 | 1.01 | 1.01 |
| `phaseCorrelate_crop` | 1.14 | 1.28 | 1.33 | 1.15 | 1.35 | 1.10 | 1.29 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.05 | 1.07 | 1.08 | — | — | — | — |
| `resize_cubic_x8_48px_patch` | 1.35 | 1.26 | 1.32 | — | — | — | — |

| recording (`measure_direct`) | 1 threads | 4 threads |
|---|---:|---:|
| panel-calibration-reverse-w960 | 0.974 | 1.027 |
| game-reference-h03 | 0.994 | 1.002 |
| svg-three-to-four-w960-r1 | 0.977 | 1.096 |

Whole-instance throughput, COOL over standard (medians): 1.205; the per-round ranges overlap, so the difference is not resolved.

On a 4-vCPU instance the slowest of the four processes sets the wall time, so a round is sensitive to any other activity on the machine. Read the throughput together with its range and with the one-thread `measure_direct` times, which measure the same work one process at a time.

## Thread scaling of single kernels (time at 1 thread / time at n threads, native frames)

**c8a.xlarge, stock**

| kernel | 1 | 2 | 4 |
|---|---:|---:|---:|
| `connectedComponentsWithStats_mask` | 1.00 | 1.91 | 3.74 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 1.00 | 1.00 | 1.00 |
| `phaseCorrelate_crop` | 1.00 | 0.99 | 1.00 |
| `goodFeaturesToTrack_frame` | 1.00 | 1.00 | 0.99 |
| `gaussianBlur_5x5_frame` | 1.00 | 1.94 | 3.62 |
| `imdecode_png_frame` | 1.00 | 1.01 | 1.02 |
| `calcOpticalFlowPyrLK_100_points` | 1.00 | 1.70 | 2.69 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.00 | 1.00 | 1.01 |

**c8g.4xlarge, cool**

| kernel | 1 | 2 | 4 | 8 | 16 |
|---|---:|---:|---:|---:|---:|
| `connectedComponentsWithStats_mask` | 1.00 | 8.37 | 10.29 | 10.93 | 7.81 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 1.00 | 1.74 | 1.75 | 1.75 | 1.75 |
| `phaseCorrelate_crop` | 1.00 | 1.13 | 1.21 | 1.23 | 1.24 |
| `goodFeaturesToTrack_frame` | 1.00 | 1.28 | 1.55 | 1.74 | 1.71 |
| `gaussianBlur_5x5_frame` | 1.00 | 2.03 | 3.89 | 6.41 | 8.77 |
| `imdecode_png_frame` | 1.00 | 0.99 | 0.99 | 0.99 | 0.99 |
| `calcOpticalFlowPyrLK_100_points` | 1.00 | 1.59 | 2.25 | 2.78 | 2.75 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.00 | 0.98 | 0.98 | 0.98 | 0.98 |

**c8g.4xlarge, stock**

| kernel | 1 | 2 | 4 | 8 | 16 |
|---|---:|---:|---:|---:|---:|
| `connectedComponentsWithStats_mask` | 1.00 | 1.76 | 3.36 | 5.81 | 8.13 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 1.00 | 0.99 | 0.98 | 0.96 | 0.95 |
| `phaseCorrelate_crop` | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `goodFeaturesToTrack_frame` | 1.00 | 1.02 | 1.04 | 1.04 | 1.02 |
| `gaussianBlur_5x5_frame` | 1.00 | 2.01 | 3.91 | 6.63 | 9.77 |
| `imdecode_png_frame` | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `calcOpticalFlowPyrLK_100_points` | 1.00 | 1.66 | 2.45 | 3.06 | 3.16 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.00 | 0.97 | 0.95 | 0.94 | 0.93 |

**c8g.xlarge, cool**

| kernel | 1 | 2 | 4 |
|---|---:|---:|---:|
| `connectedComponentsWithStats_mask` | 1.00 | 8.37 | 9.18 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 1.00 | 1.72 | 1.72 |
| `phaseCorrelate_crop` | 1.00 | 1.12 | 1.16 |
| `goodFeaturesToTrack_frame` | 1.00 | 1.28 | 1.32 |
| `gaussianBlur_5x5_frame` | 1.00 | 1.82 | 2.42 |
| `imdecode_png_frame` | 1.00 | 1.00 | 1.00 |
| `calcOpticalFlowPyrLK_100_points` | 1.00 | 1.45 | 1.70 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.00 | 0.99 | 0.98 |

**c8g.xlarge, stock**

| kernel | 1 | 2 | 4 |
|---|---:|---:|---:|
| `connectedComponentsWithStats_mask` | 1.00 | 1.73 | 3.18 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 1.00 | 0.99 | 0.98 |
| `phaseCorrelate_crop` | 1.00 | 1.00 | 1.00 |
| `goodFeaturesToTrack_frame` | 1.00 | 1.02 | 1.02 |
| `gaussianBlur_5x5_frame` | 1.00 | 1.81 | 3.13 |
| `imdecode_png_frame` | 1.00 | 1.00 | 1.00 |
| `calcOpticalFlowPyrLK_100_points` | 1.00 | 1.51 | 2.16 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.00 | 0.98 | 0.96 |

**c8i.xlarge, stock**

| kernel | 1 | 2 | 4 |
|---|---:|---:|---:|
| `connectedComponentsWithStats_mask` | 1.00 | 1.89 | 2.88 |
| `matchTemplate_ccoeff_normed_full_frame_64px` | 1.00 | 1.01 | 1.01 |
| `phaseCorrelate_crop` | 1.00 | 1.00 | 1.00 |
| `goodFeaturesToTrack_frame` | 1.00 | 0.99 | 0.98 |
| `gaussianBlur_5x5_frame` | 1.00 | 2.43 | 2.50 |
| `imdecode_png_frame` | 1.00 | 1.00 | 1.00 |
| `calcOpticalFlowPyrLK_100_points` | 1.00 | 1.34 | 1.48 |
| `matchTemplate_ccoeff_normed_200px_window_48px` | 1.00 | 1.00 | 1.00 |

