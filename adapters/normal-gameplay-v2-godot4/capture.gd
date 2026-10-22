extends Node

# Contact-v2 normal gameplay assertions. Private velocities only produce sign
# and rest booleans; no coordinates, speed or acceleration are serialized.
const FPS := 60
const WARMUP := 60
const FRAME_COUNT := 240
const PROTOCOL_REVISION := 3
const REST_EPSILON := 0.000001

class PostObserver extends Node:
	var observe: Callable
	func _physics_process(_delta: float) -> void:
		observe.call()

var config: Dictionary
var player
var tick := -1
var capture_tick := -1
var last_draw_tick := -1
var stable_ticks := 0
var previous_position := Vector2.ZERO
var settled := false
var finished := false
var frames: Array = []
var runtime_frames: Array = []
var pre_transition: Dictionary = {}
var post_transition: Dictionary = {}
var platform_removed_after_frame := -1
var events: Array = []
var window_observations: Array = []
var startup_window_observations: Array = []
var pre_player_observation: Dictionary = {}
var observed_jump_edge := false

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
	if config.get("protocol_revision") != PROTOCOL_REVISION:
		_finish("error", "Supplemental protocol revision mismatch")
		return
	if config.get("no_focus") != true or config.get("offscreen") != true:
		_finish("error", "Offscreen and no-focus startup are required")
		return
	if not bool(get_tree().root.get_meta("kiroclops_background_configured", false)):
		_finish("error", "Approved background startup did not complete before the main scene")
		return
	if not bool(ProjectSettings.get_setting("display/window/size/no_focus", false)):
		_finish("error", "Project startup no-focus setting was not applied")
		return
	var initial_position: Vector2i = ProjectSettings.get_setting("display/window/size/initial_position", Vector2i.ZERO)
	if initial_position.x > -5000 or initial_position.y > -5000:
		_finish("error", "Project startup offscreen position was not applied")
		return
	if Engine.physics_ticks_per_second != FPS:
		_finish("error", "Expected original 60 Hz physics")
		return
	if config.get("probe") != "run_jump_land_brake":
		_finish("error", "Unknown integrity probe")
		return
	if config.get("normal_level") != true:
		_finish("error", "Contact-v2 requires the original normal level")
		return
	for action in InputMap.get_actions():
		Input.action_release(action)
		InputMap.action_erase_events(action)
	seed(0)
	events = [_event(5, "right", true), _event(30, "jump", true), _event(54, "jump", false), _event(90, "right", false)]
	var map = load("res://source/maps/map_scenes/map_demo.tscn").instantiate()
	add_child(map)
	player = map.get_node("Player")
	map.get_node("MapCamera").enabled = false
	player.show()
	# This is fixture initialization only; all subsequent transitions, including
	# the tested Idle -> Fall transition, belong to the untouched game controller.
	player.states.Fall.enter()
	var camera := Camera2D.new()
	camera.position = Vector2(320, 180)
	camera.position_smoothing_enabled = false
	add_child(camera)
	camera.make_current()
	AudioServer.set_bus_mute(0, true)
	var observer := PostObserver.new()
	observer.process_physics_priority = 100
	observer.observe = _after_player_physics
	add_child(observer)
	previous_position = player.position
	RenderingServer.frame_post_draw.connect(_after_draw)
	set_physics_process(true)

func _event(at: int, action: String, pressed: bool) -> Dictionary:
	return {"tick": at, "action": action, "pressed": pressed}

func _state() -> String:
	if player.state_callback:
		return str(player.state_callback.get_object().name)
	return "None"

func _horizontal_rest() -> bool:
	return absf(player.velocity.x) <= REST_EPSILON

func _motion_observation() -> Dictionary:
	return {"state": _state(), "on_floor": player.is_on_floor(), "on_wall": player.is_on_wall(), "horizontal_rest": _horizontal_rest(), "moving_up": player.velocity.y < -REST_EPSILON, "moving_down": player.velocity.y > REST_EPSILON}

func _actions() -> Array:
	var active: Array = []
	if Input.is_action_pressed("ui_up"):
		active.append("jump")
	if Input.is_action_pressed("ui_right"):
		active.append("right")
	return active

func _physics_process(_delta: float) -> void:
	if finished:
		return
	tick += 1
	capture_tick = tick - WARMUP
	if capture_tick < 0:
		if player.is_on_floor() and player.position.is_equal_approx(previous_position):
			stable_ticks += 1
		else:
			stable_ticks = 0
		previous_position = player.position
		return
	if capture_tick == 0:
		settled = stable_ticks >= 5 and _state() == "Idle" and _horizontal_rest()
		if not settled:
			_finish("error", "Original character did not settle into Idle")
			return
	pre_player_observation = _motion_observation()
	observed_jump_edge = Input.is_action_just_pressed("ui_up")

func _after_player_physics() -> void:
	if finished or capture_tick < 0:
		return
	runtime_frames.append({"frame_index": capture_tick, "pre": pre_player_observation.duplicate(), "post": _motion_observation(), "jump_just_pressed": observed_jump_edge})

func _stage_input(next_frame: int) -> void:
	for pressed in [false, true]:
		for event in events:
			if event.tick != next_frame or event.pressed != pressed:
				continue
			var action := "ui_right" if event.action == "right" else "ui_up"
			if pressed:
				Input.action_press(action)
			else:
				Input.action_release(action)

func _after_draw() -> void:
	if finished:
		return
	# Observe warmup too: focusing before the measured frames is still a failure.
	var window_position := DisplayServer.window_get_position()
	var window_check := {"frame_index": capture_tick, "focused": DisplayServer.window_is_focused(), "no_focus": DisplayServer.window_get_flag(DisplayServer.WINDOW_FLAG_NO_FOCUS), "offscreen": window_position.x < -5000 and window_position.y < -5000, "window_position": [window_position.x, window_position.y]}
	if capture_tick < 0:
		startup_window_observations.append(window_check)
	else:
		window_observations.append(window_check)
	if window_check.focused or not window_check.no_focus or not window_check.offscreen:
		_finish("error", "Native window focus/offscreen contract violated before PNG capture")
		return
	if capture_tick < 0:
		_stage_input(capture_tick + 1)
		return
	if capture_tick != frames.size() or runtime_frames.size() != capture_tick + 1 or (last_draw_tick >= 0 and tick != last_draw_tick + 1):
		_finish("error", "Physics, observer and rendered frames are not one-to-one")
		return
	var img := get_viewport().get_texture().get_image()
	if img.get_width() != 640 or img.get_height() != 360:
		_finish("error", "Native viewport changed")
		return
	var error := img.save_png(str(config.output_dir).path_join("frames/%06d.png" % capture_tick))
	if error != OK:
		_finish("error", "PNG capture failed")
		return
	frames.append({"frame_index": capture_tick, "physics_tick": tick, "time_s": float(capture_tick) / FPS, "actions": _actions()})
	last_draw_tick = tick
	if frames.size() == FRAME_COUNT:
		_finish("ok", "")
	else:
		_stage_input(capture_tick + 1)

func _finish(status: String, error: String) -> void:
	if finished:
		return
	finished = true
	set_physics_process(false)
	for action in ["ui_up", "ui_right"]:
		Input.action_release(action)
	var result := {"schema_version": 1, "status": status, "fps": FPS, "viewport": [640, 360], "frame_count": frames.size(), "warmup_ticks": WARMUP, "settled": settled, "probe": config.get("probe"), "normal_level": config.get("normal_level", false), "frames": frames, "runtime_frames": runtime_frames, "window_observations": window_observations, "startup_window_observations": startup_window_observations, "platform_removed_after_frame": platform_removed_after_frame, "pre_transition": pre_transition, "post_transition": post_transition}
	result.protocol_revision = PROTOCOL_REVISION
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
