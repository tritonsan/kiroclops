from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.web_local.specs import scenarios,capture_scenarios,QUALIFIED_CAPTURE_POLICY,SVG_CAPTURE_POLICY
from kiroclops.local_engine.evaluation import _web_matrix
from kiroclops.local_engine.cohort import _prepare_stage
from kiroclops.local_engine.web_reference import reference_time_coverage


class QualifiedCohortPolicy(unittest.TestCase):
    def test_legacy_defaults_and_input_recipes_unchanged(self):
        for domain in ('panel','svg'):
            for split in ('calibration','validation'):
                old=scenarios(domain,split);new=capture_scenarios(domain,split,QUALIFIED_CAPTURE_POLICY)
                self.assertEqual(len(old),len(new))
                for a,b in zip(old,new):
                    self.assertNotIn('frame_driver',a)
                    self.assertEqual(a,{k:v for k,v in b.items() if k not in SVG_CAPTURE_POLICY})
                    self.assertEqual('natural_browser_raf_v1',b['frame_driver'])
                    if domain=='svg':self.assertTrue(all(b[k]==v for k,v in SVG_CAPTURE_POLICY.items()))
                    else:self.assertNotIn('visual_frame_clock',b)

    def test_unknown_policy_has_no_capture_side_effect(self):
        with patch('kiroclops.local_engine.evaluation._capture_web') as capture:
            with self.assertRaises(ValueError):
                _web_matrix('source','out','svg','calibration',lambda:None,lambda *a:None,lambda:False,'candidate',capture_policy='typo')
            capture.assert_not_called()

    def test_all_candidate_captures_use_registered_policy(self):
        with patch('kiroclops.local_engine.evaluation._capture_web',side_effect=lambda *args,**kw:args[2]) as capture:
            rows=_web_matrix('source','out','svg','validation',lambda:None,lambda *a:None,lambda:False,'candidate',capture_policy=QUALIFIED_CAPTURE_POLICY)
        self.assertEqual(12,capture.call_count)
        # V6 plan WP2.1: every row also lists its recording attempts (here one clean attempt).
        self.assertTrue(all(r['recording_retries']==[{'attempt':1,'status':'ok'}] for r in rows))
        self.assertEqual(capture_scenarios('svg','validation',QUALIFIED_CAPTURE_POLICY),
                         [{k:v for k,v in r.items() if k!='recording_retries'} for r in rows])

    def test_registered_reference_policy_reaches_both_arms(self):
        with tempfile.TemporaryDirectory() as t:
            registry={'web_reference_bundle':{'manifest':'references.json','manifest_sha256':'sha','capture_policy':QUALIFIED_CAPTURE_POLICY},
                      'goals':{'svg':'goal'}}
            for mode in ('tools','baseline'):
                with patch('kiroclops.local_engine.cohort.create_session') as create,patch('kiroclops.local_engine.cohort.configure'):
                    _prepare_stage(registry,{'domain':'svg','mode':mode},{'stage':'svg','session':str(Path(t)/mode)})
                exp=create.call_args.kwargs['experiment']
                self.assertEqual(mode,create.call_args.kwargs['mode'])
                self.assertEqual(QUALIFIED_CAPTURE_POLICY,exp['capture_policy'])
                self.assertEqual(capture_scenarios('svg','calibration',QUALIFIED_CAPTURE_POLICY),exp['calibration'])

    def test_observed_boundary_keeps_actual_unobserved_tail_explicit(self):
        events=[{'time_s':.2,'action':'click','selector':'#add-node'},{'time_s':.4,'action':'click','selector':'#add-node'}]
        manifest={'inputs':{'provenance':'recorder_verified','events':events},
                  'frames':[{'time_s':t} for t in (0,.08,.16,.24,.32,.37,.41,.49,.57,.65,.73,.81)],
                  'metadata':{'scenario':{'events':deepcopy(events)}}}
        old=reference_time_coverage(manifest)
        self.assertEqual('fail',old['status'])
        manifest['metadata']['scenario'].update(SVG_CAPTURE_POLICY)
        new=reference_time_coverage(manifest)
        self.assertEqual('pass',new['status'])
        self.assertEqual(.37,new['windows'][0]['end_s'])
        self.assertEqual(.4,new['windows'][0]['next_input_s'])
        # A hole inside the measured response remains a failure.
        manifest['frames']=[f for f in manifest['frames'] if f['time_s'] not in (.24,.32)]
        self.assertEqual('fail',reference_time_coverage(manifest)['status'])


if __name__=='__main__':unittest.main()
