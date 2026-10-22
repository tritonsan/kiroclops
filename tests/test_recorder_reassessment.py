"""Recorder qualification must precede any sealed candidate reassessment."""
import tempfile
import unittest
from pathlib import Path
from scripts.reassess_panel_recorder import run
from kiroclops.local_engine.media_session import LIMITS
from kiroclops.visual.schema import write_json


class RecorderReassessmentTests(unittest.TestCase):
    def test_failed_reference_gate_never_creates_an_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);q=root/'qualification';out=root/'new-run'
            write_json(q/'result.json',{'status':'fail','passed':6})
            write_json(q/'protocol.json',{'limits':LIMITS})
            with self.assertRaisesRegex(ValueError,'Full reference qualification'):
                run(q,out)
            self.assertFalse(out.exists())

    def test_changed_limits_cannot_be_promoted_into_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);q=root/'qualification';out=root/'new-run'
            write_json(q/'result.json',{'status':'pass','passed':7})
            write_json(q/'protocol.json',{'limits':{**LIMITS,'invented_relaxation':True}})
            with self.assertRaisesRegex(ValueError,'Full reference qualification'):
                run(q,out)
            self.assertFalse(out.exists())

    def test_historical_output_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);q=root/'qualification';out=root/'existing';out.mkdir()
            sentinel=out/'failure.txt';sentinel.write_text('Preserved failure')
            write_json(q/'result.json',{'status':'pass','passed':7})
            write_json(q/'protocol.json',{'limits':LIMITS})
            with self.assertRaisesRegex(ValueError,'Fresh output'):
                run(q,out)
            self.assertEqual(sentinel.read_text(),'Preserved failure')
