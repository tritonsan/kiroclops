"""Public result transport stays bounded, lossless, and bound to its job."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine.job_feedback import compact_job_feedback, MAX_RESPONSE_CHARS
from kiroclops.local_engine.session import Session
from kiroclops.visual.schema import VisualError, digest, file_sha256, read_json, write_json


class JobFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='public feedback naïve ')
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'target with spaces'
        self.public = dict(job_id='a'*32, operation='evaluate', status='succeeded',
                           candidate_id='candidate-02', source_sha256='b'*64,
                           result=dict(eligible=False, score=.04, samples=['é'*200]*120))

    def test_lossless_hash_bound_export_and_bounded_inline_response(self):
        original = deepcopy(self.public)
        response = compact_job_feedback(self.public, self.target)
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=True, indent=2)), MAX_RESPONSE_CHARS)
        link = response['result']['full_feedback']
        path = Path(link['path'])
        self.assertTrue(path.is_relative_to(self.target.resolve()))
        saved = read_json(path)
        self.assertEqual(original['result'], saved['result'])
        self.assertEqual(original, self.public)
        self.assertEqual(digest(original['result']), link['result_sha256'])
        self.assertEqual(file_sha256(path), link['sha256'])
        for key in ('job_id', 'candidate_id', 'source_sha256'):
            self.assertEqual(original[key], saved[key])
            self.assertEqual(original[key], link[key])
        self.assertFalse(response['result']['eligible'])
        self.assertEqual(.04, response['result']['score'])
        self.assertEqual(response, compact_job_feedback(original, self.target))

    def test_each_identity_and_result_change_has_distinct_export(self):
        seen = set()
        for key, value in ((None, None), ('job_id','c'*32), ('candidate_id','candidate-03'),
                           ('source_sha256','d'*64), ('result', {'samples':['x'*200]*120})):
            public = deepcopy(self.public)
            if key: public[key] = value
            link = compact_job_feedback(public, self.target)['result']['full_feedback']
            self.assertNotIn(link['path'], seen)
            seen.add(link['path'])

    def test_tampered_export_is_rejected_without_replacing_it(self):
        link = compact_job_feedback(self.public, self.target)['result']['full_feedback']
        path = Path(link['path'])
        write_json(path, {'tampered':True})
        with self.assertRaises(VisualError) as caught:
            compact_job_feedback(self.public, self.target)
        self.assertEqual('feedback_changed', caught.exception.code)
        self.assertEqual({'tampered':True}, read_json(path))

    def test_no_export_or_heldout_leak_when_result_deferred(self):
        public = {**self.public, 'status':'deferred_to_runner', 'result':None}
        session = Session.__new__(Session)
        with patch.object(Session, 'get_job', return_value=public) as get_job, \
             patch.object(Session, '_load', return_value={'target':str(self.target)}):
            self.assertEqual(public, session.get_job_feedback('a'*32, .5))
        get_job.assert_called_once_with('a'*32, .5)
        self.assertFalse(self.target.exists())

    def test_running_failed_and_small_results_are_unchanged(self):
        for result, status in ((None,'running'),(None,'failed'),({'neutral_capture':True},'succeeded')):
            public = {**self.public, 'status':status, 'result':result}
            self.assertEqual(public, compact_job_feedback(public, self.target))
        self.assertFalse(self.target.exists())

    def test_neutral_arm_does_not_gain_profiles_or_measurements(self):
        public = {**self.public, 'result':{'neutral_capture':True,'media_paths':['frames/'+str(i)+'.png' for i in range(4000)]}}
        response = compact_job_feedback(public, self.target)
        self.assertTrue(response['result']['neutral_capture'])
        self.assertNotIn('profile', response['result'])
        self.assertNotIn('comparisons', response['result'])
        self.assertEqual(public['result'],read_json(response['result']['full_feedback']['path'])['result'])

    def test_foreign_job_path_is_rejected(self):
        with self.assertRaises(VisualError) as caught:
            compact_job_feedback({**self.public,'job_id':'../outside'}, self.target)
        self.assertEqual('invalid_job_id', caught.exception.code)

    def test_export_is_readable_but_not_writable_under_existing_guard(self):
        from kiroclops.local_engine.launcher import _guard_policy
        path=compact_job_feedback(self.public,self.target)['result']['full_feedback']['path']
        config={'target_dir':str(self.target.resolve())}
        state={'sealed_at':None,'domain':'web_panel'}
        args={'path':path}
        self.assertTrue(_guard_policy(self.target.parent,config,state,{'tool_name':'fs_read','tool_input':args})['allow'])
        self.assertFalse(_guard_policy(self.target.parent,config,state,{'tool_name':'fs_write','tool_input':args})['allow'])

    def test_large_profile_and_comparison_explicitly_omit_inline_only(self):
        public = deepcopy(self.public)
        public['result']['profile']={'profiles':[{'objects':[{'id':'o'*120,'valid_ratio':1}]*200}]*10}
        public['result']['comparisons']=[{'pass':False,'windows':[{'object_windows':[
            {'pass':False,'reference_id':'object','position_rmse':.1}]*300}]}]*10
        response = compact_job_feedback(public,self.target)
        self.assertTrue(response['result']['additional_fields_omitted'])
        self.assertLessEqual(len(json.dumps(response,ensure_ascii=True,indent=2)),MAX_RESPONSE_CHARS)
        self.assertEqual(public['result'],read_json(response['result']['full_feedback']['path'])['result'])


if __name__ == '__main__':
    unittest.main()
