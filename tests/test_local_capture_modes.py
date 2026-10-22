import unittest
from kiroclops.local_engine.capture_modes import reference_capture_driver,LEGACY_DRIVER,PANEL_DRIVER
from kiroclops.visual.schema import VisualError
from kiroclops.web_local.specs import validate_scenario


class CaptureModeTests(unittest.TestCase):
    def test_old_references_keep_legacy_driver_without_rewriting(self):
        for domain in ('web_panel','svg'):
            self.assertEqual(reference_capture_driver(domain,{}),LEGACY_DRIVER)

    def test_new_panel_reference_keeps_its_qualified_recorder(self):
        self.assertEqual(reference_capture_driver('web_panel',{'metadata':{'scenario':{'frame_driver':PANEL_DRIVER}}}),PANEL_DRIVER)

    def test_unqualified_driver_or_domain_cannot_silently_fall_back(self):
        for domain,driver in [('svg',PANEL_DRIVER),('web_panel','natural_browser_v1'),('web_panel','unknown')]:
            with self.assertRaises(VisualError):reference_capture_driver(domain,{'metadata':{'scenario':{'frame_driver':driver}}})

    def test_panel_calibration_and_validation_accept_qualified_driver(self):
        for split in ('calibration','validation'):
            scenario={'domain':'panel','frame_driver':PANEL_DRIVER,'split':split,'viewport':[960,640],
                'duration_s':2.,'events':[{'action':'click','time_s':.4,'selector':'#open-panel'}]}
            self.assertEqual(validate_scenario(scenario)['frame_driver'],PANEL_DRIVER)

    def test_svg_model_trial_remains_unqualified(self):
        with self.assertRaisesRegex(ValueError,'qualification-only'):
            validate_scenario({'domain':'svg','frame_driver':PANEL_DRIVER,'split':'calibration'})
