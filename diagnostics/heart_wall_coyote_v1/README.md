# Post-hoc Heart wall/coyote diagnostic

This engineering diagnostic follows the completed `heart-02` cohort. It does not
replace, relax, or rerun registered acceptance. Original controller bytes and
selected candidate bytes are copied unchanged. Only this new scene/script is
added to each disposable project, at the already supported integrity scene path.
The frozen integrity adapter and registered projects are untouched.

The scene uses a static ledge, a nearby static wall, and action-system inputs.
After an ordinary spawn, it never assigns character position, velocity, timers,
or jump flags and never directly calls the controller's movement functions.
Both directions are checked. Two wall-only cases supply controls. A case is
valid only when the recorded jump edge has the declared airborne wall contact;
overlap cases additionally require observed prior floor loss and a live native
coyote timer. Input schedules, case boundaries and source hashes are declared
before launching. Every physics step is mapped to a completed draw.

The result describes input-only reachability **in this controlled geometry**.
It does not prove this state occurs in the game's original normal map or that
every possible wall state is preserved. Native motor values are private
diagnostic evidence; they are not inputs to model fitting or Kiro.

`run.py` requires a terminal cohort and all six terminal native launches. Godot
uses the existing private desktop transport and exclusive runtime lock. Each
output directory must be new. Existing failed qualifications remain intact.

Example from the repository root:

```powershell
.venv-kiro/Scripts/python.exe diagnostics/heart_wall_coyote_v1/run.py --label original --output artifacts/phase2c/diagnostics/wall-coyote-native-01/original
```

Qualify the original fixture before comparing selected jump controllers `01`,
`02`, and `03` under the same schedule. A context failure is `invalid_fixture`,
not evidence for or against movement preservation. Compare native launch and
following four-step vertical/horizontal responses, retaining all raw records,
checkpoint PNGs, private-runtime receipts, and source hashes.

## Observed result (2026-09-21)

The original controller qualified on the first schedule: walking began at step
10, native floor contact was lost at step 32, and jump was pressed at step 35.
At the jump edge, both mirrored overlap cases were airborne against a wall with
0.05 seconds remaining on the real coyote timer. All four controllers had
exactly equal recorded pre-jump states in each case.

| Selected jump controller | Overlap right/left launch vertical speed | Overlap five-step response | Wall-only controls |
|---|---:|---|---|
| Original | -250 | Baseline | Baseline |
| Chain 01 / candidate-03 | -194.681518554688 | Changed | Preserved |
| Chain 02 / candidate-02 | -250 | Preserved | Preserved |
| Chain 03 / candidate-03 | -195.179428100586 | Changed | Preserved |

The five samples are the jump physics step and four following steps; comparison
uses exact recorded velocity and position equality. The values above are Godot
float observations, not inferred source constants. Fixture validity and private
runtime proof passed for all 16 cases. This confirms the conditional wall/coyote
regression in chains 01 and 03 and its absence in these four cases for chain 02.
It makes no broader universal preservation claim.

Evidence is in `artifacts/phase2c/diagnostics/wall-coyote-native-01/summary.json`
(SHA256 `3c8dc73e542e2159c31f1043862fa6e6a190e6e474622bdeac735516ca5eba74`).
Each run contains immutable source maps, protocol declaration, raw observations,
checkpoint images, and private-process receipts. `final-source-proof.json`
records a successful replay of all 20 registered protocol source hashes,
qualification inputs, original source and prepared baseline after this diagnostic.
Historical cohort results and source reviews were not overwritten.

For a fresh repeat, choose a new output folder. The following runs must stay
sequential and must not overlap another native cohort:

```powershell
.venv-kiro/Scripts/python.exe diagnostics/heart_wall_coyote_v1/run.py --label original --output artifacts/phase2c/diagnostics/wall-coyote-native-02/original
.venv-kiro/Scripts/python.exe diagnostics/heart_wall_coyote_v1/run.py --label 01 --output artifacts/phase2c/diagnostics/wall-coyote-native-02/chain01
.venv-kiro/Scripts/python.exe diagnostics/heart_wall_coyote_v1/run.py --label 02 --output artifacts/phase2c/diagnostics/wall-coyote-native-02/chain02
.venv-kiro/Scripts/python.exe diagnostics/heart_wall_coyote_v1/run.py --label 03 --output artifacts/phase2c/diagnostics/wall-coyote-native-02/chain03
.venv-kiro/Scripts/python.exe diagnostics/heart_wall_coyote_v1/compare.py artifacts/phase2c/diagnostics/wall-coyote-native-02
```

Stop if the original fixture is invalid. Do not interpret a contact/timing error
as a controller failure or adjust the registered experiment to fit this result.
