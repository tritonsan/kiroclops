"""Worker code belongs to measurement evidence, independent of request/result SHA.

Only small temporary source/receipt files are changed. No worker, CV, browser,
game, model or actual production-source mutation is used by these tests.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.local_engine import qualification
from kiroclops.local_engine.reference_acceptance import validate_reference_gate
from kiroclops.visual.schema import VisualError, file_sha256, write_json


WORKER='kiroclops/local_engine/worker.py'
INITIALIZERS={'kiroclops/__init__.py','kiroclops/local_engine/__init__.py'}


class AnalysisWorkerSourceBindingTests(unittest.TestCase):
    def test_real_analyzer_source_inventory_binds_worker_and_package_initializers(self):
        actual=qualification.analyzer_sources()
        for name in {WORKER,*INITIALIZERS}:
            self.assertIn(name,actual)
            self.assertEqual(file_sha256(qualification.ROOT/name),actual[name])

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='worker source naïve ')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        # Mirror the current real inventory; newly required entries cannot be
        # silently omitted by an old test fixture's hardcoded source list.
        names=set(qualification.analyzer_sources())|{'kiroclops/local_engine/web_reference.py','kiroclops/local_engine/session.py'}
        for name in names:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('temporary fixture source: '+name+'\n',encoding='utf-8')

    def inventory(self):
        with patch.object(qualification,'ROOT',self.root):
            return qualification.analyzer_sources()

    def proof(self):
        manifest=self.root/'raw-reference-manifest.json';write_json(manifest,{'test':'source-gate-only'})
        source=self.inventory()
        for name in ('web_reference.py','session.py'):
            relative='kiroclops/local_engine/'+name;source[relative]=file_sha256(self.root/relative)
        gate=self.root/'gate.json'
        value={'kind':'ReferenceObservabilityGateV2','status':'pass','fatal_error':None,
            'count':32,'passed':32,'actual_comparator_self_consistency_passed':32,'required_count':32,
            'manifest_sha256':file_sha256(manifest),'source_hashes':source,
            'completed_source_hashes':deepcopy(source),'source_binding_status':'pass','records':[]}
        return manifest,gate,value

    def assert_source_gate_rejects(self,manifest,gate):
        with patch.object(qualification,'ROOT',self.root), \
             patch('kiroclops.local_engine.reference_acceptance.ROOT',self.root), \
             patch('kiroclops.local_engine.web_reference.load_reference_split',side_effect=AssertionError('Raw loading must not start')) as raw, \
             patch('kiroclops.local_engine.web_reference.check_reference_comparison_coverage',side_effect=AssertionError('Comparator must not run')) as comparison:
            with self.assertRaises(VisualError) as error:
                validate_reference_gate(manifest,gate)
        self.assertEqual('reference_not_qualified',error.exception.code)
        self.assertIn('different measurement version',str(error.exception))
        raw.assert_not_called();comparison.assert_not_called()

    def test_worker_only_change_alters_actual_freeze_inventory(self):
        original=self.inventory()
        (self.root/WORKER).write_text('changed worker result semantics\n',encoding='utf-8')
        changed=self.inventory()
        self.assertEqual({WORKER},{p for p in original if original[p]!=changed[p]})

    def test_missing_worker_file_cannot_produce_analyzer_source_inventory(self):
        (self.root/WORKER).unlink()
        with self.assertRaises(FileNotFoundError):self.inventory()

    def test_old_gate_omitting_worker_is_rejected_even_with_matching_summary_sources(self):
        manifest,gate,value=self.proof()
        value['source_hashes'].pop(WORKER);value['completed_source_hashes'].pop(WORKER)
        write_json(gate,value)
        self.assert_source_gate_rejects(manifest,gate)

    def test_worker_change_rejects_previously_bound_gate_before_any_raw_analysis(self):
        manifest,gate,value=self.proof();write_json(gate,value)
        (self.root/WORKER).write_text('changed worker after old gate\n',encoding='utf-8')
        self.assert_source_gate_rejects(manifest,gate)

    def test_each_initializer_change_also_alters_the_actual_freeze(self):
        for name in sorted(INITIALIZERS):
            with self.subTest(name=name):
                before=self.inventory()
                (self.root/name).write_text('changed import-time behavior\n',encoding='utf-8')
                after=self.inventory()
                self.assertEqual({name},{p for p in before if before[p]!=after[p]})


if __name__=='__main__':unittest.main()
