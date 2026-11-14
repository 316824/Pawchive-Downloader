"""Offline daily-cache, preview lifecycle and attachment safety regressions."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import (QBuffer, QByteArray, QCoreApplication, QEvent, QIODevice,
                           QObject, QPointF, QThread, Qt, Signal)
from PySide6.QtGui import QColor, QEnterEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
import requests

import popular_features as popular


def markup(start=1):
    return ''.join(f'<article class="post-card"><a href="/fanbox/user/123/post/{index}">'
        f'<h2>Album {index}</h2><img src="https://file.pawchive.pw/thumb/{index}.jpg"></a></article>'
        for index in range(start, start + 25))


class Response:
    def __init__(self, payload=b'', *, headers=None, status=200, chunks=None):
        self.headers = headers or {}
        self.status_code = status
        self.payload, self.chunks, self.closed = payload, chunks, False
        self.encoding = 'utf-8'

    def raise_for_status(self):
        if self.status_code >= 400: raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        yield from self.chunks if self.chunks is not None else [self.payload]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception): raise response
        return response


class ManualImage(QObject):
    result = Signal(str, object, str)
    finished = Signal()

    def __init__(self, url, parent=None, *, full=False):
        super().__init__(parent)
        self.url, self.full, self.running, self.cancelled = url, full, False, False

    def start(self): self.running = True
    def isRunning(self): return self.running
    def requestInterruption(self): self.cancelled = True
    def wait(self): self.finish()

    def finish(self, payload=None):
        if payload is not None: self.result.emit(self.url, payload, '')
        self.running = False
        self.finished.emit()


class ManualAssets(QObject):
    result = Signal(object, object, str)
    finished = Signal()

    def __init__(self, *args):
        super().__init__()
        self.running, self.cancelled = False, False

    def start(self): self.running = True
    def isRunning(self): return self.running
    def requestInterruption(self): self.cancelled = True
    def wait(self): self.finish()
    def finish(self):
        self.running = False
        self.finished.emit()

    def deliver(self, images, attachments):
        self.result.emit(images, attachments, '')
        self.finish()


class GatedAssets(QThread):
    result = Signal(object, object, str)

    def __init__(self, *args):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def run(self):
        self.entered.set()
        self.result.emit([], [], '')
        self.release.wait(3)
        self.result.emit([], [], '')


class PopularFeaturesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pawchive_popular_test_')
        self.root = Path(self.temporary.name)
        self.dialogs = []
        self.network = patch('requests.sessions.Session.request',
            side_effect=AssertionError('No network in recommendation tests'))
        self.network_mock = self.network.start()

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            for worker in list(dialog._workers):
                if isinstance(worker, GatedAssets): worker.release.set(); worker.wait(3000)
                elif hasattr(worker, 'running') and worker.running: worker.finish()
            self.app.processEvents()
            self.assertFalse(dialog._workers)
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.network_mock.assert_not_called()
        self.network.stop()
        self.temporary.cleanup()

    def wait_until(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            time.sleep(.001); QTest.qWait(5)
        self.assertTrue(condition())

    def preview(self):
        with patch.object(popular, 'PostAssetsWorker', ManualAssets):
            dialog = popular.PostPreviewDialog(None,
                {'url': 'https://pawchive.pw/fanbox/user/123/post/1', 'title': 'Album'})
        self.dialogs.append(dialog)
        return dialog

    @staticmethod
    def png_bytes():
        image = QImage(40, 30, QImage.Format.Format_ARGB32); image.fill(QColor('#8844cc'))
        data = QByteArray(); buffer = QBuffer(data); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'PNG')
        return bytes(data)

    def test_date_seed_rank_weight_and_no_duplicates(self):
        html = markup() + markup()
        first = popular.parse_popular_html(html, day='2026-11-14')
        self.assertEqual(10, len(first))
        self.assertEqual(first, popular.parse_popular_html(html, day='2026-11-14'))
        self.assertEqual(10, len({item['url'] for item in first}))
        ranks = [popular.parse_popular_html(markup(), day=f'2026-{month:02}-{day:02}', limit=1)[0]['rank']
                 for month in (1, 2, 3) for day in range(1, 21)]
        self.assertLess(sum(ranks) / len(ranks), 13)
        self.assertEqual('https://pawchive.pw/posts/popular?date=2026-11-14&period=day',
                         popular.popular_url('2026-11-14'))

    def test_daily_cache_survives_rank_changes_and_refreshes_next_day(self):
        session = Session(Response(markup().encode()))
        first = popular.PopularFetchWorker('2026-11-14', cache_dir=self.root)
        results = []; first.result.connect(results.append)
        with patch.object(popular.requests, 'Session', return_value=session): first.run()
        cached = popular.PopularFetchWorker('2026-11-14', cache_dir=self.root)
        cached.result.connect(results.append)
        with patch.object(popular.requests, 'Session', side_effect=AssertionError('same day must use disk cache')):
            cached.run()
        self.assertEqual(results[0], results[1])
        next_day = popular.PopularFetchWorker('2026-11-15', cache_dir=self.root)
        next_day.result.connect(results.append)
        with patch.object(popular.requests, 'Session', return_value=Session(Response(markup(100).encode()))):
            next_day.run()
        self.assertNotEqual(results[0], results[2])
        self.assertFalse(list(self.root.glob('.pawchive-popular-*')))

    def test_malformed_cache_is_ignored_and_foreign_cover_is_rejected(self):
        cache = popular.DailyRecommendationCache(self.root)
        cache.path.write_text('{invalid', encoding='utf-8')
        self.assertIsNone(cache.load('2026-11-14'))
        html = markup().replace('file.pawchive.pw', 'evilpawchive.pw')
        self.assertTrue(all(not item['image_url'] for item in popular.parse_popular_html(html)))
        for bad in ('https://evilpawchive.pw/a', 'https://pawchive.pw.evil.invalid/a',
                    'https://user:pass@pawchive.pw/a', 'https://pawchive.pw:444/a'):
            self.assertFalse(popular._safe_url(bad, subdomains=True))

    def test_opaque_image_links_and_declared_image_attachments_are_separate(self):
        html = '<div class="post__files"><a class="fileThumb" href="https://file.pawchive.pw/hash">'
        html += '<img src="/thumb.jpg"></a><a class="fileThumb" href="https://evilpawchive.pw/x.jpg">bad</a></div>'
        html += '<a class="post__attachment-link" href="https://file.pawchive.pw/file.jpg">image attachment</a>'
        html += '<a class="post__attachment-link" href="https://external.invalid/archive">external</a>'
        html += '<img src="https://evilpawchive.pw/img-original/x.jpg">'
        images, attachments = popular.extract_post_assets(html, popular.BASE_URL)
        self.assertEqual(1, len(images)); self.assertEqual('image_1.jpg', images[0]['name'])
        self.assertEqual(2, len(attachments))

    def test_untrusted_redirect_is_not_requested_and_streams_are_bounded(self):
        worker = popular.ImageBytesWorker('https://file.pawchive.pw/a.jpg')
        redirect = Response(status=302, headers={'location': 'https://evilpawchive.pw/a.jpg'})
        session = Session(redirect)
        with self.assertRaisesRegex(ValueError, 'unsafe_download_redirect'):
            popular._read_bytes(session, worker.url, worker, 3)
        self.assertEqual(1, len(session.calls)); self.assertTrue(redirect.closed)
        response = Response(chunks=[b'123', b'456'])
        with self.assertRaisesRegex(ValueError, 'response_too_large'):
            popular._read_bytes(Session(response), worker.url, worker, 3)
        self.assertTrue(response.closed)

    def test_attachment_download_preserves_existing_file_and_part(self):
        (self.root / 'archive.zip').write_bytes(b'existing')
        (self.root / 'archive.zip.part').write_bytes(b'user partial')
        worker = popular.AttachmentDownloadWorker([
            {'url': 'https://file.pawchive.pw/archive', 'name': 'archive.zip'}], self.root)
        result = []; worker.result.connect(lambda *args: result.append(args))
        with patch.object(popular.requests, 'Session', return_value=Session(Response(b'new archive'))): worker.run()
        self.assertEqual((1, 0, ''), result[0])
        self.assertEqual(b'existing', (self.root / 'archive.zip').read_bytes())
        self.assertEqual(b'user partial', (self.root / 'archive.zip.part').read_bytes())
        self.assertEqual(b'new archive', (self.root / 'archive_1.zip').read_bytes())
        self.assertFalse(list(self.root.glob('.pawchive-attachment-*')))

    def test_attachment_failure_removes_only_owned_temporary_and_external_is_never_fetched(self):
        (self.root / 'keep.zip.part').write_bytes(b'user data')
        worker = popular.AttachmentDownloadWorker([
            {'url': 'https://file.pawchive.pw/archive', 'name': '../keep.zip'},
            {'url': 'https://external.invalid/archive', 'name': 'external.zip'}], self.root)
        session = Session(Response(b'short', headers={'content-length': '99'}))
        result = []; worker.result.connect(lambda *args: result.append(args))
        with patch.object(popular.requests, 'Session', return_value=session): worker.run()
        self.assertEqual((0, 2), result[0][:2]); self.assertEqual(1, len(session.calls))
        self.assertEqual([self.root / 'keep.zip.part'], list(self.root.iterdir()))
        self.assertEqual('attachment_1.zip', popular._attachment_name('../../CON.zip', 1))

    def test_numbered_file_hosts_are_supported_but_foreign_redirects_are_not_downloaded(self):
        self.assertTrue(popular._file_host('n7.pawchive.pw'))
        self.assertFalse(popular._file_host('notfiles.pawchive.pw'))
        worker = popular.AttachmentDownloadWorker([
            {'url': 'https://n7.pawchive.pw/archive', 'name': 'archive.zip'}], self.root)
        session = Session(Response(status=302, headers={'location': 'https://static.pawchive.pw/not-a-file'}))
        result = []; worker.result.connect(lambda *args: result.append(args))
        with patch.object(popular.requests, 'Session', return_value=session): worker.run()
        self.assertEqual((0, 1), result[0][:2]); self.assertEqual(1, len(session.calls))
        self.assertFalse(list(self.root.iterdir()))

    def test_previews_are_viewport_lazy_and_full_images_are_requested_only_on_click(self):
        dialog = self.preview(); images = []
        for index in range(30):
            images.append(dict(url=f'https://file.pawchive.pw/full/{index}.png',
                               thumb_url=f'https://file.pawchive.pw/thumb/{index}.png', name=f'{index}.png'))
        created = []
        def factory(*args, **kwargs):
            worker = ManualImage(*args, **kwargs); created.append(worker); return worker
        with patch.object(popular, 'ImageBytesWorker', side_effect=factory), \
                patch.object(popular, 'AttachmentDownloadWorker') as attachments, \
                patch.object(dialog, '_show_full') as show:
            dialog.show(); self.app.processEvents()
            dialog._worker.deliver(images, [{'url': 'https://file.pawchive.pw/archive.zip', 'name': 'archive.zip'}])
            self.app.processEvents()
            self.assertLessEqual(len(created), 3); self.assertGreater(len(created), 0)
            self.assertTrue(all(not worker.full for worker in created))
            self.assertLess(len(dialog._thumb_requested), len(images)); attachments.assert_not_called()
            dialog.list.scrollToItem(dialog.list.item(29)); self.app.processEvents()
            self.assertIn(images[29]['thumb_url'], dialog._thumb_requested)
            self.assertLessEqual(len(dialog._image_active), 3)
            dialog._download_full(dialog.list.item(0))
            # A high-resolution request takes priority as soon as a slot frees.
            created[0].finish()
            self.app.processEvents()
            full = next(worker for worker in created if worker.full)
            self.assertEqual(images[0]['url'], full.url)
            full.finish(self.png_bytes()); self.app.processEvents()
            self.assertTrue(dialog._full_cache[full.url].is_file())
            before = len(created)
            dialog._download_full(dialog.list.item(0))
            self.assertEqual(before, len(created)); self.assertEqual(2, show.call_count)
            dialog.reject()
            self.assertFalse(dialog._temp.exists())

    def test_closing_preview_does_not_destroy_live_native_thread_and_ignores_late_results(self):
        with patch.object(popular, 'PostAssetsWorker', GatedAssets):
            dialog = popular.PostPreviewDialog(None, {'url': 'https://pawchive.pw/fanbox/user/123/post/1'})
        self.dialogs.append(dialog)
        worker = dialog._worker
        self.wait_until(worker.entered.is_set)
        (dialog._temp / 'opened-image').write_bytes(b'private preview')
        self.assertIn(dialog._temp, popular.active_preview_paths())
        closed = []; dialog.finished.connect(closed.append)
        dialog.reject()
        self.assertFalse(dialog._temp.exists()); self.assertNotIn(dialog._temp, popular.active_preview_paths())
        self.assertTrue(worker.isRunning()); self.assertEqual([], closed)
        worker.release.set()
        self.wait_until(lambda: not dialog._workers)
        self.assertEqual([0], closed); self.assertEqual(0, dialog.list.count())

    def test_download_callback_waits_for_native_preview_workers_before_closing_ancestors(self):
        with patch.object(popular, 'PostAssetsWorker', GatedAssets):
            dialog = popular.PostPreviewDialog(None, {'url': 'https://pawchive.pw/fanbox/user/123/post/1'})
        self.dialogs.append(dialog)
        dispatched = []; dialog.on_post = dispatched.append
        worker = dialog._worker
        self.wait_until(worker.entered.is_set)
        dialog._dispatch('post')
        self.assertEqual([], dispatched)
        self.assertTrue(worker.isRunning())
        worker.release.set(); self.wait_until(lambda: not dialog._workers)
        self.assertEqual([dialog.data], dispatched)

    def test_manual_preview_cleanup_skips_active_roots_and_unrelated_directories(self):
        active = self.root / 'pawchive_preview_active123'; active.mkdir()
        orphan = self.root / 'pawchive_preview_orphan123'; orphan.mkdir()
        unrelated = self.root / 'user-images'; unrelated.mkdir()
        with popular._PREVIEW_LOCK: popular._ACTIVE_PREVIEWS.add(active.resolve())
        try:
            self.assertEqual(1, popular.cleanup_inactive_previews(temp_root=self.root))
            self.assertTrue(active.exists()); self.assertTrue(unrelated.exists()); self.assertFalse(orphan.exists())
        finally:
            with popular._PREVIEW_LOCK: popular._ACTIVE_PREVIEWS.discard(active.resolve())

    def test_external_attachments_need_explicit_browser_confirmation(self):
        dialog = self.preview()
        item = {'url': 'https://external.invalid/archive', 'name': 'archive'}
        with patch.object(popular.QMessageBox, 'question', return_value=QMessageBox.StandardButton.No), \
                patch.object(popular.QDesktopServices, 'openUrl') as opened:
            dialog._open_external_attachment(item); opened.assert_not_called()
        with patch.object(popular.QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
                patch.object(popular.QDesktopServices, 'openUrl', return_value=True) as opened:
            dialog._open_external_attachment(item)
            self.assertEqual(item['url'], opened.call_args.args[0].toString())

    def test_popular_timeout_sets_the_required_visible_message(self):
        with patch.object(popular.PopularRecommendationsDialog, 'load'):
            dialog = popular.PopularRecommendationsDialog()
        self.dialogs.append(dialog)
        dialog._failed('timeout')
        self.assertEqual('今日热搜数据流中断，请稍后再试', dialog.status.text())

    def test_artist_id_tag_has_right_slide_and_fade_animation(self):
        card = popular.RecommendationCard('65139682'); card.resize(260, 180); card.show()
        event = QEnterEvent(QPointF(10, 10), QPointF(10, 10), QPointF(10, 10))
        card.enterEvent(event); QTest.qWait(220)
        self.assertEqual(card._tag_position(), card.artist_tag.pos())
        self.assertEqual(1, card._tag_effect.opacity())
        self.assertIn('65139682', card.artist_tag.text())
        card.leaveEvent(QEvent(QEvent.Type.Leave)); QTest.qWait(220)
        self.assertEqual(0, card._tag_effect.opacity())
        card.close(); card.deleteLater()


if __name__ == '__main__':
    unittest.main()
