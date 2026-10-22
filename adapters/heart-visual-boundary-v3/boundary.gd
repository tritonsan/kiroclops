extends "res://kiroclops_heart/capture.gd"

# This scene changes fixture geometry only. The real character script, sprite,
# movement resources, input staging and post-draw recorder remain untouched.
func _ready() -> void:
	super._ready()
	if finished:
		return
	var side := str(config.get("wall_side", ""))
	if side != "left" and side != "right":
		_finish("error", "Visual fixture wall side missing")
		return
	var wall = load("res://level_tile_map.tscn").instantiate()
	wall.clear()
	var column := 4 if side == "left" else 12
	for row in range(4, 10):
		wall.set_cell(0, Vector2i(column, row), 0, Vector2i(0, 0))
	add_child(wall)
