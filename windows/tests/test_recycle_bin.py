import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recycle_bin import RecycleBin


class RecycleBinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_recycle_test_')
        self.now = [1_700_000_000.0]
        self.bin = RecycleBin(Path(self.temp.name) / 'state',
                              clock=lambda: self.now[0], retention_seconds=100)

    def tearDown(self):
        self.bin.close()
        self.temp.cleanup()

    def test_metadata_and_cover_are_persisted(self):
        cover = Path(self.temp.name) / 'cover.png'
        cover.write_bytes(b'\x89PNG\r\n')
        entry = self.bin.add(
            title='Night study', profile_url='https://pawchive.pw/fanbox/user/a',
            post_url='https://pawchive.pw/fanbox/user/a/post/1',
            cover_path=cover, metadata={'ordinal': 3})
        self.assertEqual(self.bin.list_entries()[0], entry)
        self.assertTrue(Path(entry.cover_path).is_file())
        self.assertEqual(entry.metadata, {'ordinal': 3})
        self.assertEqual(self.bin.get(entry.id).download_url, entry.post_url)

    def test_payload_moves_and_restore_never_overwrites_existing_files(self):
        payload = Path(self.temp.name) / 'album'
        payload.mkdir()
        (payload / 'image.jpg').write_bytes(b'partial')
        entry = self.bin.add(title='Album', payload_path=payload)
        self.assertFalse(payload.exists())
        self.assertTrue(Path(entry.payload_path).is_dir())
        target = Path(self.temp.name) / 'restored'
        target.mkdir()
        restored = self.bin.restore(entry.id, target / 'album')
        self.assertEqual(restored.id, entry.id)
        self.assertEqual((target / 'album' / 'image.jpg').read_bytes(), b'partial')
        self.assertEqual(self.bin.list_entries(), [])

    def test_clock_floor_prevents_early_expiry_after_rollback(self):
        entry = self.bin.add(title='Keep me')
        self.now[0] -= 10_000
        self.assertEqual(self.bin.cleanup(), 0)
        self.now[0] = 1_700_000_100.0
        self.assertEqual(self.bin.cleanup(), 1)
        self.assertIsNone(self.bin.get(entry.id))

    def test_expiry_is_checked_after_reopening_the_store(self):
        entry = self.bin.add(title='Old')
        self.bin.close()
        self.now[0] += 101
        reopened = RecycleBin(Path(self.temp.name) / 'state', clock=lambda: self.now[0], retention_seconds=100)
        self.assertIsNone(reopened.get(entry.id))
        reopened.close()


if __name__ == '__main__':
    unittest.main()
