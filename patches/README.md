# Target-controller patches

`kiroclops.godot_patch.apply_profile(project_path, profile, body_height_px)`
returns a unified diff for the actual SlayHorizon player and normal-jump state.
The experiment runner saves that returned diff here with the fitted profile.

The helper never opens Tiny Crate's source. It converts the measured body-unit
profile using target body height measured from images. Fixture and normal-level
runs load the same patched `Player.tscn`. Original horizontal, double-jump, and
wall-jump parameters remain in place; fitted gravity applies only during the
normal jump and its subsequent fall.
