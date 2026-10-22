import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('materialize_local_kit',Path(__file__).resolve().parents[1]/'scripts/materialize_local_kit.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class KitTests(unittest.TestCase):
    def test_resolves_in_unicode_installation_without_mutating_template(self):
        with tempfile.TemporaryDirectory(prefix='café setup ') as directory:
            root=Path(directory);(root/'config').mkdir();template=root/'config/a.json.in'
            value={'template_run':'@PACKAGE_ROOT@/package-resources/templates/a','roi':[1,2,3,4]}
            template.write_text(json.dumps(value),encoding='utf-8');before=template.read_bytes()
            module.materialize(root);actual=json.loads((root/'config/a.json').read_text('utf-8'))
            self.assertEqual(str(root/'package-resources/templates/a'),actual['template_run'])
            self.assertEqual(before,template.read_bytes());module.materialize(root)

    def test_different_existing_annotation_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir()
            (root/'config/a.json.in').write_text('{"roi":[1,2,3,4]}')
            target=root/'config/a.json';target.write_text('{"roi":[9,9,9,9]}')
            with self.assertRaises(ValueError):module.materialize(root)
            self.assertEqual([9,9,9,9],json.loads(target.read_text())['roi'])

    def test_escaping_portable_path_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir()
            (root/'config/a.json.in').write_text('{"template_run":"@PACKAGE_ROOT@/../outside"}')
            with self.assertRaises(ValueError):module.materialize(root)


if __name__=='__main__':unittest.main()
