"""Apply a measured jump profile to SlayHorizon's real player controller.

This module reads only the target project. Source-game code and constants are
not inputs. Heights are the target's visually measured standing-body pixels.
"""
from __future__ import annotations

import difflib
import math
from pathlib import Path
from typing import Any, Mapping


_MARKER_START = "# KIROCLOPS_PROFILE_BEGIN"
_MARKER_END = "# KIROCLOPS_PROFILE_END"


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def apply_profile(
    project_path: str | Path,
    profile: Mapping[str, Any],
    body_height_px: float,
) -> dict[str, Any]:
    """Patch the pinned target and return the reviewable diff and parameters.

    ``v0_body_s`` is positive up, ``gravity_body_s2`` positive down. Integration
    matches the fitted convention: held steps move at the held speed; free
    steps apply gravity before displacement, including a ballistic first tick.
    The original horizontal, double-jump and wall-jump parameters are retained.
    Calling this again on an already patched tree raises rather than stacking
    edits; each candidate must start from the unmodified target snapshot.
    """
    project = Path(project_path).resolve()
    family = profile.get("family")
    if family not in {"ballistic", "held_velocity"}:
        raise ValueError("Unsupported motion-profile family")
    height = _positive_number(body_height_px, "body_height_px")
    v0 = _positive_number(profile["v0_body_s"], "v0_body_s") * height
    gravity = _positive_number(profile["gravity_body_s2"], "gravity_body_s2") * height
    raw_hold = profile.get("hold_ticks", 0 if family == "ballistic" else 1)
    minimum_hold = 0 if family == "ballistic" else 1
    if isinstance(raw_hold, bool) or int(raw_hold) != raw_hold or not minimum_hold <= int(raw_hold) <= 120:
        raise ValueError(f"hold_ticks must be an integer from {minimum_hold} through 120")
    hold_ticks = int(raw_hold)
    player_path = project / "source/characters/player/player.gd"
    jump_path = project / "source/characters/player/state_machine/states/jump.gd"
    originals = {p: p.read_text(encoding="utf-8") for p in (player_path, jump_path)}
    player = originals[player_path]
    if _MARKER_START in player:
        raise ValueError("Target already contains a KiroClops profile; use a fresh work copy")

    variable_anchor = "var state_callback: Callable\n"
    physics_anchor = "func _physics_process(delta: float) -> void:\n"
    gravity_anchor = "\t\tvelocity += gravity * delta\n"
    for anchor in (variable_anchor, physics_anchor, gravity_anchor):
        if player.count(anchor) != 1:
            raise ValueError("Target player structure differs from the supported pinned snapshot")
    expected_jump = "\tplayer.velocity.y = player.jump_velocity\n"
    if originals[jump_path].count(expected_jump) != 1:
        raise ValueError("Target Jump state differs from the supported pinned snapshot")

    parameters = (
        f"\n\n{_MARKER_START}\n"
        "# Values are fitted from video and scaled by observed target body height.\n"
        f'var motion_profile_family: String = "{family}"\n'
        f"var motion_profile_velocity: float = {-v0:.12g}\n"
        f"var motion_profile_gravity: float = {gravity:.12g}\n"
        f"var motion_profile_hold_ticks: int = {hold_ticks}\n"
        "var motion_profile_active: bool = false\n"
        f"{_MARKER_END}\n"
    )
    player = player.replace(variable_anchor, variable_anchor + parameters)
    player = player.replace(
        physics_anchor,
        physics_anchor
        + "\t# A normal jump owns its fitted gravity through the Fall state.\n"
        + "\t# Landing, double-jump, or wall-jump restores original game gravity.\n"
        + "\tif is_on_floor() or (state_callback and state_callback.get_object() not in [states.Jump, states.Fall]):\n"
        + "\t\tmotion_profile_active = false\n",
    )
    player = player.replace(
        gravity_anchor,
        "\t\tif motion_profile_active:\n"
        "\t\t\tvelocity.y += motion_profile_gravity * delta\n"
        "\t\telse:\n"
        "\t\t\tvelocity += gravity * delta\n",
    )
    jump = '''@tool
extends PlayerState

# KiroClops changes this actual game state, shared by fixture and normal map.
var profile_held_steps: int = 0


func enter() -> void:
\t# The original WallJump state also enters Jump for its launch. Preserve
\t# that existing launch instead of applying the measured ground-jump rule.
\tvar from_wall: bool = player.state_callback and player.state_callback.get_object() == player.states.WallJump
\tsuper.enter()
\tplayer.jump_buffer = false
\tif from_wall:
\t\tplayer.motion_profile_active = false
\t\tprofile_held_steps = 0
\t\tplayer.velocity.y = player.jump_velocity
\t\treturn
\tplayer.motion_profile_active = true
\tprofile_held_steps = 1
\tplayer.velocity.y = player.motion_profile_velocity
\tif player.motion_profile_family == "ballistic":
\t\tplayer.velocity.y += player.motion_profile_gravity * player.get_physics_process_delta_time()


func state_process(delta: float) -> void:
\tif player.motion_profile_active and player.motion_profile_family == "held_velocity" and Input.is_action_pressed("ui_up") and profile_held_steps < player.motion_profile_hold_ticks:
\t\tplayer.velocity.y = player.motion_profile_velocity
\telse:
\t\tplayer.apply_gravity(delta)
\tprofile_held_steps += 1
\tplayer.move()
\tif player.is_on_floor():
\t\tplayer.states.Idle.enter()
\telif player.double_jump and Input.is_action_just_pressed("ui_up"):
\t\tplayer.states.DoubleJump.enter()
\telif player.velocity.y > 0:
\t\tplayer.states.Fall.enter()
'''
    modified = {player_path: player, jump_path: jump}
    diff = "".join(
        "".join(
            difflib.unified_diff(
                originals[path].splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile="a/" + path.relative_to(project).as_posix(),
                tofile="b/" + path.relative_to(project).as_posix(),
            )
        )
        for path, content in modified.items()
    )
    for path, content in modified.items():
        path.write_text(content, encoding="utf-8", newline="\n")
    return {
        "changed_files": [str(path.relative_to(project)) for path in modified],
        "diff": diff,
        "parameters": {
            "family": family,
            "body_height_px": height,
            "v0_px_s": v0,
            "gravity_px_s2": gravity,
            "hold_ticks": hold_ticks,
        },
    }
