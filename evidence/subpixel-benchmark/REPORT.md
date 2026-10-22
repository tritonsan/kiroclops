# V6 sub-pixel benchmark (synthetic bilinear shifts)

OpenCV 5.0.0; shifts [(0.0, 0.0), (0.1, 0.0), (0.2, 0.0), (0.3, 0.0), (0.4, 0.0), (0.5, 0.0), (0.25, -0.25), (-0.35, 0.15), (0.45, 0.45), (-0.1, -0.4)]; repeats 3.

| texture | noise σ | gamma | method | valid/trials | RMSE px | max px | bias x | bias y |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| card_with_text | 0.0 | 1.0 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| card_with_text | 0.0 | 1.0 | parabolic_v1 | 30/30 | 0.076 | 0.136 | -0.026 | 0.008 |
| card_with_text | 0.0 | 1.0 | upsampled_v1 | 30/30 | 0.051 | 0.094 | -0.018 | 0.004 |
| card_with_text | 0.0 | 1.15 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| card_with_text | 0.0 | 1.15 | parabolic_v1 | 30/30 | 0.075 | 0.135 | -0.025 | 0.008 |
| card_with_text | 0.0 | 1.15 | upsampled_v1 | 30/30 | 0.05 | 0.089 | -0.017 | 0.004 |
| card_with_text | 2.0 | 1.0 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| card_with_text | 2.0 | 1.0 | parabolic_v1 | 30/30 | 0.076 | 0.136 | -0.026 | 0.008 |
| card_with_text | 2.0 | 1.0 | upsampled_v1 | 30/30 | 0.051 | 0.094 | -0.017 | 0.004 |
| card_with_text | 2.0 | 1.15 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| card_with_text | 2.0 | 1.15 | parabolic_v1 | 30/30 | 0.075 | 0.135 | -0.026 | 0.008 |
| card_with_text | 2.0 | 1.15 | upsampled_v1 | 30/30 | 0.05 | 0.088 | -0.017 | 0.003 |
| card_with_text | 5.0 | 1.0 | integer | 30/30 | 0.373 | 0.636 | -0.142 | 0.005 |
| card_with_text | 5.0 | 1.0 | parabolic_v1 | 30/30 | 0.076 | 0.138 | -0.026 | 0.011 |
| card_with_text | 5.0 | 1.0 | upsampled_v1 | 30/30 | 0.052 | 0.097 | -0.018 | 0.004 |
| card_with_text | 5.0 | 1.15 | integer | 30/30 | 0.373 | 0.636 | -0.108 | 0.005 |
| card_with_text | 5.0 | 1.15 | parabolic_v1 | 30/30 | 0.075 | 0.135 | -0.026 | 0.009 |
| card_with_text | 5.0 | 1.15 | upsampled_v1 | 30/30 | 0.05 | 0.088 | -0.017 | 0.003 |
| smooth_random | 0.0 | 1.0 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| smooth_random | 0.0 | 1.0 | parabolic_v1 | 30/30 | 0.055 | 0.114 | -0.009 | -0.002 |
| smooth_random | 0.0 | 1.0 | upsampled_v1 | 30/30 | 0.038 | 0.071 | -0.007 | -0.005 |
| smooth_random | 0.0 | 1.15 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| smooth_random | 0.0 | 1.15 | parabolic_v1 | 30/30 | 0.055 | 0.104 | -0.012 | -0.002 |
| smooth_random | 0.0 | 1.15 | upsampled_v1 | 30/30 | 0.039 | 0.065 | -0.009 | -0.004 |
| smooth_random | 2.0 | 1.0 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| smooth_random | 2.0 | 1.0 | parabolic_v1 | 30/30 | 0.054 | 0.103 | -0.01 | -0.002 |
| smooth_random | 2.0 | 1.0 | upsampled_v1 | 30/30 | 0.038 | 0.069 | -0.008 | -0.004 |
| smooth_random | 2.0 | 1.15 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| smooth_random | 2.0 | 1.15 | parabolic_v1 | 30/30 | 0.054 | 0.104 | -0.011 | -0.002 |
| smooth_random | 2.0 | 1.15 | upsampled_v1 | 30/30 | 0.039 | 0.069 | -0.008 | -0.004 |
| smooth_random | 5.0 | 1.0 | integer | 30/30 | 0.373 | 0.636 | -0.142 | 0.005 |
| smooth_random | 5.0 | 1.0 | parabolic_v1 | 30/30 | 0.056 | 0.111 | -0.012 | 0.001 |
| smooth_random | 5.0 | 1.0 | upsampled_v1 | 30/30 | 0.04 | 0.074 | -0.009 | -0.005 |
| smooth_random | 5.0 | 1.15 | integer | 30/30 | 0.373 | 0.636 | -0.075 | 0.005 |
| smooth_random | 5.0 | 1.15 | parabolic_v1 | 30/30 | 0.059 | 0.12 | -0.013 | -0.005 |
| smooth_random | 5.0 | 1.15 | upsampled_v1 | 30/30 | 0.042 | 0.083 | -0.01 | -0.007 |

The integer method cannot do better than half a pixel by construction. Bias is mean signed error against the known shift.
