"""Private fixed-baseline matrices using local PNGs, without native capture."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import media_suite, suite_revision as revision, suite_web
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.schema import VisualError, digest
from scripts import phase3d_cache as cache
from tests.test_suite_revision import Fixture


class CacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = Fixture(self.root)
        self.fixture.selection.update(lifecycle_bindings={'panel': [{'start': {'kind': 'clip_start'}, 'end': {'kind': 'clip_end'}}]},
                                      input_origin_s=0.)
        self.fixture.selection['objects'][0]['active_intervals_s'] = [[0., .2]]
        self.code = media_suite.analysis_source_hashes()
        patches = [patch.object(media_suite, 'analysis_source_hashes', return_value=self.code),
                   patch.object(revision, 'measurement_source_hashes', return_value=self.fixture.sources),
                   patch.object(cache, 'measurement_source_hashes', return_value=self.fixture.sources),
                   patch.object(revision, 'service_sources', return_value=self.fixture.sources),
                   patch.object(revision, 'engine_identity', return_value=self.fixture.engine),
                   patch.object(cache, '_capture', side_effect=self.capture),
                   patch('kiroclops.local_engine.evaluation._analysis', side_effect=self.fixture.analyze),
                   patch('kiroclops.web_local.selection.select_package_from_pixels', side_effect=lambda *args: deepcopy(self.fixture.selection))]
        self.mocks = [item.start() for item in patches]
        for item in reversed(patches):
            self.addCleanup(item.stop)

    def record(self, path, scenario, project=None):
        original = self.fixture.scenario
        try:
            self.fixture.scenario = deepcopy(scenario)
            return self.fixture.record(path, project=project)
        finally:
            self.fixture.scenario = original

    def capture(self, project, case, destination, reference, check, progress, cancel, runtime_root):
        return self.record(destination, case['scenario'], project)

    def suite(self, *, retained=False):
        rows = []
        for role, split in (('calibration', 'calibration'), ('regression', 'known_regression'), ('validation', 'validation')):
            scenario = suite_web.scenarios('panel', split)[0]
            rows.append({'case_id': scenario['case_id'], 'role': role, 'stage': 'panel',
                         'record': self.record(self.root / 'reference' / role, scenario)})
        calibration, regression = rows[0], rows[1]
        initial = [{'case_id': calibration['case_id'], 'role': role,
                    'record': self.record(self.root / 'initial' / role, calibration['record']['scenario'], project)}
                   for role, project in (('original', self.fixture.original), ('before', self.fixture.parent))]
        baseline = [{'case_id': regression['case_id'], 'role': 'before',
                     'record': self.record(self.root / 'retained', regression['record']['scenario'])}] if retained else []
        path = self.root / 'suite'
        value = media_suite.create_suite(path, domain='web_panel', stage='panel', records=rows,
            required_cases=[{'case_id': item['case_id'], 'role': item['role']} for item in rows],
            original=self.fixture.original, authoring_parent=self.fixture.parent, goal='Match the visible responses',
            event_contract=suite_web.event_contract('panel'), initial_records=initial, baseline_records=baseline)
        return path, value

    def test_complete_cache_captures_missing_roles_only_and_keeps_calibration_private_unchanged(self):
        old_path, old = self.suite(retained=True)
        old_files = source_hashes(old_path)
        result = cache.cache_baselines(old_path, self.root / 'cache', self.fixture.parent)
        new = media_suite.load_suite(result['suite'])
        self.assertEqual(3, result['new_captures'])
        self.assertEqual(1, result['retained_captures'])
        self.assertEqual(4, len(new['baseline_records']))
        self.assertEqual(old_files, source_hashes(old_path))
        self.assertTrue(result['measurement_revision_required'])
        self.assertFalse(result['exposed_to_model'])
        self.assertFalse(result['candidate_captured'])
        self.assertEqual(old['required_cases'], new['required_cases'])
        self.assertEqual([(item['case_id'], item['role']) for item in old['initial_records']],
                         [(item['case_id'], item['role']) for item in new['initial_records']])
        for item in new['baseline_records']:
            record = media_suite.record_from_suite(result['suite'], item)
            if (item['role'], item['case_id']) != ('before', old['cases'][1]['case_id']):
                revision.verify_measurement_record(record, 'panel', expected_sources=self.fixture.sources)

    def test_second_cache_reuses_every_fixed_baseline_without_new_captures(self):
        old_path, _ = self.suite()
        first = cache.cache_baselines(old_path, self.root / 'first', self.fixture.parent)
        self.mocks[5].reset_mock()
        second = cache.cache_baselines(first['suite'], self.root / 'second', self.fixture.parent)
        self.assertEqual(0, second['new_captures'])
        self.assertEqual(4, second['retained_captures'])
        self.assertFalse(self.mocks[5].called)

    def test_canonical_revision_copies_fresh_stamped_cache_and_reanalyzes_old_refs_initials(self):
        old_path, _ = self.suite()
        cached = cache.cache_baselines(old_path, self.root / 'cache', self.fixture.parent)
        self.mocks[6].reset_mock()
        result = revision.revise_suite(cached['suite'], self.root / 'canonical', self.fixture.parent)
        new = media_suite.load_suite(result['suite'])
        self.assertEqual(4, result['copied_records'])
        self.assertEqual(5, result['reanalyzed_records'])
        self.assertEqual(5, self.mocks[6].call_count)
        self.assertEqual(4, result['private_baseline_records'])
        self.assertFalse(result['measurement_revision_required'])
        for item in new['cases'] + new['initial_records'] + new['baseline_records']:
            revision.verify_measurement_record(media_suite.record_from_suite(result['suite'], item), 'panel',
                                              expected_sources=self.fixture.sources)

    def test_captured_stale_source_is_rejected_before_cache_publication(self):
        old_path, _ = self.suite()
        def stale(*args, **kwargs):
            record = self.capture(*args, **kwargs)
            record['source_sha256'] = digest('foreign source')
            return record
        self.mocks[5].side_effect = stale
        with self.assertRaises(VisualError):
            cache.cache_baselines(old_path, self.root / 'stale', self.fixture.parent)
        self.assertFalse((self.root / 'stale/suite/suite.json').exists())

    def test_captured_foreign_case_is_rejected(self):
        old_path, _ = self.suite()
        def foreign(*args, **kwargs):
            record = self.capture(*args, **kwargs)
            record['scenario']['case_id'] = 'foreign'
            return record
        self.mocks[5].side_effect = foreign
        with self.assertRaises(VisualError):
            cache.cache_baselines(old_path, self.root / 'foreign', self.fixture.parent)
        self.assertFalse((self.root / 'foreign/suite/suite.json').exists())

    def test_changed_authoring_parent_rejects_without_capture(self):
        old_path, _ = self.suite()
        (self.fixture.parent / 'index.html').write_text('changed', encoding='utf-8')
        self.mocks[5].reset_mock()
        with self.assertRaises(VisualError) as caught:
            cache.cache_baselines(old_path, self.root / 'changed', self.fixture.parent)
        self.assertEqual('wrong_authoring_parent', caught.exception.code)
        self.assertFalse(self.mocks[5].called)


class WrapperTests(unittest.TestCase):
    def test_started_session_is_rejected_before_runtime_or_capture(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cache, 'Session') as session, \
                patch.object(cache, 'prepare_runtime') as runtime, patch.object(cache, 'cache_baselines') as captures:
            session.return_value._load.return_value = {'started_at': 1., 'candidates': [],
                                                       'experiment': {'reference_mode': 'media_suite'}}
            with self.assertRaises(VisualError) as caught:
                cache.run(Path(temporary) / 'previous', 'cache1', Path(temporary) / 'runtime')
            self.assertEqual('session_started', caught.exception.code)
            self.assertFalse(runtime.called)
            self.assertFalse(captures.called)

    def test_native_capture_routes_preserved_case_stage_and_before_role(self):
        scenario = {'mechanic': 'jump', 'case_id': 'preserved-jump'}
        with patch('kiroclops.local_engine.suite_game.capture_target', return_value={'ok': True}) as native:
            cache._capture('parent', {'stage': 'jump', 'scenario': scenario}, 'output', {}, lambda: None,
                           lambda *args: None, None, Path('runtime'))
        self.assertEqual('jump', native.call_args.kwargs['stage'])
        self.assertEqual('before', native.call_args.kwargs['variant'])
        self.assertEqual(Path('runtime/runtime/game'), native.call_args.kwargs['runtime_root'])


if __name__ == '__main__':
    unittest.main()
