# V6 equivalence replay

Records: 5, identical to stored observations: 5.
Contract: `{"min_valid_ratio": 0.95, "require_static_camera": true}`. Refinement: `None`.

| role | case | identical | max object delta px | validity mismatches | max connection delta px | seconds |
|---|---|---|---:|---:|---:|---:|
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | yes | 0 | 0 | 0 | 12.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | yes | 0 | 0 | 0 | 12.0 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | yes | 0 | 0 | 0 | 6.7 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | yes | 0 | 0 | 0 | 8.8 |
| reference | realistic-svg-six-node-measurement | yes | 0 | 0 | 0 | 10.4 |

## Learned-feature cross-check `features_v1` (ALIKED + LightGlue, OpenCV 5 features module)

| role | case | object | frames | located | median px | p95 px | max px | median scale | median inlier ratio | median matches | not located | seconds |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | a | 179 | 179 | 0.64 | 1.13 | 1.59 | 1.00 | 0.82 | 25 | - | 684.8 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | b | 179 | 179 | 0.60 | 0.60 | 0.60 | 1.01 | 0.97 | 35 | - | 684.8 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | c | 179 | 179 | 0.62 | 0.77 | 0.97 | 1.00 | 1.00 | 30 | - | 684.8 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | d | 25 | 16 | 0.54 | 0.65 | 0.65 | 1.01 | 1.00 | 10.00 | {'too_few_matches': 5, 'no_consistent_similarity': 4} | 684.8 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | a | 179 | 179 | 0.64 | 0.93 | 1.17 | 1.00 | 0.80 | 25 | - | 695.0 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | b | 179 | 179 | 0.60 | 0.60 | 0.60 | 1.01 | 0.97 | 35 | - | 695.0 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | c | 179 | 179 | 0.61 | 0.77 | 0.97 | 1.00 | 0.97 | 30 | - | 695.0 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | d | 25 | 20 | 0.54 | 0.66 | 0.66 | 1.01 | 1.00 | 10.00 | {'no_consistent_similarity': 4, 'too_few_matches': 1} | 695.0 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | a | 179 | 179 | 0.64 | 0.80 | 0.98 | 1.00 | 0.82 | 17 | - | 906.1 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | b | 179 | 179 | 0.60 | 0.60 | 0.60 | 1.01 | 0.97 | 35 | - | 906.1 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | c | 179 | 179 | 0.40 | 0.73 | 0.93 | 1.00 | 0.93 | 19 | - | 906.1 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | d | 154 | 154 | 0.64 | 0.72 | 0.92 | 1.00 | 1.00 | 12.00 | - | 906.1 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | a | 180 | 180 | 0.64 | 0.88 | 0.91 | 1.00 | 0.82 | 17.00 | - | 1053.7 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | b | 180 | 180 | 0.60 | 0.60 | 0.60 | 1.01 | 0.97 | 35.00 | - | 1053.7 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | c | 180 | 180 | 0.39 | 0.75 | 1.00 | 1.00 | 0.91 | 19.00 | - | 1053.7 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | d | 155 | 155 | 0.66 | 0.80 | 1.14 | 1.00 | 1.00 | 12 | - | 1053.7 |
| reference | realistic-svg-six-node-measurement | a | 179 | 152 | 0.91 | 0.91 | 0.91 | 0.99 | 0.77 | 13.00 | {'no_consistent_similarity': 27} | 1618.9 |
| reference | realistic-svg-six-node-measurement | b | 179 | 179 | 0.50 | 0.80 | 0.82 | 0.99 | 0.83 | 35 | - | 1618.9 |
| reference | realistic-svg-six-node-measurement | c | 179 | 179 | 0.35 | 0.35 | 0.42 | 1.00 | 1.00 | 20 | - | 1618.9 |
| reference | realistic-svg-six-node-measurement | d | 179 | 179 | 0.67 | 1.06 | 1.06 | 0.99 | 0.86 | 14 | - | 1618.9 |
| reference | realistic-svg-six-node-measurement | e | 179 | 179 | 0.42 | 0.57 | 0.57 | 0.99 | 1.00 | 28 | - | 1618.9 |
| reference | realistic-svg-six-node-measurement | f | 179 | 179 | 0.84 | 0.93 | 0.93 | 1.00 | 0.97 | 30 | - | 1618.9 |
