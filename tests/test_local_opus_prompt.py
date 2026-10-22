"""The two native arms must share the repaired authoring clock and fair inputs."""
from pathlib import Path
import tempfile
import unittest

from kiroclops.local_engine.launcher import configure, load_config
from kiroclops.local_engine.session import create_session
from kiroclops.visual.schema import read_json


class LocalOpusPromptTests(unittest.TestCase):
    def test_generated_controls_share_budget_and_prioritize_early_candidate(self):
        with tempfile.TemporaryDirectory(prefix="opus café ") as temporary:
            root = Path(temporary)
            original = root / "clean source"
            (original / "src").mkdir(parents=True)
            (original / "src/panel-motion.js").write_text(
                "export const cleanTarget = true;", encoding="utf-8"
            )
            reference = root / "private reference"
            reference.mkdir()
            (reference / "implementation.js").write_text(
                "PRIVATE_REFERENCE_IMPLEMENTATION", encoding="utf-8"
            )
            controls = {}
            for mode in ("tools", "baseline"):
                session = root / f"{mode} session"
                create_session(session, "web_panel", original, reference,
                               "Match the panel motion", mode=mode,
                               experiment={"capture_lock": str(root / "capture.lock")})
                config = configure(session)
                load_config(session)  # The real generated controls pass their own integrity check.
                agent = read_json(config["agent_path"])
                instructions = (Path(config["target_dir"]) / ".kiro" / "instructions.md").read_text("utf-8")
                prompt = Path(config["prompt_path"]).read_text("utf-8")
                self.assertEqual(instructions, agent["prompt"])
                self.assertEqual("claude-opus-5.5", agent["model"])
                self.assertEqual("max", agent["effortLevel"])
                self.assertEqual(("claude-opus-5.5", "max"), (config["model"], config["effort"]))
                self.assertIn("Model: claude-opus-5.5; effort: max.", prompt)
                self.assertIn("submit the first candidate early", prompt)
                self.assertNotIn("PRIVATE_REFERENCE_IMPLEMENTATION", instructions + prompt)
                self.assertNotIn("export const cleanTarget = true", instructions + prompt)
                controls[mode] = (instructions, prompt, agent)

            tool_instructions, _, tool_agent = controls["tools"]
            baseline_instructions, _, baseline_agent = controls["baseline"]
            # The same clock, candidate cap and action priority apply to both arms.
            shared = tool_instructions.split("\n\n", 1)[0]
            self.assertEqual(shared, baseline_instructions.split("\n\n", 1)[0])
            self.assertIn("1800 seconds", shared)
            self.assertIn("at most 3 submitted candidates and 1800 seconds of model authoring per stage", shared)
            self.assertIn("candidate 01 by about minute 15", shared)
            self.assertIn("includes waiting for preparation and candidate evaluation", shared)
            self.assertIn("candidate evaluation as the first application run check", shared)

            # The tool arm gets measurements and prioritizes using them. The
            # baseline receives only neutral raw-media commands.
            self.assertIn("returned measured profile, units and confidence", tool_instructions)
            self.assertIn("Submit candidate 01 to evaluate_candidate early", tool_instructions)
            self.assertIn("instead of fully re-tracking and re-fitting", tool_instructions)
            self.assertIn("Tools never generate a patch", tool_instructions)
            self.assertIn("@kiroclops", tool_agent["tools"])
            self.assertNotIn("@kiroclops", baseline_agent["tools"])
            self.assertEqual({}, baseline_agent["mcpServers"])
            self.assertIn("Calibration commands give no motion profile or numeric guidance", baseline_instructions)
            self.assertIn("Submit your first candidate early enough", baseline_instructions)
            self.assertIn("Neutral commands:", baseline_instructions)
            self.assertNotIn("Neutral commands:", tool_instructions)


if __name__ == "__main__":
    unittest.main()
