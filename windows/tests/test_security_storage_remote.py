"""Offline regression fixtures for remote requests and persisted cleanup paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication
from requests.structures import CaseInsensitiveDict

from artist_cards import ArtistMetadataWorker, AvatarFetchWorker, cleanup_cache
from artist_lookup import lookup_profiles
from background_store import BackgroundStore, BackgroundError
from download_journal import inside, valid_manifest_component
from download_settings import fetch_artist_name, sanitize_artist_name
from recycle_bin import RecycleBin
from static_download_plan import ManifestFetchWorker, extract_assets, clean_component


PROFILE = 'https://pawchive.pw/fanbox/user/offline-security'
POST = PROFILE + '/post/one'


class Response:
    def __init__(self, body=b'', *, status=200, headers=None, during_read=None):
        self.body = body
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.encoding = 'utf-8'
        self.closed = False
        self.during_read = during_read

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError('offline HTTP error')

    def iter_content(self, size):
        yield self.body[:7]
        if self.during_read:
            self.during_read()
        yield self.body[7:]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError('An unapproved redirect was requested.')
        return self.responses.pop(0)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RemoteSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_security_remote_')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def redirect(self):
        return Response(status=302, headers={'Location': 'https://outside.invalid/private'})

    def test_avatar_metadata_and_manifest_never_request_external_redirect(self):
        workers = (AvatarFetchWorker(self.root, PROFILE), ArtistMetadataWorker(PROFILE),
                   ManifestFetchWorker([dict(url=POST)]))
        for worker in workers:
            response = self.redirect()
            session = Session(response)
            results = []
            signal = worker.failed if isinstance(worker, ManifestFetchWorker) else worker.result
            signal.connect(lambda *args: results.append(args))
            with patch('requests.Session', return_value=session):
                worker.run()
            self.assertEqual(len(session.calls), 1)
            self.assertFalse(session.calls[0][1]['allow_redirects'])
            self.assertTrue(response.closed)
            self.assertTrue(session.closed)
            self.assertTrue(results)
            if not isinstance(worker, ManifestFetchWorker):
                self.assertTrue(results[0][-1])

    def test_lookup_checks_profile_and_avatar_redirects_before_request(self):
        session = Session(self.redirect())
        self.assertFalse(lookup_profiles((PROFILE,), session=session))
        self.assertEqual(len(session.calls), 1)
        header = b'<div class="user-header"><b itemprop="name">Artist</b></div>'
        session = Session(Response(header), self.redirect())
        records = lookup_profiles((PROFILE,), session=session)
        self.assertEqual(records[0]['name'], 'Artist')
        self.assertFalse(records[0]['avatar'])
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(not kwargs['allow_redirects'] for _, kwargs in session.calls))

    def test_artist_folder_name_fetch_rejects_redirect_and_oversized_html(self):
        for response in (self.redirect(), Response(headers={'Content-Length': str(3 * 1024 * 1024)}),
                         Response(b'x' * (2 * 1024 * 1024 + 1))):
            session = Session(response)
            with patch('requests.Session', return_value=session), self.assertRaises(ValueError):
                fetch_artist_name(PROFILE)
            self.assertTrue(response.closed)
            self.assertTrue(session.closed)

    def test_metadata_and_manifest_reject_oversized_html_before_parsing(self):
        for worker in (ArtistMetadataWorker(PROFILE), ManifestFetchWorker([dict(url=POST)])):
            response = Response(headers={'Content-Length': str(9 * 1024 * 1024)})
            session = Session(response)
            errors = []
            signal = worker.failed if isinstance(worker, ManifestFetchWorker) else worker.result
            signal.connect(lambda *args: errors.append(args[-1]))
            with patch('requests.Session', return_value=session):
                worker.run()
            self.assertIn('response_too_large', errors[0])
            self.assertTrue(response.closed)

    def test_manifest_rejects_external_initial_url_without_request(self):
        session = Session()
        worker = ManifestFetchWorker([dict(url='https://outside.invalid/post/one')])
        errors = []
        worker.failed.connect(errors.append)
        with patch('requests.Session', return_value=session):
            worker.run()
        self.assertTrue(errors)
        self.assertFalse(session.calls)

    def test_malformed_remote_asset_url_is_ignored_without_aborting_the_manifest(self):
        html = '<div class="post__files"><a class="fileThumb" href="https://file.pawchive.pw:invalid/data/bad.jpg">bad</a>'
        html += '<a class="fileThumb" href="https://file.pawchive.pw/data/good.jpg">good.jpg</a></div>'
        assets = extract_assets(html, POST)
        self.assertEqual([item['url'] for item in assets], ['https://file.pawchive.pw/data/good.jpg'])

    def test_overlapping_avatar_downloads_use_independent_temporary_files(self):
        image = QImage(48, 32, QImage.Format.Format_RGB32)
        image.fill(QColor('#33cc77'))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, 'JPG'))
        body = bytes(buffer.data())
        workers = [AvatarFetchWorker(self.root, PROFILE) for _ in range(2)]
        results = []
        for worker in workers:
            worker.result.connect(lambda *args: results.append(args))
        first = Response(body, during_read=workers[1].run)
        second = Response(body)
        with patch('requests.Session', side_effect=[Session(first), Session(second)]):
            workers[0].run()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not result[-1] for result in results), results)
        self.assertEqual(Path(results[0][1]).read_bytes(), body)
        self.assertFalse(list((self.root / 'artist-cards').glob('*.tmp')))


class StorageContainmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_security_storage_')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def junction(self, link, target):
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest('Directory junction creation is unavailable.')
        else:
            link.symlink_to(target, target_is_directory=True)
        self.addCleanup(link.rmdir if os.name == 'nt' else link.unlink)

    def test_background_manifest_cannot_load_or_remove_unrelated_local_state(self):
        victim = self.root / 'state.sqlite3'
        victim.write_bytes(b'private local settings')
        config = dict(mode='default', pages={'workspace': {'preview': 'state.sqlite3'}})
        (self.root / 'backgrounds.json').write_text(json.dumps(config), encoding='utf-8')
        store = BackgroundStore(self.root)
        self.assertIsNone(store.page_config('workspace'))
        store.remove_page('workspace')
        store.cleanup_unused(remove_disabled=True)
        self.assertEqual(victim.read_bytes(), b'private local settings')

    def test_background_root_junction_cannot_redirect_saves_or_cleanup(self):
        outside = self.root / 'outside'
        outside.mkdir()
        victim = outside / 'private.jpg'
        victim.write_bytes(b'private source')
        state = self.root / 'state'
        state.mkdir()
        self.junction(state / 'backgrounds', outside)
        with self.assertRaises(BackgroundError):
            BackgroundStore(state)
        self.assertEqual(victim.read_bytes(), b'private source')

    def test_avatar_cleanup_never_sweeps_a_linked_cache_root(self):
        outside = self.root / 'outside'
        outside.mkdir()
        victim = outside / 'private.jpg'
        victim.write_bytes(b'private source')
        state = self.root / 'state'
        state.mkdir()
        self.junction(state / 'artist-cards', outside)
        store = type('Store', (), {'data_dir': state})()
        self.assertEqual(cleanup_cache(store), 0)
        self.assertEqual(victim.read_bytes(), b'private source')

    def test_expired_database_identifier_cannot_escape_recycle_root(self):
        state = self.root / 'state'
        recycle = RecycleBin(state, clock=lambda: 100)
        self.addCleanup(recycle.close)
        victim = self.root / 'victim'
        victim.mkdir()
        (victim / 'private.txt').write_bytes(b'keep me')
        entry = recycle.add(title='offline fixture')
        recycle._db.execute('UPDATE entries SET id=?,expires_at=0 WHERE id=?', ('../../victim', entry.id))
        recycle._db.commit()
        self.assertEqual(recycle.cleanup(), 0)
        self.assertEqual(recycle.purge(), 0)
        self.assertEqual((victim / 'private.txt').read_bytes(), b'keep me')

    def test_restore_cannot_move_a_payload_outside_its_recycle_entry(self):
        recycle = RecycleBin(self.root / 'state', clock=lambda: 100)
        self.addCleanup(recycle.close)
        entry = recycle.add(title='offline fixture')
        victim = self.root / 'private.txt'
        victim.write_bytes(b'private local file')
        recycle._db.execute('UPDATE entries SET payload_path=? WHERE id=?', (str(victim), entry.id))
        recycle._db.commit()
        destination = self.root / 'unexpected.txt'
        with self.assertRaises(ValueError):
            recycle.restore(entry.id, destination)
        self.assertEqual(victim.read_bytes(), b'private local file')
        self.assertFalse(destination.exists())

    def test_expired_recycle_entry_junction_cannot_redirect_cleanup(self):
        recycle = RecycleBin(self.root / 'state', clock=lambda: 100)
        self.addCleanup(recycle.close)
        entry = recycle.add(title='offline fixture')
        entry_dir = recycle.root / entry.id
        entry_dir.rmdir()
        victim = self.root / 'outside'
        victim.mkdir()
        (victim / 'private.txt').write_bytes(b'keep this directory')
        self.junction(entry_dir, victim)
        recycle._db.execute('UPDATE entries SET expires_at=0 WHERE id=?', (entry.id,))
        recycle._db.commit()
        self.assertEqual(recycle.cleanup(), 0)
        self.assertIsNone(recycle.remove(entry.id))
        self.assertEqual((victim / 'private.txt').read_bytes(), b'keep this directory')

    def test_failed_recycle_insert_and_failed_rollback_preserve_moved_payload(self):
        recycle = RecycleBin(self.root / 'state', clock=lambda: 100)
        self.addCleanup(recycle.close)
        source = self.root / 'source.txt'
        source.write_bytes(b'user payload')
        original_move = recycle._move_payload

        def move_then_block_rollback(*args):
            result = original_move(*args)
            source.write_bytes(b'another file now occupies the original path')
            recycle._db.execute('DROP TABLE entries')
            recycle._db.commit()
            return result

        with patch.object(recycle, '_move_payload', side_effect=move_then_block_rollback):
            with self.assertRaises(Exception):
                recycle.add(title='offline fixture', payload_path=source)
        payloads = list(recycle.root.glob('*/payload/source.txt'))
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].read_bytes(), b'user payload')
        self.assertEqual(source.read_bytes(), b'another file now occupies the original path')

    def test_checkpoint_paths_reject_ntfs_streams_and_windows_devices(self):
        for relative in ('image.jpg:private', 'NUL.jpg', 'COM1.txt', 'LPT¹.png',
                         'folder./image.jpg', 'folder /image.jpg', 'folder/../image.jpg'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                inside(self.root, self.root / relative)
        self.assertEqual(inside(self.root, self.root / '画师' / 'image.jpg'),
                         self.root / '画师' / 'image.jpg')

    def test_remote_names_are_sanitized_consistently_with_checkpoint_validation(self):
        for name in ('CON .jpg', 'NUL .png', 'COM¹.jpg', 'LPT³ .jpg'):
            for sanitize in (clean_component, sanitize_artist_name):
                with self.subTest(name=name, sanitize=sanitize):
                    self.assertTrue(valid_manifest_component(sanitize(name)))


if __name__ == '__main__':
    unittest.main()
