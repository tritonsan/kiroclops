extends Node

# This adapter schedules input and records rendered pixels. It never supplies a
# trajectory, changes a movement parameter, or exports character coordinates.
const FPS = 60
var config = {}
var output_dir = ""
var player
var tick = -1
var capture_tick = -1
var last_draw_tick = -2
var frames = []
var settled = false
var finished = false
var recording_started = false
var pressed = false
var warmup = 60
var frame_count = 240
var start_tick = 30
var hold_ticks = 3
var stable_ticks = 0
var previous_position = Vector2.ZERO

func _ready():
	print("KIROCLOPS_USER_DIR=", OS.get_user_data_dir())
	process_priority = -100
	set_physics_process(false)
	var file = File.new()
	var path = OS.get_environment("KIROCLOPS_CONFIG")
	if file.open(path, File.READ) != OK:
		printerr("Cannot read KIROCLOPS_CONFIG: ", path)
		get_tree().quit(2)
		return
	var parsed = JSON.parse(file.get_as_text())
	file.close()
	if parsed.error != OK or typeof(parsed.result) != TYPE_DICTIONARY:
		printerr("Invalid capture JSON")
		get_tree().quit(2)
		return
	config = parsed.result
	output_dir = str(config.get("output_dir", ""))
	warmup = int(config.get("warmup_ticks", 60))
	frame_count = int(config.get("frame_count", 240))
	start_tick = int(config.get("start_tick", 30))
	hold_ticks = int(config.get("hold_ticks", 3))
	if output_dir.empty() or warmup < 10 or frame_count < 1 or hold_ticks < 1 or start_tick < 0 or start_tick + hold_ticks >= frame_count:
		_finish("error", "Invalid capture configuration")
		return
	var dir = Directory.new()
	if dir.make_dir_recursive(output_dir.plus_file("frames")) != OK:
		_finish("error", "Cannot create output frames directory")
		return
	if Engine.iterations_per_second != FPS:
		_finish("error", "Expected unchanged 60 Hz project physics")
		return
	# Disable physical bindings only in this disposable capture process.
	for action in InputMap.get_actions():
		Input.action_release(action)
		InputMap.action_erase_events(action)
	seed(0)
	Shared.is_level_select = false
	Shared.is_in_game = false
	Shared.is_creator = false
	if bool(config.get("normal_level", false)):
		var stage = load("res://src/map/1-1.tscn").instance()
		add_child(stage)
		player = stage.get_node("Actors/Player")
		player.position.x += int(config.get("spawn_offset", 0))
	else:
		var stage = Node2D.new()
		stage.name = "Stage"
		stage.set_script(load("res://src/stage/Stage.gd"))
		stage.palette = 0
		var floor_map = load("res://src/stage/SolidTileMap.tscn").instance()
		for x in range(-16, 16):
			for y in range(6, 8):
				floor_map.set_cell(x, y, 0)
		stage.add_child(floor_map)
		player = load("res://src/actor/Player.tscn").instance()
		player.position = Vector2(int(config.get("spawn_offset", 0)), 32)
		stage.add_child(player)
		add_child(stage)
	Shared.cam.node_target = null
	Shared.cam.pos_target_offset = Vector2.ZERO
	Shared.cam.set_physics_process(false)
	Shared.cam.set_pos(Vector2.ZERO)
	Shared.cam.zoom = Vector2.ONE
	Shared.cam.current = true
	# Autoloads stay available to the original character; their UI is omitted.
	for child in get_tree().root.get_children():
		if child != self and child != Shared:
			_hide_visuals(child)
	Shared.node_ghost.hide()
	Shared.node_ghosts.hide()
	AudioServer.set_bus_mute(0, true)
	previous_position = player.position
	VisualServer.connect("frame_post_draw", self, "_on_frame_post_draw")
	set_physics_process(true)

func _hide_visuals(node):
	if node is CanvasItem:
		node.hide()
	for child in node.get_children():
		_hide_visuals(child)

func _physics_process(_delta):
	if finished:
		return
	tick += 1
	if tick < warmup:
		if player.is_on_floor and player.position == previous_position:
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

func _stage_next_input():
	# action_press targets the NEXT physics frame in Godot's Input API.
	# Queue after drawing, before that frame, so held and just_pressed agree.
	var next_capture_tick = tick + 1 - warmup
	if next_capture_tick == start_tick:
		Input.action_press("jump")
		pressed = true
	elif next_capture_tick == start_tick + hold_ticks:
		Input.action_release("jump")
		pressed = false

func _on_frame_post_draw():
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
	var img = get_viewport().get_texture().get_data()
	img.flip_y()
	if img.get_width() != 228 or img.get_height() != 128:
		_finish("error", "Unexpected viewport dimensions")
		return
	var save_error = img.save_png(output_dir.plus_file("frames/%06d.png" % capture_tick))
	if save_error != OK:
		_finish("error", "PNG write failed")
		return
	frames.append({"frame_index": capture_tick, "physics_tick": tick, "time_s": float(capture_tick) / FPS, "actions": ["jump"] if pressed else []})
	last_draw_tick = tick
	if frames.size() == frame_count:
		_finish("ok", "")
	else:
		_stage_next_input()

func _finish(status, error):
	if finished:
		return
	finished = true
	Input.action_release("jump")
	var result = {"status": status, "fps": FPS, "viewport": [228, 128], "frame_count": frames.size(), "frames": frames, "settled": settled}
	if not error.empty():
		result["error"] = error
		printerr(error)
	if not output_dir.empty():
		var file = File.new()
		if file.open(output_dir.plus_file("recording.json"), File.WRITE) == OK:
			file.store_string(JSON.print(result, "  "))
			file.close()
	if VisualServer.is_connected("frame_post_draw", self, "_on_frame_post_draw"):
		VisualServer.disconnect("frame_post_draw", self, "_on_frame_post_draw")
	get_tree().call_deferred("quit", 0 if status == "ok" else 2)
