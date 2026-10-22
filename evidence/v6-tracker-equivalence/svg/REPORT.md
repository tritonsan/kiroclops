# V6 equivalence replay

Records: 15, identical to stored observations: 15.
Contract: `{"min_valid_ratio": 0.95, "require_static_camera": true}`. Refinement: `upsampled_v1`.

| role | case | identical | max object delta px | validity mismatches | max connection delta px | seconds |
|---|---|---|---:|---:|---:|---:|
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | yes | 0 | 0 | 0 | 11.9 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | yes | 0 | 0 | 0 | 11.8 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | yes | 0 | 0 | 0 | 6.6 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | yes | 0 | 0 | 0 | 6.6 |
| reference | realistic-svg-six-node-measurement | yes | 0 | 0 | 0 | 8.5 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r1 | yes | 0 | 0 | 0 | 12.0 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r2 | yes | 0 | 0 | 0 | 11.7 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r1 | yes | 0 | 0 | 0 | 6.6 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r2 | yes | 0 | 0 | 0 | 6.8 |
| before | realistic-svg-six-node-measurement | yes | 0 | 0 | 0 | 8.1 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r1 | yes | 0 | 0 | 0 | 11.8 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r2 | yes | 0 | 0 | 0 | 11.8 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r1 | yes | 0 | 0 | 0 | 6.7 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r2 | yes | 0 | 0 | 0 | 6.6 |
| original | realistic-svg-six-node-measurement | yes | 0 | 0 | 0 | 8.2 |

## Opt-in refinement `upsampled_v1`: offset from the integer peak

| role | case | object | frames | mean |offset| px | max px | fraction > 0.25 px | statuses | seconds |
|---|---|---|---:|---:|---:|---:|---|---:|
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r1 | d | 25 | 0.000 | 0.000 | 0.00 | {'interior': 25} | 14.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.2 |
| reference | realistic-suite-svg-calibration-four-to-three-w960-r2 | d | 25 | 0.000 | 0.000 | 0.00 | {'interior': 25} | 14.2 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 9.5 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 9.5 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 9.5 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r1 | d | 154 | 0.000 | 0.000 | 0.00 | {'interior': 151, 'clamped_to_half_pixel': 3} | 9.5 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | a | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 9.6 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | b | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 9.6 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | c | 180 | 0.000 | 0.000 | 0.00 | {'interior': 179, 'clamped_to_half_pixel': 1} | 9.6 |
| reference | realistic-suite-svg-calibration-three-to-four-w960-r2 | d | 155 | 0.000 | 0.000 | 0.00 | {'interior': 152, 'clamped_to_half_pixel': 3} | 9.6 |
| reference | realistic-svg-six-node-measurement | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 12.9 |
| reference | realistic-svg-six-node-measurement | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 12.9 |
| reference | realistic-svg-six-node-measurement | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 12.9 |
| reference | realistic-svg-six-node-measurement | d | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 12.9 |
| reference | realistic-svg-six-node-measurement | e | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 12.9 |
| reference | realistic-svg-six-node-measurement | f | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 12.9 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r1 | a | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 14.6 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r1 | b | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 14.6 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r1 | c | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 14.6 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r1 | d | 25 | 0.000 | 0.000 | 0.00 | {'interior': 25} | 14.6 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r2 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.1 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r2 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.1 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r2 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.1 |
| before | realistic-suite-svg-calibration-four-to-three-w960-r2 | d | 25 | 0.000 | 0.000 | 0.00 | {'interior': 25} | 14.1 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r1 | a | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 9.5 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r1 | b | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 9.5 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r1 | c | 180 | 0.000 | 0.000 | 0.00 | {'interior': 175, 'clamped_to_half_pixel': 5} | 9.5 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r1 | d | 155 | 0.000 | 0.000 | 0.00 | {'interior': 153, 'clamped_to_half_pixel': 2} | 9.5 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r2 | a | 181 | 0.000 | 0.000 | 0.00 | {'interior': 181} | 9.5 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r2 | b | 181 | 0.000 | 0.000 | 0.00 | {'interior': 181} | 9.5 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r2 | c | 181 | 0.000 | 0.000 | 0.00 | {'interior': 175, 'clamped_to_half_pixel': 6} | 9.5 |
| before | realistic-suite-svg-calibration-three-to-four-w960-r2 | d | 156 | 0.000 | 0.000 | 0.00 | {'interior': 154, 'clamped_to_half_pixel': 2} | 9.5 |
| before | realistic-svg-six-node-measurement | a | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.5 |
| before | realistic-svg-six-node-measurement | b | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.5 |
| before | realistic-svg-six-node-measurement | c | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.5 |
| before | realistic-svg-six-node-measurement | d | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.5 |
| before | realistic-svg-six-node-measurement | e | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.5 |
| before | realistic-svg-six-node-measurement | f | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.5 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r1 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.1 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r1 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.1 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r1 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.1 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r1 | d | 25 | 0.000 | 0.000 | 0.00 | {'interior': 25} | 14.1 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r2 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.6 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r2 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.6 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r2 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 14.6 |
| original | realistic-suite-svg-calibration-four-to-three-w960-r2 | d | 25 | 0.000 | 0.000 | 0.00 | {'interior': 25} | 14.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r1 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 9.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r1 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 9.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r1 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 168, 'clamped_to_half_pixel': 11} | 9.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r1 | d | 154 | 0.000 | 0.000 | 0.00 | {'interior': 153, 'clamped_to_half_pixel': 1} | 9.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r2 | a | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 9.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r2 | b | 179 | 0.000 | 0.000 | 0.00 | {'interior': 179} | 9.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r2 | c | 179 | 0.000 | 0.000 | 0.00 | {'interior': 169, 'clamped_to_half_pixel': 10} | 9.6 |
| original | realistic-suite-svg-calibration-three-to-four-w960-r2 | d | 154 | 0.000 | 0.000 | 0.00 | {'interior': 153, 'clamped_to_half_pixel': 1} | 9.6 |
| original | realistic-svg-six-node-measurement | a | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.6 |
| original | realistic-svg-six-node-measurement | b | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.6 |
| original | realistic-svg-six-node-measurement | c | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.6 |
| original | realistic-svg-six-node-measurement | d | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.6 |
| original | realistic-svg-six-node-measurement | e | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.6 |
| original | realistic-svg-six-node-measurement | f | 180 | 0.000 | 0.000 | 0.00 | {'interior': 180} | 12.6 |
