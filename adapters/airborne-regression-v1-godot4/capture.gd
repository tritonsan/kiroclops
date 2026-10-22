extends Node
# Operator-only seeded contract. No kinematic values are serialized.
const FPS := 60
const WARMUP := 60
const FRAME_COUNT := 240
const EPSILON := 0.000001
const PRIVATE_VERTICAL_SEED := 300.0
const PRIVATE_HORIZONTAL_SEED := 35.0

class PostObserver extends Node:
	var observe: Callable
	func _physics_process(_delta: float) -> void:
		observe.call()

var config: Dictionary
var player
var tick := -1
var frame_index := -1
var last_draw_tick := -1
var finished := false
var frames: Array = []
var cases: Array = []
var active_case := -1
var pre: Dictionary = {}
var pre_velocity := Vector2.ZERO
var pre_position := Vector2.ZERO

func _ready() -> void:
	process_physics_priority = -100
	set_physics_process(false)
	var file := FileAccess.open(OS.get_environment("KIROCLOPS_CONFIG"), FileAccess.READ)
	if file == null:
		get_tree().quit(2)
		return
	var parsed = JSON.parse_string(file.get_as_text())
	file.close()
	if not parsed is Dictionary:
		get_tree().quit(2)
		return
	config = parsed
	if config.get("protocol") != "airborne-regression-v1" or Engine.physics_ticks_per_second != FPS:
		_finish("error", "Protocol or physics rate differs")
		return
	if not config.get("cases") is Array or config.cases.size() != 18:
		_finish("error", "Expected fixed eighteen cases")
		return
	for action in InputMap.get_actions():
		Input.action_release(action)
		InputMap.action_erase_events(action)
	seed(0)
	AudioServer.set_bus_mute(0, true)
	DisplayServer.window_set_size(Vector2i(640, 360))
	var camera := Camera2D.new()
	camera.position = Vector2(320, 180)
	camera.position_smoothing_enabled = false
	add_child(camera)
	camera.make_current()
	# A far lower floor makes this actual open-air physics, not assigned contact.
	var lower_floor := StaticBody2D.new()
	lower_floor.position = Vector2(320, 10000)
	var collision := CollisionShape2D.new()
	var rectangle := RectangleShape2D.new()
	rectangle.size = Vector2(10000, 32)
	collision.shape = rectangle
	lower_floor.add_child(collision)
	add_child(lower_floor)
	var observer := PostObserver.new()
	observer.process_physics_priority = 100
	observer.observe = _after_physics
	add_child(observer)
	RenderingServer.frame_post_draw.connect(_after_draw)
	set_physics_process(true)

func _state() -> String:
	if player != null and player.state_callback:
		return str(player.state_callback.get_object().name)
	return "None"

func _actions() -> Array:
	var result: Array = []
	for item in [["left", "ui_left"], ["right", "ui_right"], ["jump", "ui_up"]]:
		if Input.is_action_pressed(item[1]):
			result.append(item[0])
	return result

func _clear_inputs() -> void:
	for action in InputMap.get_actions():
		Input.action_release(action)

func _hash_x() -> String:
	var context := HashingContext.new()
	context.start(HashingContext.HASH_SHA256)
	# Pinned Windows Godot standard build uses float32 Vector2 components.
	context.update(PackedFloat32Array([player.velocity.x]).to_byte_array())
	return context.finish().hex_encode()

func _snapshot() -> Dictionary:
	return {"state": _state(), "on_floor": player.is_on_floor(), "on_wall": player.is_on_wall(),
		"horizontal_rest": absf(player.velocity.x) <= EPSILON,
		"moving_up": player.velocity.y < -EPSILON, "moving_down": player.velocity.y > EPSILON,
		"actions": _actions(), "jump_just_pressed": Input.is_action_just_pressed("ui_up"),
		"horizontal_response_sha256": _hash_x()}

func _create_case(index: int) -> void:
	if player != null:
		_finish("error", "Previous actor was not disposed")
		return
	_clear_inputs()
	active_case = index
	player = load("res://source/characters/player/player.tscn").instantiate()
	player.position = Vector2(320, 180)
	add_child(player)
	player.show()
	player.states.Fall.enter()
	player.velocity = Vector2.ZERO
	cases.append({"case_id": str(config.cases[index].case_id), "contact_refresh": [], "steps": []})

func _seed_case() -> void:
	var spec: Dictionary = config.cases[active_case]
	# Only this explicit contract fixture seeds motion. The observer never calls
	# move() or replaces the actor's actual physics callback/integration.
	player.states[str(spec.state)].enter()
	player.position = Vector2(320, 180)
	player.velocity = Vector2(0.0 if spec.state == "Idle" else PRIVATE_HORIZONTAL_SEED,
		-PRIVATE_VERTICAL_SEED if spec.motion == "rising" else PRIVATE_VERTICAL_SEED)
	_clear_inputs()
	if spec.input == "left":
		Input.action_press("ui_left")
	elif spec.input == "right":
		Input.action_press("ui_right")

func _physics_process(delta: float) -> void:
	if finished:
		return
	tick += 1
	frame_index = tick - WARMUP
	if absf(delta - 1.0 / FPS) > EPSILON:
		_finish("error", "Physics delta changed")
		return
	if player == null or active_case < 0:
		return
	if player.process_physics_priority != 0 or not player.is_physics_processing():
		_finish("error", "Actor physics callback priority/processing differs")
		return
	var relative := frame_index - active_case * 8
	if relative >= 3 and relative <= 6:
		pre = _snapshot()
		pre_velocity = player.velocity
		pre_position = player.position

func _after_physics() -> void:
	if finished or player == null or active_case < 0:
		return
	var relative := frame_index - active_case * 8
	if relative == 1 or relative == 2:
		cases[active_case].contact_refresh.append({"frame_index": frame_index,
			"airborne": not player.is_on_floor(), "no_wall": not player.is_on_wall()})
	if relative >= 3 and relative <= 6:
		if pre.is_empty():
			_finish("error", "Missing pre-player observation")
			return
		var post := _snapshot()
		var predicates := {"horizontal_velocity_unchanged": absf(player.velocity.x - pre_velocity.x) <= EPSILON,
			"horizontal_position_unchanged": absf(player.position.x - pre_position.x) <= EPSILON,
			"horizontal_speed_increased": absf(player.velocity.x) > absf(pre_velocity.x) + EPSILON,
			"horizontal_speed_decreased": absf(player.velocity.x) < absf(pre_velocity.x) - EPSILON,
			"horizontal_direction_changed": signf(player.velocity.x) != signf(pre_velocity.x),
			"vertical_direction_changed": signf(player.velocity.y) != signf(pre_velocity.y)}
		cases[active_case].steps.append({"step_index": relative - 3, "frame_index": frame_index,
			"physics_tick": tick, "pre": pre, "post": post, "predicates": predicates})
		pre = {}

func _after_draw() -> void:
	if finished or tick < WARMUP or tick == last_draw_tick:
		return
	if frame_index != frames.size() or tick != WARMUP + frames.size():
		_finish("error", "Physics/render sequence skipped or duplicated a step")
		return
	var image := get_viewport().get_texture().get_image()
	if image.get_width() != 640 or image.get_height() != 360:
		_finish("error", "Native viewport changed")
		return
	if image.save_png(str(config.output_dir).path_join("frames/%06d.png" % frame_index)) != OK:
		_finish("error", "Native PNG write failed")
		return
	frames.append({"frame_index": frame_index, "physics_tick": tick, "time_s": float(frame_index) / FPS, "actions": _actions()})
	last_draw_tick = tick
	if frame_index < 144:
		var relative := frame_index % 8
		if relative == 0:
			_create_case(frame_index / 8)
		elif relative == 2:
			_seed_case()
		elif relative == 6:
			player.queue_free()
			player = null
			_clear_inputs()
	if frames.size() == FRAME_COUNT:
		_finish("ok", "")

func _finish(status: String, error: String) -> void:
	if finished:
		return
	finished = true
	set_physics_process(false)
	_clear_inputs()
	var result := {"schema_version": 1, "protocol": "airborne-regression-v1", "status": status,
		"fps": FPS, "viewport": [640, 360], "frame_count": frames.size(), "warmup_ticks": WARMUP,
		"frames": frames, "cases": cases}
	if not error.is_empty():
		result.error = error
		push_error(error)
	if config.has("output_dir"):
		var file := FileAccess.open(str(config.output_dir).path_join("recording.json"), FileAccess.WRITE)
		if file != null:
			file.store_string(JSON.stringify(result, "  "))
			file.close()
	if RenderingServer.frame_post_draw.is_connected(_after_draw):
		RenderingServer.frame_post_draw.disconnect(_after_draw)
	get_tree().call_deferred("quit", 0 if status == "ok" else 2)
