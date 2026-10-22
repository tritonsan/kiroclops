extends Node2D
# Versioned preservation fixture. Installed only in disposable suite copies.
# Static geometry + initial spawn + action inputs only; no timer/velocity seeding.
const FPS := 60
const WARMUP := 60

class PostObserver extends Node:
	var callback: Callable
	func _physics_process(_delta: float) -> void:
		callback.call()

var config: Dictionary
var tick := -1
var frame := -1
var last_draw := -1
var finished := false
var case_index := 0
var player
var fixture
var frames: Array = []
var cases: Array = []
var pre: Dictionary = {}

func _ready() -> void:
	process_physics_priority = -100
	set_physics_process(false)
	var file := FileAccess.open(OS.get_environment("KIROCLOPS_CONFIG"), FileAccess.READ)
	config = JSON.parse_string(file.get_as_text())
	file.close()
	if config.get("protocol") != "heart-wall-coyote-v2" or Engine.physics_ticks_per_second != FPS:
		_finish("error", "Wrong diagnostic protocol or frequency")
		return
	for action in InputMap.get_actions():
		Input.action_release(action)
		InputMap.action_erase_events(action)
	seed(0)
	AudioServer.set_bus_mute(0, true)
	DisplayServer.window_set_size(Vector2i(320, 180))
	var camera := Camera2D.new()
	camera.position = Vector2(160, 90)
	add_child(camera)
	camera.make_current()
	var observer := PostObserver.new()
	observer.process_physics_priority = 100
	observer.callback = _after_physics
	add_child(observer)
	RenderingServer.frame_post_draw.connect(_after_draw)
	set_physics_process(true)

func _actions() -> Array:
	var value: Array = []
	for action in ["jump", "move_left", "move_right"]:
		if Input.is_action_pressed(action): value.append(action)
	return value

func _snapshot() -> Dictionary:
	return {"position": [player.position.x, player.position.y],
		"velocity": [player.velocity.x, player.velocity.y],
		"floor": player.is_on_floor(), "wall_only": player.is_on_wall_only(),
		"wall": player.is_on_wall(), "wall_normal": [player.get_wall_normal().x, player.get_wall_normal().y],
		"coyote_seconds": player.coyote_jump_timer.time_left,
		"wall_grace_seconds": player.wall_jump_timer.time_left,
		"air_jump": bool(player.air_jump), "jump_edge": Input.is_action_just_pressed("jump")}

func _box(where: Vector2, size_value: Vector2) -> void:
	var body := StaticBody2D.new()
	body.position = where
	var shape := CollisionShape2D.new()
	var rectangle := RectangleShape2D.new()
	rectangle.size = size_value
	shape.shape = rectangle
	body.add_child(shape)
	var visual := Polygon2D.new()
	visual.polygon = PackedVector2Array([-size_value/2, Vector2(size_value.x/2, -size_value.y/2), size_value/2, Vector2(-size_value.x/2, size_value.y/2)])
	visual.color = Color(0.2, 0.4, 0.6)
	body.add_child(visual)
	fixture.add_child(body)

func _create_case() -> void:
	fixture = Node2D.new()
	add_child(fixture)
	var spec: Dictionary = config.cases[case_index]
	var side: float = 1.0 if spec.side == "right" else -1.0
	# Floor has its edge 30px from spawn; wall begins 40px from spawn.
	if spec.kind == "overlap":
		_box(Vector2(160 - side*470, 160), Vector2(1000, 32))
	_box(Vector2(160 + side*56, 80), Vector2(32, 600))
	player = load("res://player.tscn").instantiate()
	player.position = Vector2(160, 144) if spec.kind == "overlap" else Vector2(160 + side*35.8, 80)
	fixture.add_child(player)
	player.get_node("Camera2D").enabled = false
	cases.append({"id": spec.id, "kind": spec.kind, "side": spec.side, "press": -1, "steps": []})

func _next_inputs(step: int) -> void:
	var spec: Dictionary = config.cases[case_index]
	var press: int = int(cases[case_index].press)
	# Read contacts; write only action inputs. A legal ground law may reach this
	# static ledge/wall at another step, so the overlap input is contact-triggered.
	if press < 0:
		if spec.kind == "wall_only" and step == 35:
			press = step
		elif spec.kind == "overlap" and step >= 11 and not player.is_on_floor() and player.is_on_wall_only() and player.coyote_jump_timer.time_left > 0:
			press = step
		cases[case_index].press = press
	var direction: bool = step >= int(spec.direction_start) and (press < 0 or step < press + 4)
	var jump: bool = press >= 0 and step >= press and step < press + 4
	for pair in [["move_left", direction and spec.side == "left"], ["move_right", direction and spec.side == "right"], ["jump", jump]]:
		if bool(pair[1]) != Input.is_action_pressed(pair[0]):
			if pair[1]: Input.action_press(pair[0])
			else: Input.action_release(pair[0])

func _physics_process(delta: float) -> void:
	if finished: return
	tick += 1
	frame = tick - WARMUP
	if absf(delta - 1.0/FPS) > 0.000001:
		_finish("error", "Unexpected physics delta")
		return
	if player == null: return
	if player.process_physics_priority != 0 or not player.is_physics_processing():
		_finish("error", "Player callback disabled or reordered")
		return
	pre = _snapshot()

func _after_physics() -> void:
	if finished or player == null: return
	var spec: Dictionary = config.cases[case_index]
	var step := frame - int(spec.start)
	if step < 1 or step >= int(spec.duration): return
	if pre.is_empty():
		_finish("error", "Missing pre-physics observation")
		return
	cases[case_index].steps.append({"step": step, "pre": pre, "post": _snapshot()})
	pre = {}

func _after_draw() -> void:
	if finished or tick < WARMUP or tick == last_draw: return
	if frame != frames.size():
		_finish("error", "Physics/render mapping missing or repeated step")
		return
	var spec: Dictionary = config.cases[case_index]
	var step := frame - int(spec.start)
	frames.append({"frame": frame, "tick": tick, "case_id": spec.id, "step": step, "actions": _actions()})
	last_draw = tick
	if step == 0: _create_case()
	# Every draw is retained: review can cover variable contact-triggered timing.
	if step >= 0:
		var picture := get_viewport().get_texture().get_image()
		if picture.get_width() != 320 or picture.get_height() != 180 or picture.save_png(str(config.output_dir).path_join("frames/%06d.png" % frame)) != OK:
			_finish("error", "Checkpoint PNG failed")
			return
	if step == int(spec.duration)-1:
		fixture.queue_free()
		fixture = null
		player = null
		for action in InputMap.get_actions(): Input.action_release(action)
		case_index += 1
	else:
		_next_inputs(step+1)
	if frames.size() == int(config.frame_count): _finish("ok", "")

func _finish(status: String, error: String) -> void:
	if finished: return
	finished = true
	set_physics_process(false)
	for action in InputMap.get_actions(): Input.action_release(action)
	var result := {"schema_version": 1, "protocol": config.get("protocol"), "status": status,
		"error": error, "fps": FPS, "warmup": WARMUP, "viewport": [320,180],
		"frame_count": frames.size(), "frames": frames, "cases": cases}
	var file := FileAccess.open(str(config.output_dir).path_join("recording.json"), FileAccess.WRITE)
	if file != null:
		file.store_string(JSON.stringify(result, "  "))
		file.close()
	if RenderingServer.frame_post_draw.is_connected(_after_draw): RenderingServer.frame_post_draw.disconnect(_after_draw)
	get_tree().call_deferred("quit", 0 if status == "ok" else 2)
