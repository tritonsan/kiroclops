# V6 equivalence replay

Records: 16, identical to stored observations: 16.
Contract: `{"min_valid_ratio": 0.95, "require_static_camera": true}`. Refinement: `None`.

| role | case | identical | max object delta px | validity mismatches | max connection delta px | seconds |
|---|---|---|---:|---:|---:|---:|
| reference | realistic-suite-panel-calibration-interrupt-w960-r1 | yes | 0 | 0 | 0 | 3.9 |
| reference | realistic-suite-panel-calibration-open-close-w960-r1 | yes | 0 | 0 | 0 | 5.2 |
| reference | realistic-suite-panel-calibration-open-w960-r1 | yes | 0 | 0 | 0 | 4.2 |
| reference | realistic-suite-panel-calibration-reverse-w960-r1 | yes | 0 | 0 | 0 | 5.4 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r1 | yes | 0 | 0 | 0 | 3.3 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r2 | yes | 0 | 0 | 0 | 3.6 |
| reference | realistic-v3-suite-panel-calibration-reverse-w768-r1 | yes | 0 | 0 | 0 | 4.6 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r1 | yes | 0 | 0 | 0 | 4.4 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r2 | yes | 0 | 0 | 0 | 4.3 |
| reference | realistic-v4-suite-panel-calibration-open-wide-w1280-r1 | yes | 0 | 0 | 0 | 4.5 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r1 | yes | 0 | 0 | 0 | 4.3 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r2 | yes | 0 | 0 | 0 | 4.3 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r1 | yes | 0 | 0 | 0 | 3.9 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r2 | yes | 0 | 0 | 0 | 3.1 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1 | yes | 0 | 0 | 0 | 5.7 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2 | yes | 0 | 0 | 0 | 5.7 |

## Learned-feature cross-check `features_v1` (ALIKED + LightGlue, OpenCV 5 features module)

| role | case | object | frames | located | median px | p95 px | max px | median scale | median inlier ratio | median matches | not located | seconds |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| reference | realistic-suite-panel-calibration-interrupt-w960-r1 | bookmark | 155 | 148 | 0.99 | 1.23 | 1.77 | 1.01 | 1.00 | 21.00 | {'too_few_matches': 6, 'matcher_engine_failure': 1} | 609.3 |
| reference | realistic-suite-panel-calibration-interrupt-w960-r1 | handle | 155 | 115 | 0.73 | 1.11 | 1.31 | 1.01 | 1.00 | 23 | {'matcher_engine_failure': 32, 'too_few_matches': 7, 'no_consistent_similarity': 1} | 609.3 |
| reference | realistic-suite-panel-calibration-interrupt-w960-r1 | star | 155 | 146 | 0.82 | 0.91 | 221.50 | 1.02 | 0.98 | 39.50 | {'matcher_engine_failure': 7, 'too_few_matches': 1, 'no_consistent_similarity': 1} | 609.3 |
| reference | realistic-suite-panel-calibration-open-close-w960-r1 | bookmark | 211 | 206 | 0.93 | 1.10 | 1.47 | 1.01 | 1.00 | 21.00 | {'too_few_matches': 5} | 857.0 |
| reference | realistic-suite-panel-calibration-open-close-w960-r1 | handle | 211 | 112 | 0.73 | 1.18 | 1.61 | 1.01 | 1.00 | 23.00 | {'too_few_matches': 19, 'matcher_engine_failure': 80} | 857.0 |
| reference | realistic-suite-panel-calibration-open-close-w960-r1 | star | 211 | 206 | 0.75 | 0.89 | 220.11 | 1.02 | 0.99 | 37.50 | {'too_few_matches': 3, 'matcher_engine_failure': 2} | 857.0 |
| reference | realistic-suite-panel-calibration-open-w960-r1 | bookmark | 163 | 158 | 0.82 | 1.01 | 1.49 | 1.02 | 1.00 | 25.00 | {'too_few_matches': 5} | 639.8 |
| reference | realistic-suite-panel-calibration-open-w960-r1 | handle | 163 | 8 | 0.74 | 0.94 | 0.94 | 1.01 | 1.00 | 23.50 | {'too_few_matches': 13, 'matcher_engine_failure': 142} | 639.8 |
| reference | realistic-suite-panel-calibration-open-w960-r1 | star | 163 | 158 | 0.71 | 0.86 | 220.37 | 1.02 | 1.00 | 38.00 | {'too_few_matches': 3, 'matcher_engine_failure': 2} | 639.8 |
| reference | realistic-suite-panel-calibration-reverse-w960-r1 | bookmark | 211 | 206 | 0.85 | 1.07 | 1.56 | 1.02 | 1.00 | 25.00 | {'too_few_matches': 5} | 827.1 |
| reference | realistic-suite-panel-calibration-reverse-w960-r1 | handle | 211 | 15 | 0.71 | 0.98 | 0.98 | 1.01 | 1.00 | 23 | {'matcher_engine_failure': 180, 'too_few_matches': 16} | 827.1 |
| reference | realistic-suite-panel-calibration-reverse-w960-r1 | star | 211 | 206 | 0.71 | 0.75 | 220.16 | 1.02 | 1.00 | 38.00 | {'matcher_engine_failure': 4, 'too_few_matches': 1} | 827.1 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r1 | bookmark | 155 | 147 | 0.99 | 1.18 | 1.59 | 1.01 | 1.00 | 21 | {'too_few_matches': 6, 'matcher_engine_failure': 1, 'no_consistent_similarity': 1} | 603.7 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r1 | handle | 155 | 111 | 0.72 | 1.10 | 1.46 | 1.01 | 1.00 | 23 | {'too_few_matches': 13, 'matcher_engine_failure': 31} | 603.7 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r1 | star | 155 | 143 | 0.81 | 0.89 | 219.76 | 1.02 | 0.98 | 40 | {'no_consistent_similarity': 3, 'matcher_engine_failure': 6, 'too_few_matches': 3} | 603.7 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r2 | bookmark | 155 | 148 | 0.99 | 1.12 | 1.58 | 1.01 | 1.00 | 21.00 | {'matcher_engine_failure': 2, 'too_few_matches': 5} | 611.3 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r2 | handle | 155 | 114 | 0.73 | 1.17 | 1.36 | 1.01 | 1.00 | 23.00 | {'matcher_engine_failure': 31, 'too_few_matches': 10} | 611.3 |
| reference | realistic-v3-suite-panel-calibration-interrupt-w768-r2 | star | 155 | 143 | 0.81 | 0.89 | 220.56 | 1.02 | 0.98 | 40 | {'too_few_matches': 6, 'matcher_engine_failure': 4, 'no_consistent_similarity': 2} | 611.3 |
| reference | realistic-v3-suite-panel-calibration-reverse-w768-r1 | bookmark | 211 | 206 | 0.84 | 1.15 | 1.51 | 1.02 | 1.00 | 24.00 | {'matcher_engine_failure': 2, 'too_few_matches': 3} | 828.8 |
| reference | realistic-v3-suite-panel-calibration-reverse-w768-r1 | handle | 211 | 14 | 0.71 | 1.12 | 1.12 | 1.01 | 0.98 | 26.00 | {'too_few_matches': 15, 'matcher_engine_failure': 182} | 828.8 |
| reference | realistic-v3-suite-panel-calibration-reverse-w768-r1 | star | 211 | 206 | 0.67 | 0.76 | 220.20 | 1.01 | 1.00 | 37.00 | {'matcher_engine_failure': 5} | 828.8 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r1 | bookmark | 162 | 156 | 1.04 | 1.19 | 1.77 | 1.01 | 0.92 | 22.00 | {'too_few_matches': 4, 'matcher_engine_failure': 1, 'no_consistent_similarity': 1} | 630.0 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r1 | handle | 162 | 126 | 0.52 | 1.17 | 1.33 | 1.02 | 0.97 | 27.00 | {'matcher_engine_failure': 33, 'too_few_matches': 3} | 630.0 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r1 | star | 162 | 148 | 0.71 | 0.83 | 220.81 | 1.03 | 1.00 | 35.00 | {'no_consistent_similarity': 5, 'matcher_engine_failure': 4, 'too_few_matches': 5} | 630.0 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r2 | bookmark | 162 | 155 | 1.04 | 1.17 | 1.33 | 1.01 | 0.94 | 22 | {'too_few_matches': 6, 'no_consistent_similarity': 1} | 637.6 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r2 | handle | 162 | 124 | 0.52 | 1.20 | 1.47 | 1.02 | 0.97 | 27.00 | {'no_consistent_similarity': 1, 'matcher_engine_failure': 29, 'too_few_matches': 8} | 637.6 |
| reference | realistic-v4-suite-panel-calibration-interrupt-wide-w1280-r2 | star | 162 | 149 | 0.71 | 0.82 | 220.87 | 1.03 | 1.00 | 34 | {'no_consistent_similarity': 4, 'matcher_engine_failure': 5, 'too_few_matches': 4} | 637.6 |
| reference | realistic-v4-suite-panel-calibration-open-wide-w1280-r1 | bookmark | 164 | 159 | 0.82 | 1.02 | 1.37 | 1.02 | 1.00 | 24 | {'too_few_matches': 4, 'matcher_engine_failure': 1} | 643.7 |
| reference | realistic-v4-suite-panel-calibration-open-wide-w1280-r1 | handle | 164 | 5 | 0.50 | 0.78 | 0.78 | 1.01 | 1.00 | 27 | {'too_few_matches': 4, 'matcher_engine_failure': 155} | 643.7 |
| reference | realistic-v4-suite-panel-calibration-open-wide-w1280-r1 | star | 164 | 158 | 0.67 | 0.78 | 220.87 | 1.01 | 1.00 | 37.00 | {'too_few_matches': 3, 'matcher_engine_failure': 3} | 643.7 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r1 | bookmark | 158 | 151 | 1.04 | 1.19 | 1.75 | 1.01 | 0.92 | 22 | {'too_few_matches': 6, 'matcher_engine_failure': 1} | 618.3 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r1 | handle | 158 | 125 | 0.52 | 1.01 | 1.61 | 1.02 | 0.97 | 28 | {'matcher_engine_failure': 27, 'too_few_matches': 6} | 618.3 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r1 | star | 158 | 145 | 0.71 | 0.85 | 220.87 | 1.03 | 1.00 | 35 | {'no_consistent_similarity': 4, 'matcher_engine_failure': 5, 'too_few_matches': 4} | 618.3 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r2 | bookmark | 158 | 152 | 1.04 | 1.21 | 1.71 | 1.01 | 0.94 | 22.00 | {'too_few_matches': 5, 'matcher_engine_failure': 1} | 616.4 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r2 | handle | 158 | 126 | 0.52 | 1.00 | 1.72 | 1.02 | 0.97 | 28.00 | {'matcher_engine_failure': 23, 'too_few_matches': 9} | 616.4 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w1280-r2 | star | 158 | 145 | 0.71 | 0.82 | 219.35 | 1.03 | 1.00 | 35 | {'matcher_engine_failure': 6, 'no_consistent_similarity': 4, 'too_few_matches': 3} | 616.4 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r1 | bookmark | 153 | 144 | 0.99 | 1.19 | 1.77 | 1.01 | 1.00 | 21.00 | {'matcher_engine_failure': 3, 'too_few_matches': 5, 'no_consistent_similarity': 1} | 605.4 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r1 | handle | 153 | 114 | 0.73 | 1.11 | 1.38 | 1.01 | 1.00 | 23.00 | {'matcher_engine_failure': 30, 'too_few_matches': 9} | 605.4 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r1 | star | 153 | 142 | 0.83 | 0.90 | 221.50 | 1.02 | 0.98 | 40.00 | {'matcher_engine_failure': 6, 'too_few_matches': 3, 'no_consistent_similarity': 2} | 605.4 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r2 | bookmark | 152 | 144 | 0.99 | 1.19 | 1.59 | 1.01 | 1.00 | 21.00 | {'too_few_matches': 7, 'matcher_engine_failure': 1} | 596.7 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r2 | handle | 152 | 116 | 0.72 | 1.15 | 1.44 | 1.01 | 1.00 | 23.00 | {'matcher_engine_failure': 22, 'too_few_matches': 14} | 596.7 |
| reference | realistic-v5-suite-panel-calibration-interrupt-early-w768-r2 | star | 152 | 139 | 0.83 | 0.89 | 220.07 | 1.02 | 0.98 | 42 | {'too_few_matches': 4, 'matcher_engine_failure': 7, 'no_consistent_similarity': 2} | 596.7 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1 | bookmark | 211 | 206 | 0.84 | 1.08 | 1.48 | 1.02 | 1.00 | 24.00 | {'too_few_matches': 4, 'matcher_engine_failure': 1} | 827.6 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1 | handle | 211 | 14 | 0.71 | 1.04 | 1.04 | 1.01 | 1.00 | 24.00 | {'no_consistent_similarity': 1, 'matcher_engine_failure': 190, 'too_few_matches': 6} | 827.6 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r1 | star | 211 | 203 | 0.67 | 0.80 | 220.62 | 1.02 | 1.00 | 37 | {'no_consistent_similarity': 2, 'matcher_engine_failure': 5, 'too_few_matches': 1} | 827.6 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2 | bookmark | 211 | 206 | 0.83 | 1.08 | 1.57 | 1.02 | 1.00 | 24.00 | {'too_few_matches': 5} | 824.6 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2 | handle | 211 | 17 | 0.57 | 1.06 | 1.06 | 1.01 | 1.00 | 26 | {'too_few_matches': 8, 'matcher_engine_failure': 186} | 824.6 |
| reference | realistic-v5-suite-panel-calibration-reverse-wide-w1280-r2 | star | 211 | 204 | 0.67 | 0.75 | 219.83 | 1.02 | 1.00 | 37.00 | {'too_few_matches': 1, 'matcher_engine_failure': 5, 'no_consistent_similarity': 1} | 824.6 |
