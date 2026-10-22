"""Byte-complete source inventory and one-walk path safety regressions."""
from pathlib import Path
import os
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiroclops.local_engine import source_inventory as inventory
from kiroclops.local_engine.session import source_hashes
from kiroclops.visual.schema import VisualError, file_sha256


class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'source'
        self.root.mkdir()

    def write(self, relative, content=b'ordinary'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def assert_code(self, code, callback):
        with self.assertRaises(VisualError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def test_exact_default_keys_and_byte_hashes_match_session_inventory(self):
        for name in ('z.txt', 'nested/player.gd', 'nested/player.gd.uid', '.runtime/receipt.json',
                     'nested/BYTE.PYC', 'nested/.gitkeep', 'frame.png'):
            self.write(name, bytes(range(256)) * 5)
        for name in ('cached.pyc', 'nested/cached.pyo', '.git/HEAD', '.godot/cache',
                     '.import/cache', '__pycache__/one.pyc', '.kiro/prompts',
                     '.kiroclops-evidence/raw.png', '.kiroclops-scratch/tmp'):
            self.write(name)
        self.assertEqual(source_hashes(self.root), inventory.full_byte_inventory(self.root))
        self.assertIn('nested/player.gd.uid', inventory.full_byte_inventory(self.root))

    def test_explicit_complete_inventory_includes_every_regular_file(self):
        paths = [self.write(name, name.encode()) for name in
                 ('cached.pyc', '.godot/cache', '.git/HEAD', 'ordinary.txt')]
        expected = {p.relative_to(self.root).as_posix(): file_sha256(p) for p in paths}
        self.assertEqual(expected, inventory.full_byte_inventory(self.root, ignored=(), excluded_suffixes=()))

    def test_equal_size_and_timestamp_never_reuse_a_previous_hash(self):
        path = self.write('raw.png', b'first')
        original = path.stat()
        before = inventory.full_byte_inventory(self.root)
        path.write_bytes(b'other')
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(original.st_size, path.stat().st_size)
        self.assertEqual(original.st_mtime_ns, path.stat().st_mtime_ns)
        after = inventory.full_byte_inventory(self.root)
        self.assertNotEqual(before, after)
        self.assertEqual(file_sha256(path), after['raw.png'])

    def test_large_file_is_fully_hashed_across_multiple_chunks(self):
        path = self.write('large.bin', b'A' * (inventory._CHUNK_BYTES + 17) + b'last bytes')
        self.assertEqual(file_sha256(path), inventory.full_byte_inventory(self.root)['large.bin'])

    def test_missing_root_or_file_as_root_rejects(self):
        self.assert_code('invalid_project', lambda: inventory.full_byte_inventory(self.root / 'missing'))
        path = self.write('file')
        self.assert_code('invalid_project', lambda: inventory.full_byte_inventory(path))

    def test_each_directory_is_enumerated_once_and_ignored_contents_are_pruned(self):
        for name in ('a/one.png', 'a/b/two.png', '.godot/unclaimed/cache.png'):
            self.write(name)
        scandir = os.scandir
        visited = []
        def logged(path):
            visited.append(Path(path))
            return scandir(path)
        with patch.object(inventory.os, 'scandir', side_effect=logged):
            result = inventory.full_byte_inventory(self.root)
        self.assertEqual({'a/one.png', 'a/b/two.png'}, set(result))
        self.assertEqual(3, len(visited))
        self.assertEqual(3, len(set(visited)))
        self.assertNotIn(self.root / '.godot', visited)

    def test_file_replacement_between_inventory_and_open_rejects(self):
        path = self.write('one.png', b'first')
        hash_regular = inventory._hash_regular
        def changed(file, expected):
            file.write_bytes(b'different length')
            return hash_regular(file, expected)
        with patch.object(inventory, '_hash_regular', side_effect=changed):
            self.assert_code('source_changed', lambda: inventory.full_byte_inventory(self.root))

    def test_changed_content_while_reading_rejects(self):
        self.write('one.png', b'first')
        fstat = inventory.os.fstat
        calls = 0
        def changed(descriptor):
            nonlocal calls
            value = fstat(descriptor)
            calls += 1
            if calls == 2:
                return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino,
                    st_size=value.st_size, st_mtime_ns=value.st_mtime_ns + 1,
                    st_ctime_ns=value.st_ctime_ns)
            return value
        with patch.object(inventory.os, 'fstat', side_effect=changed):
            self.assert_code('source_changed', lambda: inventory.full_byte_inventory(self.root))

    def test_reparse_attribute_rejects_even_without_symlink_mode(self):
        value = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assert_code('unsafe_path', lambda: inventory._reject_link(value))

    def test_root_ancestor_reparse_rejects(self):
        lstat = Path.lstat
        ancestor = self.root.parent
        def reparse(path, *args, **kwargs):
            value = lstat(path, *args, **kwargs)
            if path == ancestor:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        with patch.object(Path, 'lstat', reparse):
            self.assert_code('unsafe_path', lambda: inventory.full_byte_inventory(self.root))

    def reparse_lstat(self, target):
        lstat = Path.lstat
        def reparse(path, *args, **kwargs):
            value = lstat(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        return reparse

    def test_ignored_directory_reparse_attribute_is_checked_before_pruning(self):
        self.write('.godot/cache')
        with patch.object(Path, 'lstat', self.reparse_lstat(self.root / '.godot')):
            self.assert_code('unsafe_path', lambda: inventory.full_byte_inventory(self.root))

    def test_excluded_file_reparse_attribute_is_checked_before_skipping(self):
        path = self.write('cached.pyc')
        with patch.object(Path, 'lstat', self.reparse_lstat(path)):
            self.assert_code('unsafe_path', lambda: inventory.full_byte_inventory(self.root))

    def test_directory_replacement_after_walk_is_rechecked(self):
        self.write('nested/frame.png')
        lstat = Path.lstat
        calls = 0
        def replacement(path, *args, **kwargs):
            nonlocal calls
            value = lstat(path, *args, **kwargs)
            if path == self.root / 'nested':
                calls += 1
                if calls == 3:
                    return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev,
                                           st_ino=value.st_ino + 1)
            return value
        with patch.object(Path, 'lstat', replacement):
            self.assert_code('source_changed', lambda: inventory.full_byte_inventory(self.root))

    def symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest('Symlink creation unavailable: ' + str(error))

    def test_regular_file_symlink_rejects(self):
        target = self.write('target', b'data')
        self.symlink(self.root / 'link.png', target)
        self.assert_code('unsafe_path', lambda: inventory.full_byte_inventory(self.root))

    def test_ignored_directory_symlink_rejects_before_pruning(self):
        target = self.root.parent / 'outside'
        target.mkdir()
        self.symlink(self.root / '.godot', target, directory=True)
        self.assert_code('unsafe_path', lambda: inventory.full_byte_inventory(self.root))

    def test_excluded_suffix_symlink_rejects_before_skipping(self):
        target = self.write('target')
        self.symlink(self.root / 'cached.pyc', target)
        self.assert_code('unsafe_path', lambda: inventory.full_byte_inventory(self.root))

    def test_source_root_symlink_rejects(self):
        alias = self.root.parent / 'alias'
        self.symlink(alias, self.root, directory=True)
        self.assert_code('unsafe_path', lambda: inventory.full_byte_inventory(alias))


if __name__ == '__main__':
    unittest.main()
