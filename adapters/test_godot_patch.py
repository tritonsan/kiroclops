"""Target patch contract tests; no reference-game source is opened."""
from pathlib import Path
import tempfile
import unittest

from kiroclops.godot_patch import apply_profile


ROOT = Path(__file__).resolve().parents[1]


class ProfilePatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        for relative in (
            "source/characters/player/player.gd",
            "source/characters/player/state_machine/states/jump.gd",
        ):
            destination = self.project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / "vendor/slay" / relative).read_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def test_measured_units_and_original_other_jump_parameters(self):
        result = apply_profile(self.project, {
            "family": "held_velocity", "v0_body_s": 9.25,
            "gravity_body_s2": 85.5, "hold_ticks": 13,
        }, 20)
        self.assertEqual(result["parameters"]["v0_px_s"], 185)
        self.assertEqual(result["parameters"]["gravity_px_s2"], 1710)
        player = (self.project / "source/characters/player/player.gd").read_text()
        self.assertIn("var jump_velocity: float = -310.0", player)
        self.assertIn("var gravity: Vector2 = Vector2(0, 980)", player)
        self.assertEqual(len(result["changed_files"]), 2)
        with self.assertRaises(ValueError):
            apply_profile(self.project, {
                "family": "held_velocity", "v0_body_s": 9.25,
                "gravity_body_s2": 85.5, "hold_ticks": 13,
            }, 20)

    def test_ballistic_profile_accepts_zero_hold(self):
        result = apply_profile(self.project, {
            "family": "ballistic", "v0_body_s": 10,
            "gravity_body_s2": 80, "hold_ticks": 0,
        }, 20)
        self.assertEqual(result["parameters"]["hold_ticks"], 0)

    def test_invalid_measurement_does_not_mutate_target(self):
        player_path = self.project / "source/characters/player/player.gd"
        original = player_path.read_bytes()
        with self.assertRaises(ValueError):
            apply_profile(self.project, {
                "family": "held_velocity", "v0_body_s": float("nan"),
                "gravity_body_s2": 80, "hold_ticks": 10,
            }, 20)
        self.assertEqual(player_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
