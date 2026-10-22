# Supplemental movement probe, revision 2

The air probe removes the upper platform after rendered frame 29, keeps input
released through frame 31, and holds right on frames [32, 46). The fixed private
pre/post oracle observes the original player callback at frame 32. It requires
natural airborne Idle at horizontal rest before that callback and the same
condition in the preceding post-player observation. The original controller
must then enter Fall without changing horizontal velocity on that tick.

Revision 1 used [31, 45). The unchanged parent qualification in
`artifacts/model-trials/preflight/probes-01/parent-air/capture` remained grounded
before frame 31 because deferred platform removal needed another contact update.
Its `probe_invalid` evidence is preserved. Revision 2 is a fixed pre-model
qualification correction, not a dynamically selected frame or controller change.

The combined run/jump/land/brake schedule remains unchanged: right [5, 90),
jump [30, 54). Both probes retain 60 Hz, 60 warmup ticks and 240 native PNGs.
