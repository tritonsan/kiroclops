extends Node

# Ground experiment: only the declared action schedule is applied.
# Original character physics stay unchanged; only rendered pixels leave this adapter.
const FPS := 60
var config: Dictionary = {}
var output_dir := ""
var player: Node2D
var tick := -1
var capture_tick := -1
var last_draw_tick := -2
var frames: Array = []
var settled := false
var finished := false
var recording_started := false
var warmup := 60
var frame_count := 240
var stable_ticks := 0
var previous_position := Vector2.ZERO
var last_debug_state := ""

func _ready() -> void:
	print("KIROCLOPS_USER_DIR=", OS.get_user_data_dir())
	process_physics_priority = -100
	set_physics_process(false)
	var path := OS.get_environment("KIROCLOPS_CONFIG")
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		push_error("Cannot read KIROCLOPS_CONFIG: " + path)
		get_tree().quit(2)
		return
	var parsed = JSON.parse_string(file.get_as_text())
	file.close()
	if not parsed is Dictionary:
		push_error("Invalid capture JSON")
		get_tree().quit(2)
		return
	config = parsed
	output_dir = str(config.get("output_dir", ""))
	warmup = int(config.get("warmup_ticks", 60))
	frame_count = int(config.get("frame_count", 240))
	if output_dir.is_empty() or warmup != 60 or frame_count != 240 or not config.get("events", []) is Array:
		_finish("error", "Invalid capture configuration")
		return
	if DirAccess.make_dir_recursive_absolute(output_dir.path_join("frames")) != OK:
		_finish("error", "Cannot create output frames directory")
		return
	if Engine.physics_ticks_per_second != FPS:
		_finish("error", "Expected unchanged 60 Hz project physics")
		return
	DisplayServer.window_set_size(Vector2i(640, 360))
	for action in InputMap.get_actions():
		Input.action_release(action)
		InputMap.action_erase_events(action)
	seed(0)
	if bool(config.get("normal_level", false)):
		var map = load("res://source/maps/map_scenes/map_demo.tscn").instantiate()
		add_child(map)
		player = map.get_node("Player")
		player.position.x += float(config.get("spawn_offset", 0))
		map.get_node("MapCamera").enabled = false
	else:
		# Reuse the original map's TileSet and collision data without running
		# its hazards, moving platforms, transitions, or collectibles.
		var scene_state = load("res://source/maps/map_scenes/map_demo.tscn").get_state()
		var floor_map := TileMapLayer.new()
		for node_index in range(scene_state.get_node_count()):
			if scene_state.get_node_name(node_index) == "MapLayer":
				for property_index in range(scene_state.get_node_property_count(node_index)):
					if scene_state.get_node_property_name(node_index, property_index) == "tile_set":
						floor_map.tile_set = scene_state.get_node_property_value(node_index, property_index)
		if floor_map.tile_set == null:
			floor_map.free()
			_finish("error", "Original map TileSet was not found")
			return
		for x in range(0, 40):
			for y in range(18, 23):
				floor_map.set_cell(Vector2i(x, y), 0, Vector2i(0, 0))
		# Optional regression-only wall. Calibration never enables this.
		if bool(config.get("smoke_wall", false)):
			for y in range(3, 18):
				floor_map.set_cell(Vector2i(23, y), 0, Vector2i(0, 0))
		add_child(floor_map)
		player = load("res://source/characters/player/player.tscn").instantiate()
		player.position = Vector2(320 + float(config.get("spawn_offset", 0)), 268)
		add_child(player)
	# Player intentionally waits for Main's transition in the normal game.
	# A fixture enters its existing Fall state, then lets original physics settle.
	player.show()
	player.states.Fall.enter()
	var camera := Camera2D.new()
	camera.position = Vector2(320, 180)
	camera.position_smoothing_enabled = false
	add_child(camera)
	camera.make_current()
	AudioServer.set_bus_mute(0, true)
	previous_position = player.position
	RenderingServer.frame_post_draw.connect(_on_frame_post_draw)
	set_physics_process(true)

func _physics_process(_delta: float) -> void:
	if finished:
		return
	tick += 1
	if player.state_callback:
		var state_name: String = player.state_callback.get_object().name
		if state_name != last_debug_state:
			print("KIROCLOPS_STATE physics_tick=", tick, " state=", state_name)
			last_debug_state = state_name
	if tick < warmup:
		if player.is_on_floor() and player.position.is_equal_approx(previous_position):
			stable_ticks += 1
		else:
			stable_ticks = 0
		previous_position = player.position
		return
	if not recording_started:
		settled = stable_ticks >= 5
		if not settled:
			_finish("error", "Character did not settle during warmup")
			return
		recording_started = true
	capture_tick = tick - warmup

func _action_map():
	return {"left": "ui_left", "right": "ui_right", "jump": "ui_up"}

func _stage_next_input():
	# Stage edges after draw for the NEXT physics frame. Release before press
	# makes a same-tick reversal unambiguous, without overlapping directions.
	var next_capture_tick = tick + 1 - warmup
	var mapping = _action_map()
	for want_pressed in [false, true]:
		for event in config.get("events", []):
			if int(event.get("tick", -1)) != next_capture_tick or bool(event.get("pressed", false)) != want_pressed:
				continue
			var action = str(event.get("action", ""))
			if not mapping.has(action):
				_finish("error", "Unknown logical input action")
				return
			if want_pressed:
				if (action == "left" and Input.is_action_pressed(mapping["right"])) or (action == "right" and Input.is_action_pressed(mapping["left"])):
					_finish("error", "Simultaneous left/right input")
					return
				Input.action_press(mapping[action])
			else:
				Input.action_release(mapping[action])

func _active_actions():
	var actions = []
	var mapping = _action_map()
	for action in ["jump", "left", "right"]:
		if Input.is_action_pressed(mapping[action]):
			actions.append(action)
	return actions

func _on_frame_post_draw() -> void:
	if finished:
		return
	if not recording_started:
		_stage_next_input()
		return
	if last_draw_tick != -2 and tick != last_draw_tick + 1:
		_finish("error", "Rendered frames did not map one-to-one to physics ticks")
		return
	if capture_tick != frames.size():
		_finish("error", "Non-contiguous capture tick")
		return
	var img := get_viewport().get_texture().get_image()
	if img.get_width() != 640 or img.get_height() != 360:
		_finish("error", "Unexpected viewport dimensions")
		return
	var save_error := img.save_png(output_dir.path_join("frames/%06d.png" % capture_tick))
	if save_error != OK:
		_finish("error", "PNG write failed")
		return
	frames.append({"frame_index": capture_tick, "physics_tick": tick, "time_s": float(capture_tick) / FPS, "actions": _active_actions()})
	last_draw_tick = tick
	if frames.size() == frame_count:
		_finish("ok", "")
	else:
		_stage_next_input()

func _finish(status: String, error: String) -> void:
	if finished:
		return
	finished = true
	for action in _action_map().values():
		Input.action_release(action)
	var result := {"status": status, "fps": FPS, "viewport": [640, 360], "frame_count": frames.size(), "frames": frames, "settled": settled}
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
