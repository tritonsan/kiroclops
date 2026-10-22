# V6 methods against independent reference positions

Reference: the `raw` position of each point of the frozen numerical calibration reviews, extracted from raw pixels without production coordinates, events, DOM or smoothing. Offsets are method minus reference, in pixels.
Measuring code: live `kiroclops` identical to the V6 snapshot `c8c90f77d535…`: True.

## panel (117 points, floor 2.0 px)

| method | measured | bias x | bias y | scatter | median | p95 | max | within floor |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| accepted | 117/117 | 0.00 | 0.00 | 0.86 | 1.04 | 1.06 | 1.10 | 117 |
| template_integer | 117/117 | 0.01 | -0.18 | 0.41 | 0.38 | 0.87 | 1.06 | 117 |
| template_parabolic_v1 | 117/117 | 0.01 | -0.17 | 0.24 | 0.07 | 0.56 | 0.56 | 117 |
| template_upsampled_v1 | 117/117 | 0.00 | -0.17 | 0.24 | 0.05 | 0.54 | 0.55 | 117 |
| features_v1 | 91/117 | -0.08 | 0.21 | 0.42 | 0.35 | 0.85 | 1.17 | 91 |

features_v1 not located: {'matcher_engine_failure': 21, 'no_consistent_similarity': 1, 'too_few_matches': 4}

## svg (216 points, floor 2.0 px)

| method | measured | bias x | bias y | scatter | median | p95 | max | within floor |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| accepted | 216/216 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 216 |
| template_integer | 216/216 | -0.31 | -0.56 | 0.31 | 0.58 | 0.98 | 1.26 | 216 |
| template_parabolic_v1 | 216/216 | -0.32 | -0.55 | 0.26 | 0.58 | 0.91 | 0.98 | 216 |
| template_upsampled_v1 | 216/216 | -0.32 | -0.55 | 0.26 | 0.58 | 0.91 | 0.98 | 216 |
| features_v1 | 212/216 | -0.26 | -0.50 | 0.29 | 0.60 | 0.91 | 1.06 | 212 |

features_v1 not located: {'no_consistent_similarity': 3, 'too_few_matches': 1}

## Blind focus-ring endpoint marks (SVG)

32 blind marks (blind marker (Sonnet 5.5 subagent)): production within 2 px 32/32, attachment decision agreement 32/32; status `pass`. V6 reproduces these observations bit for bit (artifacts/phase4-v6/equivalence-01), so the score holds unchanged.
