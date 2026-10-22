# Airborne response contract v1

This is a separately registered supplemental integrity test. It does not change
canonical motion trajectories, thresholds, three repeats, two spawn positions,
or the fresh-process rule for those motion captures.

One fresh private-desktop Godot process produces 240 native 640x360 PNGs at one
rendered frame per 60 Hz physics step, following 60 warmup steps. Eighteen actors
are freshly instantiated in sequence: Idle, Run, Jump; rising or falling; neutral,
left or right. Each gets two actual physics steps to refresh contact flags in an
open-air fixture, public state entry, fixed private identical motion seeds, then
four measured callback steps. Cases occupy eight capture ticks each. Seed and
input staging occur after draw offset2; pre-observer priority-100, real player
priority0 and post-observer priority100 observe offsets3..6. The observer never
calls move() or replaces the production controller.

Seeds use registered fixture constants, not candidate fitting: vertical motion
is +/-300 world units/s; horizontal rest for Idle and +35 for Run/Jump. These
values stay inside the integrity harness. Epsilon1e-6 only labels rest/unchanged
predicates. Pre/post horizontal velocity is serialized solely as SHA256 of its
native float32 little-endian bytes, permitting exact differential equality with
no raw velocity or position feedback. Boolean predicates, actual state names,
input schedule, timing, complete source/config/runtime and PNG hashes are bound.

Every tested precondition must be valid in both suites. Any unexpected contact,
wrong seeded state/direction/rest, incomplete output, source/hash drift or failed
private process invalidates the probe; it cannot become a behavioral pass.
Comparison publishes case/step and differing field names, never kinematic data.

These are seeded controller contract states. A difference is a compatibility
counterexample over that registered domain; it is not proof that the state is
naturally reachable in the original map. Existing normal/jump/wall/floor-removal
checks and independent source review remain necessary. Four ticks in eighteen
cases do not prove every airborne state/input combination.

An optional existing import_cache_v1 binding seeds only exact engine/source-
bound texture products before installing the integrity adapter; normal headless
editor import always runs for the new controller. Captures are never reused.
