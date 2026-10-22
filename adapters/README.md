# Godot capture adapters

Copy `godot3/` into the Tiny Crate work copy as `kiroclops_capture/`, or
`godot4/` into the SlayHorizon work copy at that same relative path. Launch the
copied `res://kiroclops_capture/Capture.tscn` with `--fixed-fps 60`. Use the
ordinary rendering executable, GLES2 for Tiny and GL Compatibility for Slay.
The root runner in `kiroclops/capture.py` performs these steps.

Set `KIROCLOPS_CONFIG` to an absolute JSON file containing:

```json
{
  "output_dir": "D:/absolute/capture/path",
  "hold_ticks": 3,
  "start_tick": 30,
  "warmup_ticks": 60,
  "frame_count": 240,
  "spawn_offset": 0,
  "normal_level": false
}
```

The game physics rate must already be 60 Hz. The adapter refuses a different
rate instead of changing the controller's physical behavior. The native
recording sizes are Tiny 228×128 and Slay 640×360. Every post-draw callback
writes one `frames/000000.png`-style file and a matching physics-tick entry;
missing/duplicate ticks fail the recording. `recording.json` contains only
render-frame timing, requested actions, dimensions, completion and settling
status. It never contains character position, velocity, or collider geometry.

## Input phase matters

Godot's `Input.action_press()` stamps `just_pressed` for the **next** physics
frame. Therefore transitions are issued after the preceding frame is drawn,
not inside the intended physics callback. For a press at capture tick 30 and
a hold of 3, pressed frames are exactly `[30, 33)`. This prevents Slay's Idle
state seeing the held key on one tick and its Jump state seeing the same press
edge on the following tick, which otherwise accidentally triggers double-jump.

The harness validates stationary floor contact during warmup. The post-draw
callback records the completed physics tick. Sprite animation remains active.
There are no replayed coordinates or controller-specific jump calculations in
either adapter.

## Original scenes and isolated settings

Tiny fixtures instantiate its real `src/actor/Player.tscn` and its
`src/stage/SolidTileMap.tscn`. Tiny performs its own pixel collisions, so a
generic StaticBody2D cannot replace that TileMap. Normal mode loads `1-1.tscn`.
Slay fixtures instantiate its real Player and reuse the normal map's TileSet;
normal mode loads `map_demo.tscn`. Both modes use the same movement source.
Only scene setup, fixed cameras and scheduled inputs differ.

Before starting an engine, the runner must isolate APPDATA and LOCALAPPDATA
inside the workspace and set a distinct `application/config/custom_user_dir_name`
in each disposable work project (`KiroClops/tiny`, `KiroClops/slay-before`, or
`KiroClops/slay-after`). Tiny's original autoload reads/writes options during
startup, before the capture scene can run. The adapter prints
`KIROCLOPS_USER_DIR` so the resolved path is auditable.

Godot 4 can import with `--headless --editor --import --quit`. Godot 3's immediate
`--editor --quit` can abort its importer too early: start its hidden editor,
wait until all `dest_files` referenced by the project's `.import` sidecars
exist and the import output has stabilized, then close that editor process.
The pinned Tiny project includes 87 import sidecars. Its original splash scene
also emits resource-cleanup warnings on exit with Godot 3.6; these were reproduced
without the capture adapter (`artifacts/agent-native-tiny.err`).

## Regression-only scenarios

The Godot 4 adapter additionally accepts `extra_inputs` with entries such as
`{"tick":40,"action":"ui_up","pressed":true}` and optional `smoke_wall:true`.
These are used for walking, double-jump and wall-jump regression checks, never
for calibration or held-out jump measurement. All active actions appear in
the recording metadata. State transitions are debug text in the engine log;
the visual analyzer does not consume these logs.

Test the target patch interface with:

```text
.venv/Scripts/python.exe -m unittest adapters.test_godot_patch -v
```
