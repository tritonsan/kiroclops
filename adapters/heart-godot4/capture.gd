extends Node

# Real Heart controller, logical inputs, post-draw PNGs. No movement patches.
# This node runs while paused only to observe the original level countdown;
# capture/active warmup ticks advance only when the player can actually process.
const FPS := 60
const WARMUP := 60
const FRAME_COUNT := 240
const ACTIONS := {"left": "move_left", "right": "move_right", "jump": "jump"}
var config: Dictionary = {}
var output_dir := ""
var player: CharacterBody2D
var tick := -1
var last_draw_tick := -1
var frames: Array = []
var stable_ticks := 0
var previous_position := Vector2.ZERO
var settled := false
var finished := false
var normal_level := false
var observations: Array = []
var hazard_entries := 0
var observed_position := Vector2.ZERO
var observed_floor := false


func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	process_physics_priority = -100
	set_physics_process(false)
	var file := FileAccess.open(OS.get_environment("KIROCLOPS_CONFIG"), FileAccess.READ)
	if file == null:
		_finish("error", "Missing capture configuration")
		return
	var parsed = JSON.parse_string(file.get_as_text())
	file.close()
	if not parsed is Dictionary:
		_finish("error", "Invalid capture configuration")
		return
	config = parsed
	output_dir = str(config.get("output_dir", ""))
	normal_level = bool(config.get("normal_level", false))
	if output_dir.is_empty() or int(config.get("warmup_ticks", 0)) != WARMUP or int(config.get("frame_count", 0)) != FRAME_COUNT or not config.get("events", []) is Array:
		_finish("error", "Unexpected capture protocol")
		return
	if Engine.physics_ticks_per_second != FPS:
		_finish("error", "Expected original 60 Hz physics")
		return
	if DirAccess.make_dir_recursive_absolute(output_dir.path_join("frames")) != OK:
		_finish("error", "Cannot create native PNG directory")
		return
	DisplayServer.window_set_size(Vector2i(320, 180))
	get_tree().root.content_scale_size = Vector2i(320, 180)
	for action in InputMap.get_actions():
		Input.action_release(action)
		InputMap.action_erase_events(action)
	for action in ACTIONS.values():
		if not InputMap.has_action(action):
			_finish("error", "Original Heart action missing")
			return
	seed(0)
	AudioServer.set_bus_mute(0, true)
	if normal_level:
		# Preserve world.gd, its countdown, hazards, camera and original player.
		var level = load("res://level_one.tscn").instantiate()
		level.process_mode = Node.PROCESS_MODE_PAUSABLE
		add_child(level)
		player = level.get_node("Player")
		player.position.x += float(config.get("spawn_offset", 0))
	else:
		# Reuse original 16px TileSet/collision polygons, laying a flat fixture.
		var floor_map = load("res://level_tile_map.tscn").instantiate()
		floor_map.clear()
		for x in range(-64, 65):
			for y in range(10, 13):
				floor_map.set_cell(0, Vector2i(x, y), 0, Vector2i(0, 0))
		add_child(floor_map)
		player = load("res://player.tscn").instantiate()
		player.process_mode = Node.PROCESS_MODE_PAUSABLE
		player.position = Vector2(112 + float(config.get("spawn_offset", 0)), 140)
		add_child(player)
		player.get_node("Camera2D").enabled = false
		var camera := Camera2D.new()
		camera.position = Vector2(160, 90)
		camera.position_smoothing_enabled = false
		add_child(camera)
		camera.make_current()
	previous_position = player.position
	observed_position = player.global_position
	player.get_node("HazardDetector").area_entered.connect(_on_hazard_entered)
	print("KIROCLOPS_USER_DIR=", OS.get_user_data_dir())
	print("KIROCLOPS_DISPLAY_SERVER=", DisplayServer.get_name())
	RenderingServer.frame_post_draw.connect(_on_frame_post_draw)
	set_physics_process(true)


func _physics_process(delta: float) -> void:
	if finished or get_tree().paused or not player.can_process():
		return
	if absf(delta - 1.0 / FPS) > 0.000001:
		_finish("error", "Physics delta differs from 60 Hz")
		return
	tick += 1
	if tick < WARMUP:
		if player.is_on_floor() and player.position.is_equal_approx(previous_position):
			stable_ticks += 1
		else:
			stable_ticks = 0
		previous_position = player.position
	elif tick == WARMUP:
		settled = stable_ticks >= 5
		if not settled:
			_finish("error", "Real player did not settle during 60 active physics ticks")


func _active_actions() -> Array:
	var result: Array = []
	for action in ["jump", "left", "right"]:
		if Input.is_action_pressed(ACTIONS[action]):
			result.append(action)
	return result


func _stage_next_input() -> void:
	var next_tick := tick + 1 - WARMUP
	for pressed in [false, true]:
		for event in config["events"]:
			if int(event.get("tick", -1)) != next_tick or bool(event.get("pressed", false)) != pressed:
				continue
			var action := str(event.get("action", ""))
			if not ACTIONS.has(action):
				_finish("error", "Unknown logical input")
				return
			if pressed:
				Input.action_press(ACTIONS[action])
			else:
				Input.action_release(ACTIONS[action])


func _on_frame_post_draw() -> void:
	if finished or get_tree().paused or tick < 0:
		return
	# Every active physics update must have exactly one rendered frame, including
	# warmup. Paused countdown frames are deliberately outside this tick clock.
	if tick != last_draw_tick + 1:
		_finish("error", "Render/active-physics mapping is not one-to-one")
		return
	last_draw_tick = tick
	if tick >= WARMUP:
		var index := tick - WARMUP
		if index != frames.size():
			_finish("error", "Non-contiguous frame index")
			return
		var image := get_viewport().get_texture().get_image()
		if image.get_width() != 320 or image.get_height() != 180:
			_finish("error", "Native viewport is not 320x180")
			return
		if image.save_png(output_dir.path_join("frames/%06d.png" % index)) != OK:
			_finish("error", "Native PNG write failed")
			return
		frames.append({"frame_index": index, "physics_tick": tick, "time_s": float(index) / FPS, "actions": _active_actions()})
		var moving_horizontal := absf(player.velocity.x) > 0.000001
		var slope_direction := player.get_floor_normal().x * signf(player.velocity.x) if player.is_on_floor() else 0.0
		observations.append({"frame_index": index, "physics_tick": tick,
			"on_floor": player.is_on_floor(), "on_wall": player.is_on_wall(),
			"moving_up": player.velocity.y < 0.0, "moving_horizontal": moving_horizontal,
			"jump_edge": Input.is_action_just_pressed("jump"), "hazard_entries": hazard_entries,
			"ground_incline": player.is_on_floor() and moving_horizontal and slope_direction < -0.001,
			"ground_decline": player.is_on_floor() and moving_horizontal and slope_direction > 0.001,
			"ground_rise": observed_floor and player.is_on_floor() and player.global_position.y < observed_position.y - 0.000001,
			"ground_descent": observed_floor and player.is_on_floor() and player.global_position.y > observed_position.y + 0.000001})
		if frames.size() == FRAME_COUNT:
			_finish("ok", "")
			return
	observed_position = player.global_position
	observed_floor = player.is_on_floor()
	# Input edges belong to the next physics update, never the current callback.
	_stage_next_input()


func _on_hazard_entered(_area: Area2D) -> void:
	hazard_entries += 1


func _finish(status: String, error: String) -> void:
	if finished:
		return
	finished = true
	for action in ACTIONS.values():
		Input.action_release(action)
	var result := {"status": status, "fps": FPS, "viewport": [320, 180],
		"frame_count": frames.size(), "frames": frames, "settled": settled,
		"active_warmup_ticks": WARMUP if settled else mini(tick + 1, WARMUP),
		"normal_level": normal_level, "observations": observations}
	if not error.is_empty():
		result["error"] = error
		push_error(error)
	if not output_dir.is_empty():
		var file := FileAccess.open(output_dir.path_join("recording.json"), FileAccess.WRITE)
		if file != null:
			file.store_string(JSON.stringify(result, "  "))
			file.close()
	if RenderingServer.frame_post_draw.is_connected(_on_frame_post_draw):
		RenderingServer.frame_post_draw.disconnect(_on_frame_post_draw)
	get_tree().call_deferred("quit", 0 if status == "ok" else 2)
