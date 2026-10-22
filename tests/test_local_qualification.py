"""Engineering qualification cannot turn status-only evidence into acceptance."""
from __future__ import annotations
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from kiroclops.local_engine.qualification import controls,_control_source,_classify,write_manifest,bind_evidence,combine_manifests
from kiroclops.visual.schema import VisualError,file_sha256,write_json
from kiroclops.web_local.specs import editable_files,fixture_project


class QualificationTests(unittest.TestCase):
    def test_evidence_binds_manifest_and_actual_analysis_without_loading_corrupt_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);package=root/'package';reference=root/'reference'
            package.mkdir();reference.mkdir()
            files=[package/'manifest.json',reference/'manifest.json',root/'selection.json',
                   root/'contract.json',root/'analysis.json',root/'observations.json',root/'result.json']
            for path in files:path.write_text('{}',encoding='utf-8')
            row={'package_dir':str(package),'reference_package_dir':str(reference),
                 **{name+'_path':str(root/(name+'.json')) for name in ('selection','contract','analysis','result')}}
            bound=bind_evidence(row)
            self.assertEqual(set(bound['evidence_hashes']),{str(path.resolve()) for path in files})
            self.assertEqual(bound['evidence_hashes'][str(root/'observations.json')],file_sha256(root/'observations.json'))
            self.assertNotIn('evidence_hashes',row)
            (root/'result.json').unlink()
            with self.assertRaises(VisualError):bind_evidence(row)

    def test_registered_controls_have_two_good_and_four_bad_per_domain(self):
        for domain in ('panel','svg'):
            cases=controls(domain)
            self.assertEqual(len(cases),6);self.assertEqual(len({c['id'] for c in cases}),6)
            self.assertEqual(sum(c['expected_class']=='correct' for c in cases),2)

    def test_control_mutations_are_real_copied_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            for domain in ('panel','svg'):
                for case in controls(domain):
                    root=_control_source(domain,case['mutation'],Path(tmp)/case['id'])
                    name=editable_files(domain)[0]
                    if case['mutation'] not in ('none','detached_edge'):
                        self.assertNotEqual((root/name).read_text(),(fixture_project(domain,'reference')/name).read_text())
                    self.assertTrue((root/'index.html').exists())
                self.assertNotIn('farthest=',(fixture_project(domain,'reference')/editable_files(domain)[0]).read_text())

    def test_timing_and_functional_failure_do_not_become_motion_failure(self):
        capture={'timing':{'status':'fail'},'functional':{'status':'fail'}}
        self.assertEqual(_classify(capture,None,None),'capture_timing_failure')
        capture['timing']['status']='pass'
        self.assertEqual(_classify(capture,None,None),'application_integrity_failure')
        capture['functional']['status']='pass'
        self.assertEqual(_classify(capture,None,None),'tracking_failure')
        self.assertEqual(_classify(capture,{'measurement_status':'ok'},{'pass':False}),'behavioral_deviation')

    def test_classification_alone_never_grants_independent_acceptance(self):
        rows=[{'id':str(i),'classification_pass':True,'independent_review_status':'pending'} for i in range(24)]
        with tempfile.TemporaryDirectory() as tmp:
            result=write_manifest(Path(tmp)/'manifest.json',rows,source_hashes={})
            self.assertEqual(result['classification_status'],'pass');self.assertEqual(result['status'],'pending_review')
            rows[0]['classification_pass']=False
            self.assertEqual(write_manifest(Path(tmp)/'fail.json',rows,source_hashes={})['status'],'fail')

    def test_incomplete_duplicate_or_source_drift_rejects(self):
        rows=[{'id':str(i),'classification_pass':True,'independent_review_status':'pass'} for i in range(24)]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertEqual(write_manifest(root/'complete.json',rows,source_hashes={})['status'],'pass')
            self.assertEqual(write_manifest(root/'short.json',rows[:-1],source_hashes={})['status'],'fail')
            self.assertEqual(write_manifest(root/'drift.json',rows,source_hashes={},extra={'source_binding_status':'fail'})['status'],'fail')
            rows[-1]['id']=rows[0]['id']
            self.assertEqual(write_manifest(root/'duplicate.json',rows,source_hashes={})['status'],'fail')

    def test_combination_keeps_additional_domain_source_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'core.py').write_text('core');(root/'game_math.py').write_text('frozen domain math')
            sources={p.name:file_sha256(p) for p in root.glob('*.py')}
            rows=[{'id':str(i),'classification_pass':True,'independent_review_status':'pass'} for i in range(24)]
            component=root/'component.json';write_json(component,{'classification_status':'pass','samples':rows,'source_hashes':sources})
            with patch('kiroclops.local_engine.qualification.ROOT',root),patch('kiroclops.local_engine.qualification.analyzer_sources',return_value={'core.py':sources['core.py']}):
                combined=combine_manifests([component],root/'combined.json')
                self.assertEqual('pass',combined['status']);self.assertEqual(sources,combined['source_hashes'])
                (root/'game_math.py').write_text('changed after measurement')
                rejected=combine_manifests([component],root/'changed.json')
                self.assertEqual('fail',rejected['source_binding_status']);self.assertEqual('fail',rejected['status'])


if __name__=='__main__':unittest.main()
