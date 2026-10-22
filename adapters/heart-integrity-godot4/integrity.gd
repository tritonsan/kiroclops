extends Node2D
# Operator fixture. Real player scene/script remains untouched. No float-valued
# motor observations are serialized; these hashes are never a motion-fit input.
const FPS := 60
const WARMUP := 60
const EPS := 0.000001

class PostObserver extends Node:
	var callback: Callable
	func _physics_process(_delta: float) -> void:
		callback.call()

var config: Dictionary
var tick := -1
var frame := -1
var last_draw := -1
var finished := false
var frames: Array = []
var cases: Array = []
var case_index := 0
var player
var fixture: Node2D
var floor_body
var wall_body
var pre: Dictionary = {}
var pre_position := Vector2.ZERO
var pre_velocity := Vector2.ZERO
var first_takeoffs := 0
var hazard_contact := false

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
	if config.get("protocol") != "heart-integrity-v1" or Engine.physics_ticks_per_second != FPS:
		_finish("error", "Protocol or physics frequency differs")
		return
	for action in InputMap.get_actions():
		Input.action_release(action)
		InputMap.action_erase_events(action)
	seed(0)
	AudioServer.set_bus_mute(0, true)
	DisplayServer.window_set_size(Vector2i(320, 180))
	var camera := Camera2D.new()
	camera.position = Vector2(160, 90)
	camera.position_smoothing_enabled = false
	add_child(camera)
	camera.make_current()
	var observer := PostObserver.new()
	observer.process_physics_priority = 100
	observer.callback = _after_physics
	add_child(observer)
	RenderingServer.frame_post_draw.connect(_after_draw)
	set_physics_process(true)

func _clear_inputs() -> void:
	for action in InputMap.get_actions():
		Input.action_release(action)

func _actions() -> Array:
	var result: Array = []
	for pair in [["left", "move_left"], ["right", "move_right"], ["jump", "jump"]]:
		if Input.is_action_pressed(pair[1]):
			result.append(pair[0])
	return result

func _hash(value: float) -> String:
	var context := HashingContext.new()
	context.start(HashingContext.HASH_SHA256)
	context.update(PackedFloat32Array([value]).to_byte_array())
	return context.finish().hex_encode()

func _snapshot(delta_position: Vector2 = Vector2.ZERO) -> Dictionary:
	return {"vx": _hash(player.velocity.x), "vy": _hash(player.velocity.y),
		"dx": _hash(delta_position.x), "dy": _hash(delta_position.y),
		"floor": player.is_on_floor(), "wall": player.is_on_wall(),
		"up": player.velocity.y < -EPS, "down": player.velocity.y > EPS,
		"air_jump": bool(player.air_jump),
		"coyote": player.coyote_jump_timer.time_left > 0.0,
		"wall_grace": player.wall_jump_timer.time_left > 0.0}

func _box(position_value: Vector2, size_value: Vector2) -> StaticBody2D:
	var body := StaticBody2D.new()
	body.position = position_value
	var shape := CollisionShape2D.new()
	var rectangle := RectangleShape2D.new()
	rectangle.size = size_value
	shape.shape = rectangle
	body.add_child(shape)
	fixture.add_child(body)
	return body

func _create_case() -> void:
	_clear_inputs()
	var spec: Dictionary = config.cases[case_index]
	fixture = Node2D.new()
	add_child(fixture)
	first_takeoffs = 0
	hazard_contact = false
	floor_body = null
	wall_body = null
	var checks: Dictionary
	match str(spec.kind):
		"air": checks = {"fresh_air_contacts": true, "seed_valid": false}
		"ground": checks = {"settled": false, "remained_grounded": true, "moved": false, "stopped": false}
		"jump": checks = {"settled": false, "took_off": false, "returned": false, "single_takeoff": false}
		"double": checks = {"settled": false, "first_takeoff": false, "second_available": false,
			"second_consumed": false, "third_rejected": false, "landed": false, "reset_jump": false}
		"wall": checks = {"contact_established": false, "jump_context": false, "response": false}
		"coyote": checks = {"settled": false, "real_contact_loss": false, "timer_context": false,
			"response": false, "rights": false}
		"hazard": checks = {"displaced": false, "area_contact": false, "returned": false}
		_: _finish("error", "Unknown case"); return
	cases.append({"id": str(spec.id), "kind": str(spec.kind), "steps": [], "checks": checks})
	if spec.kind in ["ground", "jump", "double", "coyote", "hazard"]:
		floor_body = _box(Vector2(160, 160), Vector2(2000, 32))
	player = load("res://player.tscn").instantiate()
	player.position = Vector2(160, 144) if floor_body != null else Vector2(160, 80)
	if spec.kind == "wall":
		var right_side: bool = spec.side == "right"
		wall_body = _box(Vector2(224 if right_side else 96, 80), Vector2(32, 320))
		player.position = Vector2(203.8 if right_side else 116.2, 80)
	fixture.add_child(player)
	player.get_node("Camera2D").enabled = false
	if spec.kind == "hazard":
		var area := Area2D.new()
		area.position = Vector2(240, 118)
		area.collision_layer = 4
		area.collision_mask = 0
		var shape := CollisionShape2D.new()
		var rectangle := RectangleShape2D.new()
		rectangle.size = Vector2(24, 24)
		shape.shape = rectangle
		area.add_child(shape)
		fixture.add_child(area)
		player.get_node("HazardDetector").area_entered.connect(_hazard_entered)

func _hazard_entered(_area: Area2D) -> void:
	hazard_contact = true

func _next_inputs(next_step: int) -> void:
	# Only alter action state on transitions, preserving native just-pressed edges.
	var spec: Dictionary = config.cases[case_index]
	var left := false
	var right := false
	var jump := false
	match str(spec.kind):
		"air":
			if next_step >= 3 and next_step <= 6:
				left = spec.action == "left"
				right = spec.action == "right"
		"ground":
			match str(spec.action):
				"short": right = next_step >= 10 and next_step < 16
				"long": right = next_step >= 10 and next_step < 58
				"left": left = next_step >= 10 and next_step < 40
				"reverse":
					right = next_step >= 10 and next_step < 34
					left = next_step >= 34 and next_step < 58
		"jump": jump = next_step >= 10 and next_step < 10 + int(spec.hold)
		"double": jump = (next_step >= 10 and next_step < 13) or (next_step >= 20 and next_step < 24) or (next_step >= 27 and next_step < 30) or (next_step >= 130 and next_step < 133)
		"wall":
			left = spec.side == "left" and next_step <= 5
			right = spec.side == "right" and next_step <= 5
			var press: int = {"contact": 5, "grace": 8, "expired": 20}[str(spec.mode)]
			jump = next_step >= press and next_step < press + 4
		"coyote":
			var press := 13 if spec.mode == "inside" else 24
			jump = next_step >= press and next_step < press + 3
	for pair in [["move_left", left], ["move_right", right], ["jump", jump]]:
		if bool(pair[1]) != Input.is_action_pressed(pair[0]):
			if pair[1]: Input.action_press(pair[0])
			else: Input.action_release(pair[0])

func _physics_process(delta: float) -> void:
	if finished: return
	tick += 1
	frame = tick - WARMUP
	if absf(delta - 1.0 / FPS) > EPS:
		_finish("error", "Unexpected physics delta")
		return
	if player == null: return
	if player.process_physics_priority != 0 or not player.is_physics_processing():
		_finish("error", "Player physics callback was disabled or reordered")
		return
	pre_position = player.position
	pre_velocity = player.velocity
	pre = _snapshot()

func _after_physics() -> void:
	if finished or player == null: return
	var spec: Dictionary = config.cases[case_index]
	var step := frame - int(spec.start)
	if step < 1 or step >= int(spec.duration): return
	if pre.is_empty():
		_finish("error", "Missing pre-physics observation")
		return
	var post := _snapshot(player.position - pre_position)
	cases[case_index].steps.append({"step": step, "pre": pre, "post": post})
	var checks: Dictionary = cases[case_index].checks
	match str(spec.kind):
		"air":
			checks.fresh_air_contacts = checks.fresh_air_contacts and not post.floor and not post.wall
		"ground":
			if step == 8: checks.settled = post.floor
			if step >= 8: checks.remained_grounded = checks.remained_grounded and post.floor
			if step >= 10 and absf(player.velocity.x) > EPS: checks.moved = true
			if step == int(spec.duration) - 1: checks.stopped = absf(player.velocity.x) < EPS
		"jump":
			if step == 8: checks.settled = post.floor
			if pre.floor and not post.floor and post.up:
				checks.took_off = true
				first_takeoffs += 1
			if step > 10 and post.floor: checks.returned = true
			checks.single_takeoff = first_takeoffs == 1
		"double":
			if step == 8: checks.settled = post.floor
			if step == 10: checks.first_takeoff = pre.floor and post.up and not post.floor
			if step == 20:
				checks.second_available = not pre.floor and pre.air_jump
				checks.second_consumed = not post.air_jump and post.up
			if step == 27:
				checks.third_rejected = not pre.floor and not pre.air_jump and not post.air_jump and player.velocity.y >= pre_velocity.y - EPS
			if step > 30 and step < 130 and post.floor and post.air_jump: checks.landed = true
			if step == 130: checks.reset_jump = pre.floor and pre.air_jump and post.up
		"wall":
			if step <= 4 and post.wall and not post.floor: checks.contact_established = true
			var press: int = {"contact": 5, "grace": 8, "expired": 20}[str(spec.mode)]
			if step == press:
				if spec.mode == "contact": checks.jump_context = pre.wall and not pre.floor
				elif spec.mode == "grace": checks.jump_context = not pre.wall and pre.wall_grace
				else: checks.jump_context = not pre.wall and not pre.wall_grace and not pre.air_jump
				if spec.mode == "expired": checks.response = not post.up and not post.air_jump
				else:
					var away: bool = player.velocity.x < -EPS if spec.side == "right" else player.velocity.x > EPS
					checks.response = post.up and away and not post.floor
		"coyote":
			if step == 8: checks.settled = post.floor
			if pre.floor and not post.floor and not post.up: checks.real_contact_loss = true
			var press := 13 if spec.mode == "inside" else 24
			if step == press:
				checks.timer_context = not pre.floor and (pre.coyote if spec.mode == "inside" else not pre.coyote)
				checks.response = post.up and not post.coyote
				checks.rights = post.air_jump if spec.mode == "inside" else not post.air_jump
		"hazard":
			if step == 21: checks.displaced = pre_position.distance_to(player.starting_position) > 40.0
			if hazard_contact:
				checks.area_contact = true
				if player.position.distance_to(player.starting_position) < 2.0: checks.returned = true
	pre = {}

func _after_draw() -> void:
	if finished or tick < WARMUP or tick == last_draw: return
	if frame != frames.size():
		_finish("error", "Physics/render mapping skipped or repeated a step")
		return
	var spec: Dictionary = config.cases[case_index]
	var step := frame - int(spec.start)
	frames.append({"frame": frame, "tick": tick, "case_id": str(spec.id), "step": step, "actions": _actions()})
	last_draw = tick
	if step in [3, 6, 10, 20, 27] or step == int(spec.duration) - 1:
		var image := get_viewport().get_texture().get_image()
		if image.get_width() != 320 or image.get_height() != 180 or image.save_png(str(config.output_dir).path_join("frames/%06d.png" % frame)) != OK:
			_finish("error", "Native checkpoint image write failed")
			return
	if step == 0: _create_case()
	if spec.kind == "air" and step == 2:
		player.position = Vector2(160, 80)
		player.velocity = Vector2(0.0 if spec.vx == "rest" else (-35.0 if spec.vx == "left" else 35.0), -100.0 if spec.vy == "rising" else 100.0)
		cases[case_index].checks.seed_valid = not player.is_on_floor() and not player.is_on_wall()
	if spec.kind == "wall" and spec.mode != "contact" and step == 5:
		wall_body.queue_free()
		wall_body = null
	if spec.kind == "coyote" and step == 10:
		floor_body.queue_free()
		floor_body = null
	if spec.kind == "hazard" and step == 20:
		player.position = Vector2(240, 130)
	if step == int(spec.duration) - 1:
		fixture.queue_free()
		player = null
		fixture = null
		_clear_inputs()
		case_index += 1
	else:
		_next_inputs(step + 1)
	if frames.size() == int(config.frame_count): _finish("ok", "")

func _finish(status: String, error: String) -> void:
	if finished: return
	finished = true
	set_physics_process(false)
	_clear_inputs()
	var result := {"schema_version": 1, "protocol": "heart-integrity-v1", "stage": config.get("stage", ""),
		"status": status, "fps": FPS, "warmup": WARMUP, "viewport": [320, 180],
		"frame_count": frames.size(), "frames": frames, "cases": cases}
	if not error.is_empty():
		result.error = error
		push_error(error)
	if config.has("output_dir"):
		var file := FileAccess.open(str(config.output_dir).path_join("recording.json"), FileAccess.WRITE)
		if file != null:
			file.store_string(JSON.stringify(result, "  "))
			file.close()
	if RenderingServer.frame_post_draw.is_connected(_after_draw): RenderingServer.frame_post_draw.disconnect(_after_draw)
	get_tree().call_deferred("quit", 0 if status == "ok" else 2)
