"""V3 ground closure: the suite check accepts only the frozen floor-aware ground profile."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kiroclops.local_engine import media_suite, suite_game, suite_revision
from kiroclops.local_engine.execution_policy import REVISED_ID, resolve_policy
from kiroclops.local_engine.media_suite import (GROUND_FLOOR_PROFILE, GROUND_FLOOR_RULE, GROUND_SPEED_PROFILE,
                                               GROUND_SPEED_RULE)
from kiroclops.visual.schema import VisualError, digest, write_json
from scripts import phase3d_v2_rollup_report as base
from scripts import phase3d_v3_ground_closure as closure
from scripts.phase3d_v2_prepare import recipes
from tests.evidence import requires_evidence
from tests.frozen_native_chain import EVIDENCE, REASON, V6_DIVERGENCE


def suite_folder(root, rows=None, **changes):
    cases = [{'case_id': c['case_id'], 'role': c['role'], 'scenario': c['scenario'], 'stage': c['stage'],
              'purpose': c['purpose']} for c in (rows if rows is not None else recipes('ground'))]
    sources = media_suite.analysis_source_hashes(2)
    suite = {'kind': 'ReferenceMediaSuite', 'schema_version': 2, 'stage': 'ground', 'calibration_frozen': True,
             'execution_policy': resolve_policy(REVISED_ID, 'ground'), 'cases': cases,
             'required_cases': [{'case_id': c['case_id'], 'role': c['role']} for c in cases],
             'analysis_source_hashes': sources, 'source_epoch': digest(sources),
             'acceptance_profile': GROUND_FLOOR_PROFILE, 'game_acceptance_rule': deepcopy(GROUND_FLOOR_RULE)}
    suite.update(changes)
    folder = Path(root) / 'suite'
    write_json(folder / 'suite.json', suite)
    write_json(folder / 'suite-binding.json', {'suite_sha256': digest(suite)})
    return folder


class SuiteCheckTests(unittest.TestCase):
    def test_floor_aware_ground_suite_passes(self):
        with tempfile.TemporaryDirectory() as root:
            suite = closure._suite_v3(base.Inputs(), suite_folder(root), 'ground')
            self.assertEqual(GROUND_FLOOR_PROFILE, suite['acceptance_profile'])

    def test_legacy_profile_changed_rule_or_other_stage_fails(self):
        changed = deepcopy(GROUND_FLOOR_RULE)
        changed['resolvability_factor'] = 1.
        for kwargs in ({'acceptance_profile': 'legacy-game-v1'}, {'game_acceptance_rule': changed},
                       {'game_acceptance_rule': None}):
            with self.subTest(kwargs=list(kwargs)), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(VisualError):
                    closure._suite_v3(base.Inputs(), suite_folder(root, **kwargs), 'ground')
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                closure._suite_v3(base.Inputs(), suite_folder(root), 'jump')

    def test_v4_profile_closes_only_against_the_v4_recipes(self):
        from scripts.phase3d_v4_ground import recipes as v4_recipes
        speed = {'acceptance_profile': GROUND_SPEED_PROFILE, 'game_acceptance_rule': deepcopy(GROUND_SPEED_RULE)}
        with tempfile.TemporaryDirectory() as root:
            suite = closure._suite_v3(base.Inputs(), suite_folder(root, v4_recipes(), **speed), 'ground')
            self.assertEqual(90, len(suite['cases']))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                closure._suite_v3(base.Inputs(), suite_folder(root, **speed), 'ground')
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                closure._suite_v3(base.Inputs(), suite_folder(root, v4_recipes(), acceptance_profile=GROUND_SPEED_PROFILE,
                                                            game_acceptance_rule=deepcopy(GROUND_FLOOR_RULE)), 'ground')

    @requires_evidence(*EVIDENCE)
    @unittest.skipIf(V6_DIVERGENCE, REASON)
    def test_reader_restores_the_v2_suite_and_measurement_checks(self):
        original = (base._suite, base._native_measurement_quality)
        with self.assertRaises(RuntimeError):
            with closure._reader():
                self.assertIs(closure._suite_v3, base._suite)
                self.assertIs(closure._native_measurement_quality_v4, base._native_measurement_quality)
                raise RuntimeError('boom')
        self.assertEqual(original, (base._suite, base._native_measurement_quality))

    def test_config_schema_is_explicit(self):
        good = {'kind': 'Phase3DV3GroundClosureInputs', 'schema_version': 1,
                'stage': {'stage': 'ground', 'session': 'C:/s', 'source_review': 'C:/r.json'},
                'final_raw': {'output': 'C:/f', 'parent_chain': 'C:/c.json', 'parent_chain_sha256': 'a' * 64},
                'normal_raw': {'output': 'C:/n'}}
        self.assertEqual(good, closure._config(good))
        for broken in ({**good, 'extra': 1}, {**good, 'stage': {'stage': 'jump', 'session': 'x', 'source_review': 'y'}},
                       {**good, 'normal_raw': {}}):
            with self.assertRaises(VisualError):
                closure._config(broken)


def measurement_session(root, *, unstamped=(), statuses=None):
    """A session folder with one record per role; suite-origin roles are stamped unless listed."""
    directory = Path(root)
    for job, roles in (('e', ('candidate',)), ('f', ('reference', 'original', 'before', 'final'))):
        for role in roles:
            analysis = directory / 'a' / f'{job}-{role}.json'
            write_json(analysis, {'role': role})
            package = directory / 'p' / f'{job}-{role}'
            write_json(package / 'manifest.json', {'role': role})
            record = {'scenario': {'case_id': 'c1'}, 'analysis': str(analysis), 'package': str(package),
                      'measurement_status': (statuses or {}).get(role, 'ok'), 'role': role}
            if role not in closure.SESSION_ROLES and role not in unstamped:
                record.update(measurement_provenance={'kind': 'SuitePixelMeasurement'}, measurement_source_hashes={'x': 'y'})
            write_json(directory / 'measurements' / job / f'{role}.json', [record])
    return directory


class SessionMeasurementTests(unittest.TestCase):
    def check(self, directory, profile=GROUND_SPEED_PROFILE):
        calls = {'binding': [], 'stamp': []}

        def binding(record, stage):
            calls['binding'].append(record['role'])
            return {}

        def stamp(record, stage, **_):
            calls['stamp'].append(record['role'])
            if record.get('measurement_provenance') is None:
                raise VisualError('measurement_provenance_missing', 'Per-record measurement provenance is missing.')
            return {}

        suite = {'acceptance_profile': profile, 'cases': [{'case_id': 'c1', 'stage': 'ground'}]}
        with mock.patch.object(suite_revision, '_record_binding', binding),                 mock.patch.object(suite_revision, 'verify_measurement_record', stamp),                 mock.patch.object(suite_revision, 'measurement_source_hashes', lambda stage: {'x': 'y'}),                 mock.patch.object(suite_game, '_report', lambda record: record),                 mock.patch.object(suite_game, '_reliable', lambda report, stage: True):
            result = closure._native_measurement_quality_v4(base.Inputs(), directory, suite, {'job_id': 'e'}, {'job_id': 'f'})
        return result, calls

    def test_v4_binds_session_records_and_still_stamps_suite_records(self):
        with tempfile.TemporaryDirectory() as root:
            result, calls = self.check(measurement_session(root))
            self.assertTrue(result)
            self.assertEqual(['candidate', 'final'], calls['binding'])
            self.assertEqual(['reference', 'original', 'before'], calls['stamp'])

    def test_unstamped_suite_record_or_unqualified_session_record_fails(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.check(measurement_session(root, unstamped=('reference',)))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(VisualError):
                self.check(measurement_session(root, statuses={'final': 'tracking_failure'}))

    def test_other_profiles_keep_the_v2_check(self):
        with tempfile.TemporaryDirectory() as root:
            directory = measurement_session(root)
            with mock.patch.object(closure, '_V2_MEASUREMENT', lambda *args: 'v2') as _:
                suite = {'acceptance_profile': GROUND_FLOOR_PROFILE, 'cases': []}
                self.assertEqual('v2', closure._native_measurement_quality_v4(base.Inputs(), directory, suite, {}, {}))


if __name__ == '__main__':
    unittest.main()
