import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from kiroclops.integration import import_cache_v1 as cache
from kiroclops.integration.storage import project_hashes


class ImportCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'imported'
        self.source.mkdir()
        self.engine = self.root / 'godot.exe'
        self.engine.write_bytes(b'controlled engine bytes')
        self._write('project.godot', b'[application]\nconfig/name="Test"\n')
        self._write('source/characters/player/player.gd', b'extends Node2D\n')
        self._write('scene.tscn', b'[gd_scene format=3]\n')
        self._write('assets/hero.png', b'uncompressed source fixture')
        self.product = '.godot/imported/hero.png-deadbeef.ctex'
        self._write(self.product, b'compiled texture fixture')
        self._write('.godot/editor/filesystem_cache10', b'must not copy editor state')
        self._write('.godot/global_script_class_cache.cfg', b'must not copy script state')
        self._write('.godot/uid_cache.bin', b'must not copy uid state')
        self._write('assets/hero.png.import', (
            '[remap]\nimporter="texture"\ntype="CompressedTexture2D"\n'
            f'path="res://{self.product}"\n[deps]\nsource_file="res://assets/hero.png"\n'
            f'dest_files=["res://{self.product}"]\n[params]\ncompress/mode=0\n').encode())
        self.md5 = '.godot/imported/hero.png-deadbeef.md5'
        self._write(self.md5, (
            f'source_md5="{hashlib.md5((self.source/"assets/hero.png").read_bytes()).hexdigest()}"\n'
            f'dest_md5="{hashlib.md5((self.source/self.product).read_bytes()).hexdigest()}"\n').encode())
        self.expected = project_hashes(self.source)

    def _write(self, relative, data):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def publish(self):
        return cache.publish_import_cache(self.source, self.root/'cache', engine=self.engine,
                                          expected_source_hashes=self.expected)

    def fresh(self):
        target = self.root/'fresh'
        shutil.copytree(self.source, target, ignore=shutil.ignore_patterns('.godot'))
        return target

    def seed(self, receipt, target, expected=None):
        return cache.seed_import_cache(receipt['cache_dir'], target, engine=self.engine,
                                       expected_source_hashes=expected or self.expected,
                                       expected_manifest_sha256=receipt['manifest_sha256'])

    def test_seed_preserves_sources_and_copies_only_asset_products(self):
        receipt = self.publish(); target = self.fresh()
        result = self.seed(receipt, target)
        self.assertEqual(result['status'], 'seeded')
        self.assertTrue(result['requires_editor_import'])
        self.assertFalse(result['capture_acceptance'])
        self.assertEqual(project_hashes(target), self.expected)
        names={p.relative_to(target).as_posix() for p in (target/'.godot').rglob('*') if p.is_file()}
        self.assertEqual(names, {self.product, self.md5})
        (target/self.product).write_bytes(b'editor can change disposable cache')
        original=Path(receipt['cache_dir'])/'payload'/self.product
        self.assertEqual(original.read_bytes(), b'compiled texture fixture')

    def test_script_change_and_new_controller_helper_do_not_reuse_script_state(self):
        receipt=self.publish();target=self.fresh()
        (target/'source/characters/player/player.gd').write_bytes(b'extends Node2D\n# new candidate\n')
        (target/'source/characters/player/helper.gd').write_bytes(b'class_name NewHelper\n')
        expected=project_hashes(target)
        self.seed(receipt,target,expected)
        self.assertEqual(project_hashes(target),expected)
        self.assertFalse((target/'.godot/global_script_class_cache.cfg').exists())

    def test_asset_change_rejected_even_with_updated_candidate_fingerprint(self):
        receipt=self.publish();target=self.fresh()
        (target/'assets/hero.png').write_bytes(b'changed')
        with self.assertRaisesRegex(cache.ImportCacheError,'changed'):
            self.seed(receipt,target,project_hashes(target))
        self.assertFalse((target/'.godot').exists())

    def test_import_setting_change_rejected_despite_legacy_source_hash_policy(self):
        receipt=self.publish();target=self.fresh()
        p=target/'assets/hero.png.import';p.write_text(p.read_text().replace('compress/mode=0','compress/mode=1'))
        self.assertEqual(project_hashes(target), self.expected)
        with self.assertRaisesRegex(cache.ImportCacheError,'changed'):
            self.seed(receipt,target)

    def test_scene_or_project_change_rejects_reuse(self):
        receipt=self.publish();target=self.fresh()
        (target/'scene.tscn').write_text('changed scene')
        with self.assertRaisesRegex(cache.ImportCacheError,'changed'):
            self.seed(receipt,target,project_hashes(target))

    def test_engine_change_rejects_reuse(self):
        receipt=self.publish();target=self.fresh();self.engine.write_bytes(b'different engine')
        with self.assertRaisesRegex(cache.ImportCacheError,'changed'):
            self.seed(receipt,target)

    def test_existing_godot_state_is_not_overwritten(self):
        receipt=self.publish();target=self.fresh();(target/'.godot').mkdir()
        with self.assertRaisesRegex(cache.ImportCacheError,'fresh project'):
            self.seed(receipt,target)

    def test_stale_source_md5_rejected(self):
        (self.source/'assets/hero.png').write_bytes(b'modified after import')
        self.expected=project_hashes(self.source)
        with self.assertRaisesRegex(cache.ImportCacheError,'stale'):
            self.publish()

    def test_corrupt_compiled_texture_rejected(self):
        (self.source/self.product).write_bytes(b'corrupted')
        with self.assertRaisesRegex(cache.ImportCacheError,'stale'):
            self.publish()

    def test_payload_tamper_rejected(self):
        receipt=self.publish();target=self.fresh()
        (Path(receipt['cache_dir'])/'payload'/self.product).write_bytes(b'tamper')
        with self.assertRaisesRegex(cache.ImportCacheError,'checksum'):
            self.seed(receipt,target)

    def test_caller_binding_rejects_manifest_rewrite(self):
        receipt=self.publish();target=self.fresh()
        p=Path(receipt['cache_dir'])/'manifest.json';m=json.loads(p.read_text());m['created_at']='rewritten'
        p.write_text(json.dumps(m))
        with self.assertRaisesRegex(cache.ImportCacheError,'caller-held'):
            self.seed(receipt,target)

    def test_unregistered_payload_cannot_smuggle_script_state(self):
        receipt=self.publish();target=self.fresh()
        (Path(receipt['cache_dir'])/'payload/.godot/controller.gd').write_text('forbidden')
        with self.assertRaisesRegex(cache.ImportCacheError,'Unregistered'):
            self.seed(receipt,target)

    def test_destination_traversal_is_rejected_before_copy(self):
        p=self.source/'assets/hero.png.import';p.write_text(p.read_text().replace(self.product,'.godot/imported/../controller.ctex'))
        with self.assertRaises(cache.ImportCacheError):self.publish()

    def test_existing_entry_is_verified_and_not_rewritten(self):
        one=self.publish();manifest=Path(one['cache_dir'])/'manifest.json';before=manifest.read_bytes()
        two=self.publish()
        self.assertEqual(one['cache_key'],two['cache_key'])
        self.assertEqual(manifest.read_bytes(),before)

    def test_cache_overlap_and_cancellation(self):
        with self.assertRaisesRegex(cache.ImportCacheError,'separate'):
            cache.publish_import_cache(self.source,self.source/'cache',engine=self.engine,expected_source_hashes=self.expected)
        event=threading.Event();event.set()
        with self.assertRaisesRegex(cache.ImportCacheError,'cancelled'):
            cache.publish_import_cache(self.source,self.root/'cache',engine=self.engine,expected_source_hashes=self.expected,cancel=event)

    def test_hard_link_is_rejected(self):
        source=self.source/self.product;copy=source.with_name('original.ctex');source.rename(copy);os.link(copy,source)
        with self.assertRaisesRegex(cache.ImportCacheError,'hard-linked'):
            self.publish()

    def test_input_change_while_publishing_is_detected(self):
        original=cache.shutil.copy2;changed=False
        def copy(source,destination):
            nonlocal changed
            answer=original(source,destination)
            if not changed:
                changed=True
                (self.source/'source/characters/player/player.gd').write_text('racing candidate edit')
            return answer
        with patch.object(cache.shutil,'copy2',side_effect=copy):
            with self.assertRaisesRegex(cache.ImportCacheError,'source fingerprint'):
                self.publish()

    def test_read_only_inspection_creates_no_cache(self):
        result=cache.inspect_import_inputs(self.source,engine=self.engine,expected_source_hashes=self.expected)
        self.assertEqual(result['asset_count'],1)
        self.assertEqual(result['product_count'],2)
        self.assertFalse((self.root/'cache').exists())


if __name__=='__main__':
    unittest.main()
