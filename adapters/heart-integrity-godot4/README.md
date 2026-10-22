# Heart preservation fixture

This fixture instantiates the unchanged `player.tscn` and lets its real physics
callback run. Input actions are applied between rendered physics steps. Fixture
code runs before the actor; the observation node runs after it. No movement
callback, timer callback or player collision method is invoked by the fixture.

The dedicated integrity protocol is distinct from the 240-frame visual-motion
recording. After 60 warmup steps it records every physics/render correspondence:

| Stage | Cases | Physics steps | Native PNG checkpoints |
| --- | ---: | ---: | ---: |
| Jump transfer | 34 | 1512 | 150 |
| Ground transfer | 30 | 1032 | 126 |

Every case gets a fresh actor. Air cases seed one of three horizontal velocities,
upward/downward velocity and neutral/left/right input. Four observed steps per
case compare both velocity and actual displacement. Native contacts must remain
clear. These are 18 finite seeded cases, not a proof that every seed is reachable
in the normal level.

Other cases exercise ground motion, normal short/long jumps, second jump, rejected
third jump, landing and restored jump rights, wall contact/grace/expired grace in
both directions, coyote contact loss and real hazard-area entry. Removing a
fixture floor/wall establishes contact loss; timers are never manually started.
Normal-level and slope reachability are handled by the separate capture adapter.

The jump stage allows normal/coyote jump vertical changes. It compares the four
post-launch vertical responses and rights of the second jump, and wall responses
to the original Heart baseline. Since a redesigned first jump reaches a different
world height, exact second-jump displacement subtraction can differ by float32
rounding; the second-jump comparison uses velocity and rights. It does not claim
complete equality of the whole second-jump flight.

The ground stage compares normal vertical jump responses to the sealed jump-stage
parent. Air horizontal control, second jump, wall, coyote rights and hazard return
remain protected in both stages.

Only SHA256 digests of native float32 observations and boolean predicates are
serialized. The Python comparison publishes case names, failed predicates and
changed field names. It never supplies engine kinematics to image analysis,
motion fitting or the model's movement-parameter feedback.

All execution goes through `kiroclops.portable.runtime` on an owned, non-input
Windows desktop. Source copies, adapter code, recording configuration, native
observations, checkpoints and private-process receipts are bound by hashes.
`verify_checks` replays these bindings before accepting a saved result.
