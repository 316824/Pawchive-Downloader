"""A requested scan finish keeps the current album and opens its real picker flow.

Only HTTP responses are replaced. The application, scanner QThread, signals and
scan dialog completion timer are production objects; no network is permitted.
"""
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

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication
from requests.structures import CaseInsensitiveDict
from shiboken6 import isValid

import download_pawchive_images as desktop


PROFILE = 'https://pawchive.pw/fanbox/user/123'
FIRST_POST = PROFILE + '/post/1'
SECOND_POST = PROFILE + '/post/2'


class Response:
    def __init__(self, body, gate=None):
        self.body = body.encode('utf-8')
        self.status_code = 200
        self.headers = CaseInsensitiveDict({'content-length': str(len(self.body))})
        self.encoding = 'utf-8'
        self.gate = gate
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        if self.gate:
            # Hold a response already being parsed, rather than stopping before
            # the first request. Finishing must consume and retain this album.
            yield self.body[:16]
            self.gate[0].set()
            if not self.gate[1].wait(5):
                raise TimeoutError('The offline detail-response gate was not released')
            yield self.body[16:]
        else:
            yield self.body

    def close(self):
        self.closed = True


class GracefulScanFinishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pawchive_graceful_scan_')
        self.root = Path(self.temporary.name)
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        self.sessions_closed = []
        self.network = patch('requests.sessions.Session.request',
            side_effect=AssertionError('No live HTTP in graceful-finish regressions'))
        self.network.start()
        self.window = desktop.App(QSettings(str(self.root / 'settings.ini'),
            QSettings.Format.IniFormat))
        self.window.path.setText(str(self.root))
        self.window.select_posts_check.setChecked(True)
        self.window._handling_link_input = True
        try:
            self.window.profile.setText(PROFILE)
        finally:
            self.window._handling_link_input = False
        self.window.show()
        self.qt.processEvents()

    def tearDown(self):
        self.release.set()
        scanner = getattr(self.window, 'scanner', None)
        if scanner is not None and isValid(scanner):
            scanner.paused = False
            scanner.finish_requested = True
            if scanner.isRunning():
                scanner.requestInterruption()
                self.assertTrue(scanner.wait(6000), 'The scanner must exit before widgets are destroyed')
        splash = getattr(self.window, 'splash', None)
        if splash is not None and isValid(splash):
            splash._completion_callback = None
            splash._allow_close = True
            splash.close()
            splash.deleteLater()
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.network.stop()
        self.temporary.cleanup()

    def wait_until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(.002)
        self.assertTrue(condition(), 'Timed out while pumping the actual Qt event loop')

    def session_factory(self):
        profile_html = ''.join(
            '<article class="post-card"><a href="' + url + '"></a>'
            '<div class="post-card__header">' + title + '</div>'
            '<time datetime="2026-11-28 12:00:00"></time></article>'
            for url, title in ((FIRST_POST, 'First album'), (SECOND_POST, 'Never fetched')))
        detail_html = ('<div class="post__files"><a class="fileThumb" '
            'href="https://file.pawchive.pw/data/first.jpg" download="first.jpg">'
            '<img src="https://file.pawchive.pw/thumb/first.jpg"></a></div>')
        responses = {
            PROFILE: Response(profile_html),
            FIRST_POST: Response(detail_html, (self.entered, self.release)),
        }
        owner = self

        class Session:
            def __init__(self):
                self.headers = {}

            def get(self, url, **kwargs):
                owner.calls.append(url)
                if url not in responses:
                    raise AssertionError('Requested a subsequent album/page after finish: ' + url)
                return responses[url]

            def close(self):
                owner.sessions_closed.append(True)

        return Session, responses

    def finish_current_album(self, action):
        session, responses = self.session_factory()
        results, errors, counts = [], [], []
        with (patch.object(desktop.requests, 'Session', session),
              patch.object(self.window, 'choose_titles', return_value=None) as choose,
              patch.object(self.window, '_start_queue', return_value=True) as start):
            self.window.download()
            worker, splash = self.window.scanner, self.window.splash
            worker.result.connect(results.append)
            worker.failed.connect(errors.append)
            worker.counts.connect(lambda page, total: counts.append((page, total)))
            self.wait_until(self.entered.is_set)
            self.assertTrue(worker.isRunning())
            self.assertEqual(self.calls, [PROFILE, FIRST_POST])
            # A manual stop must open the picker even if both automatic paths
            # would otherwise send all scan results straight to the queue.
            splash.auto_queue_check.setChecked(True)
            self.window.select_posts_check.setChecked(False)
            if action == 'finish_button':
                splash.finish_button.click()
            elif action == 'main_stop':
                self.window.stop_download()
            elif action == 'paused_finish':
                splash.pause_button.click()
                self.assertTrue(worker.paused)
                splash.finish_button.click()
            elif action == 'window_close':
                splash.close()
                self.assertFalse(splash.isVisible())
            else:
                self.fail('Unknown finish action')
            self.assertTrue(worker.finish_requested)
            self.assertTrue(splash.finish_requested)
            self.assertFalse(worker.paused)
            self.assertFalse(splash.paused)
            self.assertFalse(worker.isInterruptionRequested())
            self.assertEqual(splash.finish_button.text(),
                desktop.ui_text(self.window.lang, '正在结束当前图集…'))
            self.assertFalse(splash.finish_button.isEnabled())
            self.assertFalse(choose.called)
            self.release.set()
            self.wait_until(lambda: choose.called)
            self.wait_until(lambda: bool(results))
            self.assertFalse(errors)
            self.assertEqual(len(results), 1)
            self.assertEqual([entry['url'] for entry in results[0]], [FIRST_POST])
            entry = results[0][0]
            self.assertEqual(entry['title'], 'First album')
            self.assertEqual(entry['file_count'], 1)
            self.assertEqual(entry['image_count'], 1)
            self.assertEqual(len(entry['assets']), 1)
            self.assertEqual(choose.call_count, 1)
            self.assertEqual(choose.call_args.args[0], results[0])
            self.assertEqual(self.calls, [PROFILE, FIRST_POST])
            self.assertIn((1, 1), counts)
            self.assertEqual(self.sessions_closed, [True])
            self.assertTrue(all(response.closed for response in responses.values()))
            self.assertFalse(worker.isRunning())
            self.assertFalse(splash.isVisible())
            self.assertIsNone(splash._completion_overlay)
            self.assertTrue(splash._automatic_completion)
            self.assertFalse(self.window.busy)
            self.assertEqual(self.window.workflow_state, 'ready')
            start.assert_not_called()

    def test_finish_button_retains_current_album_and_opens_picker(self):
        self.finish_current_album('finish_button')

    def test_main_stop_finishes_scan_without_interrupting_current_response(self):
        self.finish_current_album('main_stop')

    def test_finish_unpauses_scanner_and_dissolves_to_picker(self):
        self.finish_current_album('paused_finish')

    def test_closing_scan_window_does_not_strand_the_result_picker(self):
        self.finish_current_album('window_close')


if __name__ == '__main__':
    unittest.main()
