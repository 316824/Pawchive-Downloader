import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from background_store import BackgroundStore
from download_pawchive_images import cleanup_stale_cache_dirs


class CacheCleanupTests(unittest.TestCase):
    def test_stale_cache_cleanup_keeps_recent_and_active_directories(self):
        with tempfile.TemporaryDirectory(prefix='pawchive_cache_test_') as folder:
            root = Path(folder)
            old = root / 'pawchive_cache_old'
            recent = root / 'pawchive_cache_recent'
            active = root / 'pawchive_cache_active'
            for item in (old, recent, active):
                item.mkdir()
                (item / 'cover.tmp').write_bytes(b'x')
            now = 10_000.0
            old_time = now - 3 * 24 * 60 * 60
            os.utime(old, (old_time, old_time))
            with patch('download_pawchive_images.tempfile.gettempdir', return_value=folder):
                removed = cleanup_stale_cache_dirs(current=active, now=now)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(active.exists())

    def test_background_cleanup_removes_orphans_and_atomic_temps_only(self):
        with tempfile.TemporaryDirectory(prefix='pawchive_background_test_') as folder:
            root = Path(folder)
            source = root / 'source.jpg'
            source.write_bytes(b'source')
            store = BackgroundStore(root)
            store.save_page('workspace', source=source, crop={}, preview_bytes=b'preview',
                            source_size=(10, 10), preview_size=(10, 10), aspect=1.0)
            orphan = store.root / 'orphan.jpg'
            orphan.write_bytes(b'orphan')
            stale_preview = store.root / '.workspace-stale.tmp'
            stale_preview.write_bytes(b'tmp')
            stale_json = root / '.backgrounds-stale.tmp'
            stale_json.write_bytes(b'tmp')
            removed = store.cleanup_unused()
            self.assertEqual(removed, 3)
            self.assertTrue(store.preview_path('workspace').is_file())
            self.assertFalse(orphan.exists())
            self.assertFalse(stale_preview.exists())
            self.assertFalse(stale_json.exists())
            store.set_mode('reward')
            removed_disabled = store.cleanup_unused(remove_disabled=True)
            self.assertGreaterEqual(removed_disabled, 1)
            self.assertFalse(store.has_custom())


if __name__ == '__main__':
    unittest.main()
