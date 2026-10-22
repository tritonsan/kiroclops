import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiroclops.local_engine import suite_readiness as ready
from kiroclops.visual.schema import VisualError, digest, read_json, write_json


class DomainRawAdmissionTests(unittest.TestCase):
    def test_game_and_svg_require_their_independent_pixel_review(self):
        for domain in ('game', 'svg'):
            with self.subTest(domain=domain), patch(
                    'kiroclops.local_engine.suite_generic_raw_review.validate_review',
                    return_value={'status': 'verified'}) as validate:
                suite={'domain': domain}
                self.assertEqual(ready._raw_review('raw', 'suite', suite), {'status': 'verified'})
                validate.assert_called_once_with('raw', 'suite', suite)

    def test_generic_review_failure_cannot_fall_back_to_panel(self):
        with patch('kiroclops.local_engine.suite_generic_raw_review.validate_review',
                   side_effect=VisualError('environment_not_ready', 'missing event support')):
            with self.assertRaises(VisualError):
                ready._raw_review('raw', 'suite', {'domain': 'game'})

    def test_panel_v3_requires_explicit_scoring_admission(self):
        from kiroclops.local_engine.suite_raw_review_v3 import VERSION
        with tempfile.TemporaryDirectory() as directory:
            write_json(Path(directory)/'result.json', {'version': VERSION})
            with patch('kiroclops.local_engine.suite_raw_review_v3.validate_admission',
                       return_value={'whole_raw_review_accepted': False}) as validate:
                suite={'domain':'web_panel'}
                self.assertEqual(ready._raw_review(directory, 'suite', suite),
                                 {'whole_raw_review_accepted': False})
                validate.assert_called_once_with(directory, 'suite', suite)

    def test_panel_v3_failure_has_no_legacy_fallback(self):
        from kiroclops.local_engine.suite_raw_review_v3 import VERSION
        with tempfile.TemporaryDirectory() as directory:
            write_json(Path(directory)/'result.json', {'version': VERSION, 'numeric_status':'pass'})
            with patch('kiroclops.local_engine.suite_raw_review_v3.validate_admission',
                       side_effect=VisualError('environment_not_ready', 'uninspected pixels')):
                with self.assertRaises(VisualError):
                    ready._raw_review(directory, 'suite', {'domain':'web_panel'})


class WarmupAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        attempts=[]
        for n in (1,2):
            record={'package':'pixels','measurement_status':'ok','scenario':{'id':'warmup'}}
            directory=self.root/f'attempt-{n:02}'
            write_json(directory/'record.json',record)
            a={'attempt':n,'directory':str(directory),'status':'pass','capture_result':{
                'status':'ok','timing':{'status':'pass','errors':[]},'record_sha256':digest(record)}}
            attempts.append(a);write_json(self.root/f'attempt-{n:02}-result.json',a)
        self.receipt={'kind':'LocalWarmupReadiness','status':'pass','evaluated':False,'model_started':False,
            'policy':{'maximum_attempts':6,'required_consecutive_clean':2},'attempts':attempts,'consecutive_clean':2}
        write_json(self.root/'result.json',self.receipt)

    def tearDown(self):self.temp.cleanup()

    def check(self):
        with patch('kiroclops.visual.package.load_package',return_value={}) as load:
            result=ready._warmups(self.root/'result.json')
            self.assertEqual(load.call_count,2)
            return result

    def test_requires_two_actual_bound_records(self):self.assertEqual(self.check(),self.receipt)

    def test_changed_record_rejected(self):
        p=self.root/'attempt-02/record.json';value=read_json(p);value['measurement_status']='fail';write_json(p,value)
        with self.assertRaises(VisualError):self.check()

    def test_missing_attempt_receipt_rejected(self):
        (self.root/'attempt-01-result.json').unlink()
        with self.assertRaises((VisualError,OSError)):self.check()

    def test_broader_warmup_budget_rejected(self):
        self.receipt['policy']['maximum_attempts']=12;write_json(self.root/'result.json',self.receipt)
        with self.assertRaises(VisualError):self.check()

    def test_single_good_capture_is_not_ready(self):
        self.receipt['attempts']=self.receipt['attempts'][:1];write_json(self.root/'result.json',self.receipt)
        with self.assertRaises(VisualError):self.check()

    def test_failing_final_attempt_cannot_be_presented_as_consecutive(self):
        self.receipt['attempts'][-1]['status']='fail'
        write_json(self.root/'attempt-02-result.json',self.receipt['attempts'][-1]);write_json(self.root/'result.json',self.receipt)
        with self.assertRaises(VisualError):self.check()


if __name__=='__main__':unittest.main()
