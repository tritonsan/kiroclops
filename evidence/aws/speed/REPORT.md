# Analysis-service speed: standard, cool and local (2026-10-21T13:46:49.118706+00:00)

Recordings: 17 frozen V6 panel recordings, 3 repeats per remote engine, 3 local repeats. Worker time = `worker_wall_seconds` from each delivery receipt (claim to finished measurement: S3 package download and integrity check, sandbox start, extraction, full OpenCV 5 analysis); client upload, queue, polling and wake-up excluded.

| engine | OpenCV | jobs | median s | min s | max s | median end-to-end s | USD/h | USD per analysis (compute) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| standard | 5.0.0 | 51 | 5.134 | 4.448 | 6.227 | 13.9 | 0.1595 | 0.00023 |
| cool | 5.1.0-dev | 48 | 5.086 | 3.985 | 6.351 | 13.0 | 0.1695 | 0.00024 |
| local | 5.0.0 (AMD64, Windows) | 51 | 3.741 | 3.033 | 4.788 | - | - | - |

Paired per-recording ratio cool/standard over 17 recordings: median 1.003, geometric mean 0.996, range 0.871-1.036 (median speed-up -0.3%).

| recording | standard median s | cool median s | local median s |
|---|---:|---:|---:|
| reference/realistic-suite-panel-calibration-interrupt-w960-r1 | 4.528 | 4.539 | 3.306 |
| reference/realistic-suite-panel-calibration-open-close-w960-r1 | 5.666 | 5.640 | 4.029 |
| reference/realistic-suite-panel-calibration-open-w960-r1 | 4.551 | 4.679 | 3.424 |
| reference/realistic-suite-panel-calibration-reverse-w960-r1 | 5.634 | 5.569 | 4.410 |
| reference/realistic-v3-suite-panel-calibration-interrupt-w768-r1 | 4.550 | 4.679 | 3.272 |
| reference/realistic-v3-suite-panel-calibration-interrupt-w768-r2 | 4.607 | 4.014 | 3.233 |
| reference/realistic-v3-suite-panel-calibration-reverse-w768-r1 | 5.690 | 5.627 | 4.310 |
| reference/realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r1 | 5.114 | 5.066 | 3.861 |
| reference/realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r2 | 5.134 | 5.112 | 3.720 |
| reference/realistic-v4-suite-panel-calibration-open-wide-w1280-r1 | 5.126 | 5.087 | 3.679 |
| reference/realistic-v5-suite-panel-calibration-interrupt-early-w1280-r1 | 5.231 | 5.085 | 3.521 |
| reference/realistic-v5-suite-panel-calibration-interrupt-early-w1280-r2 | 5.068 | 5.119 | 3.530 |
| reference/realistic-v5-suite-panel-calibration-interrupt-early-w768-r1 | 4.484 | 4.529 | 3.065 |
| reference/realistic-v5-suite-panel-calibration-interrupt-early-w768-r2 | 4.587 | 4.648 | 3.068 |
| reference/realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1 | 6.184 | 6.227 | 4.510 |
| reference/realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2 | 6.220 | 6.247 | 4.515 |
| kiro-candidate-02/realistic-suite-panel-calibration-reverse-w960-r1 | 5.644 | 5.850 | 4.326 |

Scope: wall-time of the complete KiroClops measurement per recording on one c8g.xlarge worker with 4 threads. Not a kernel micro-benchmark; Python, I/O and sandbox start are part of every number. Prices are on-demand list prices for the instance only (the COOL Marketplace fee is listed separately when known). No measurement result, threshold or service code was changed.
