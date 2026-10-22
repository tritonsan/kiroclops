"""The same ownership/identity regressions, plus the only v3 presentation delta."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.integration import isolated_runtime_v3 as bridge
from kiroclops.integration import isolated_runtime_v2 as previous
from kiroclops.integration import runner

# Load the immutable v2 mock scenarios into a private module namespace. This
# avoids modifying the original test module's globals or duplicating its proof
# fixtures, while exercising every guard against the separately versioned v3.
_spec = importlib.util.spec_from_file_location("_v3_inherited_mock_scenarios", Path(__file__).with_name("test_isolated_runtime_v2.py"))
_scenarios = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scenarios)
_scenarios.bridge = bridge

class IsolatedRuntimeV3Tests(_scenarios.IsolatedRuntimeTests):
    def receipt(self):
        return json.loads((self.out / ("engine." + bridge.VERSION) / "bridge.json").read_text(encoding="utf-8"))

    def test_real_funnels_route_to_direct_private_engine_and_restore(self):
        for number, scene in enumerate(bridge.SCENES):
            with self.subTest(scene=scene):
                folder=self.out/str(number); folder.mkdir()
                cfg=folder/"capture_config.json"; cfg.write_text(json.dumps({"output_dir":str(folder)}))
                with self.context() as runtime:
                    self.assertEqual(self.call(self.argv(scene),log_path=folder/"engine.log",env={**self.env,"KIROCLOPS_CONFIG":str(cfg)}),0)
                args,kwargs=self.private.call_args
                self.assertEqual(args[0],[str(self.gui),*[str(x) for x in self.argv(scene)[1:]],"--disable-vsync"])
                self.assertEqual(kwargs["cwd"],self.project)
                self.assertEqual(bridge.verify_runtime_receipt(runtime.receipts[0])["status"],"pass")
        self.assertIs(runner._run_process,self.original)
        self.original.assert_not_called()

    def test_presentation_change_is_one_exact_argument_and_bound_policy(self):
        with self.context() as runtime:
            self.call()
        receipt=self.receipt()
        self.assertEqual(receipt["requested_argv"], [str(x) for x in self.argv()])
        self.assertEqual(receipt["executed_argv"], [str(self.gui), *[str(x) for x in self.argv()[1:]], "--disable-vsync"])
        self.assertEqual(receipt["presentation_policy"], bridge.PRESENTATION_POLICY)
        self.assertEqual(bridge.verify_runtime_receipt(runtime.receipts[0])["status"], "pass")
        self.assertEqual(self.before, runner.source_hashes(self.project))

    def test_supplied_presentation_or_timing_override_cannot_escape_classifier(self):
        with self.context():
            for option in (["--disable-vsync"], ["--max-fps", "0"], ["--time-scale", "2"], ["--fixed-fps", "120"]):
                with self.subTest(option=option), self.assertRaises(runner.RunnerError):
                    self.call(self.argv()+option)
        self.private.assert_not_called()
        self.original.assert_not_called()

    def test_forged_missing_presentation_flag_is_rejected_despite_valid_outer_hashes(self):
        with self.context() as runtime:
            self.call()
        path=runtime.receipts[0]
        receipt=self.receipt()
        receipt["executed_argv"].remove("--disable-vsync")
        path.write_text(json.dumps(receipt))
        self.assertEqual(bridge.verify_runtime_receipt(path)["status"], "fail")

    def test_presentation_policy_change_cannot_verify(self):
        with self.context() as runtime:
            self.call()
        path=runtime.receipts[0]
        receipt=self.receipt(); receipt["presentation_policy"]["fixed_fps"]=120
        path.write_text(json.dumps(receipt))
        self.assertEqual(bridge.verify_runtime_receipt(path)["status"], "fail")

    def test_v2_and_v3_contexts_cannot_overlap(self):
        with self.context():
            with self.assertRaises(runner.RunnerError):
                with previous.isolated_runtime(project_roots=[self.project]):
                    self.fail("Nested v2 context was entered")
        with previous.isolated_runtime(project_roots=[self.project]):
            with self.assertRaises(runner.RunnerError):
                with self.context():
                    self.fail("Nested v3 context was entered")
        self.assertIs(runner._run_process,self.original)

    def test_pinned_inherited_bridge_change_blocks_before_process(self):
        with patch.object(bridge,"PREVIOUS_SHA256","0"*64), self.context(), self.assertRaises(runner.RunnerError):
            self.call()
        self.private.assert_not_called(); self.original.assert_not_called()

if __name__=="__main__": unittest.main()
