"""A visible SVG clock is an indivisible capture policy, never a silent fallback."""
from copy import deepcopy
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from kiroclops.web_local.specs import scenarios,validate_scenario
from kiroclops.local_engine.capture_modes import reference_capture_policy
from kiroclops.visual.schema import VisualError

POLICY={'frame_driver':'natural_browser_raf_v1','analysis_policy':'observed_response_v2','visual_frame_clock':'svg_raf_tag_v1'}


class SVGAdmissionTests(unittest.TestCase):
    def test_complete_clock_policy_supports_calibration_and_validation(self):
        for split in ('calibration','validation'):
            for scenario in scenarios('svg',split):
                scenario.update(POLICY);validate_scenario(scenario)

    def test_missing_policy_components_cannot_enter_model_capture(self):
        for key in POLICY:
            scenario=scenarios('svg','calibration')[0];scenario.update(POLICY);scenario.pop(key)
            with self.assertRaises(ValueError):validate_scenario(scenario)

    def test_reference_policy_binds_actual_frame_clock(self):
        manifest={'metadata':{'scenario':dict(POLICY)},'frames':[{'timestamp_source':'svg_visible_raf_batch'}]}
        self.assertEqual(reference_capture_policy('svg',manifest),POLICY)
        wrong=deepcopy(manifest);wrong['frames'][0]['timestamp_source']='cdp'
        with self.assertRaises(VisualError):reference_capture_policy('svg',wrong)
        missing=deepcopy(manifest);missing['metadata']['scenario'].pop('visual_frame_clock')
        with self.assertRaises(VisualError):reference_capture_policy('svg',missing)

    def test_legacy_and_panel_keep_their_existing_paths(self):
        self.assertEqual(reference_capture_policy('svg',{'metadata':{}}),{'frame_driver':'controlled_begin_frame_v1'})
        self.assertEqual(reference_capture_policy('web_panel',{'metadata':{'scenario':{'frame_driver':'natural_browser_raf_v1'}}}),{'frame_driver':'natural_browser_raf_v1'})
        with self.assertRaises(VisualError):reference_capture_policy('web_panel',{'metadata':{'scenario':POLICY}})

    def test_metadata_stripping_and_mixed_clock_never_downgrade(self):
        for scenario,frames in [({},[{'timestamp_source':'svg_visible_raf_batch'}]),
                (POLICY,[]),(POLICY,[{'timestamp_source':'svg_visible_raf_batch'},{}])]:
            with self.assertRaises(VisualError):
                reference_capture_policy('svg',{'metadata':{'scenario':scenario},'frames':frames})

    def test_target_lifetime_comes_from_its_pixels_in_the_qualified_mode(self):
        from kiroclops.local_engine.media_session import _target_record
        obj={'id':'a','active_intervals_s':[[.12,2.]]}
        detected={'objects':[obj],'connections':[]}
        binding={'scenario':{'domain':'svg',**POLICY},'object_map':{'a':'a'},
            'connections':[],'lifecycle_bindings':{'a':'not used'}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch('kiroclops.local_engine.evaluation._capture_web',return_value={'package':tmp,'source_sha256':'code'}), \
                patch('kiroclops.web_local.selection.select_package_from_pixels',return_value=detected), \
                patch('kiroclops.local_engine.media_session.load_package',return_value={}), \
                patch('kiroclops.local_engine.media_session._preserve_point_semantics',side_effect=lambda r,t,m:deepcopy(t)), \
                patch('kiroclops.local_engine.evaluation._map_lifecycle',side_effect=AssertionError('Do not copy the reference lifetime')), \
                patch('kiroclops.local_engine.evaluation._analysis',return_value={'measurement_status':'ok','observations':{},'events':{}}):
            result=_target_record(Path(tmp),Path(tmp)/'out',binding,{'selection':{'objects':[obj]}},lambda:None,lambda *a:None,lambda:False,'candidate',analyze=True)
            self.assertEqual(result['selection']['objects'][0]['active_intervals_s'],[[.12,2.]])


if __name__=='__main__':unittest.main()
