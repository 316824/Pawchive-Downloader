"""Offline update metadata, bounded HTTP and Qt interaction checks."""
import json
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

import requests
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QUrl
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from app_state import AppState
import update_checker as updates


def release_payload(tag='v1.4.0', **overrides):
    value = dict(tag_name=tag, name='新版说明', body='## 更新\n\n- 修复下载',
                 html_url=updates.RELEASE_ROOT + tag, draft=False, prerelease=False)
    value.update(overrides)
    return value


class Response:
    def __init__(self, body=None, *, headers=None, status=200, chunks=None):
        self.status_code = status
        self.headers = headers if headers is not None else {'Content-Type': 'application/json; charset=utf-8'}
        self.body = json.dumps(release_payload()).encode() if body is None else body
        self.chunks = chunks
        self.closed = False

    def iter_content(self, **kwargs):
        yield from self.chunks if self.chunks is not None else [self.body]

    def close(self):
        self.closed = True


class UpdateParsingTests(unittest.TestCase):
    def test_versions_compare_numeric_components_and_normalize_old_minor_tag(self):
        self.assertEqual(updates.parse_version('v1.3'), updates.parse_version('1.3.0'))
        self.assertGreater(updates.parse_version('v1.10.0'), updates.parse_version('v1.9.99'))
        self.assertGreater(updates.parse_version('1.3.10'), updates.parse_version('1.3.9'))
        self.assertEqual(updates.parse_version('1.3.0+build.7'), updates.parse_version('1.3.0'))
        self.assertLess(updates.parse_version('1.4.0-beta.2'), updates.parse_version('1.4.0-beta.11'))
        self.assertLess(updates.parse_version('1.4.0-rc.1'), updates.parse_version('1.4.0'))

    def test_invalid_version_text_cannot_be_an_update(self):
        for value in ('1', 'v01.3.0', '1.03.0', '1.3.00', '1.3.0.4',
                      '1.4.0-alpha.01', '1.4.0-', '1.4.0+', '最新版1.4.0',
                      '1.４.0', '1.4.0\nextra', None, 123, '1.'+'9'*150):
            self.assertIsNone(updates.parse_version(value), repr(value))

    def test_download_url_requires_exact_official_repository_release_page(self):
        valid = updates.RELEASE_ROOT + 'v1.4.0'
        self.assertEqual(updates.safe_release_url(valid, 'v1.4.0'), valid)
        for value in ('http://github.com/316824/Pawchive-Downloader/releases/tag/v1.4.0',
                      'https://github.com.evil.test/316824/Pawchive-Downloader/releases/tag/v1.4.0',
                      'https://github.com@evil.test/316824/Pawchive-Downloader/releases/tag/v1.4.0',
                      'https://github.com/evil/Pawchive-Downloader/releases/tag/v1.4.0',
                      valid+'?redirect=evil', valid+'#x', valid+'/../../evil',
                      valid+'%2F..', 'file:///C:/private.txt', '//evil.test',
                      valid+'\n', valid.replace('github.com', 'github.com:443')):
            self.assertIsNone(updates.safe_release_url(value), value)
        self.assertIsNone(updates.safe_release_url(valid, 'v1.5.0'))

    def test_release_filters_drafts_prereleases_and_supplies_bounded_notes(self):
        for changes in (dict(draft=True), dict(prerelease=True), dict(draft='false'),
                        dict(tag_name='v1.4.0-rc.1'), dict(html_url='https://evil.test')):
            self.assertIsNone(updates.parse_release(release_payload(**changes)))
        empty = updates.parse_release(release_payload(body=None))
        self.assertEqual(empty.body, updates.EMPTY_NOTES)
        long = updates.parse_release(release_payload(body='a'*(updates.MAX_BODY_CHARS+20)))
        self.assertLess(len(long.body), updates.MAX_BODY_CHARS+100)

    def test_http_is_one_public_get_with_three_second_timeout_and_closes_response(self):
        response = Response()
        with patch.object(updates.requests, 'get', return_value=response) as get:
            release = updates.fetch_latest_release()
        self.assertEqual(release.tag, 'v1.4.0')
        self.assertTrue(response.closed)
        args, kwargs = get.call_args
        self.assertEqual(args, (updates.LATEST_RELEASE_API,))
        self.assertEqual(kwargs['timeout'], 3)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertTrue(kwargs['stream'])
        self.assertNotIn('Authorization', kwargs['headers'])

    def test_http_errors_invalid_json_and_large_streams_fail_silently(self):
        responses = [Response(status=status) for status in (301, 403, 404, 429, 500)]
        responses += [Response(body=b'not JSON'), Response(body=b'[]'),
                      Response(headers={'Content-Type':'text/html'}),
                      Response(headers={'Content-Type':'application/json', 'Content-Length':str(updates.MAX_RESPONSE_BYTES+1)}),
                      Response(headers={'Content-Type':'application/json', 'Content-Length':'invalid'}),
                      Response(chunks=[b'x'*8192]*70)]
        for response in responses:
            with patch.object(updates.requests, 'get', return_value=response):
                self.assertIsNone(updates.fetch_latest_release())
            self.assertTrue(response.closed)
        for error in (requests.Timeout('offline'), requests.ConnectionError('offline')):
            with patch.object(updates.requests, 'get', side_effect=error):
                self.assertIsNone(updates.fetch_latest_release())

    def test_cancel_and_overall_body_deadline_stop_without_parsing(self):
        with patch.object(updates.requests, 'get') as get:
            self.assertIsNone(updates.fetch_latest_release(interrupted=lambda:True))
            get.assert_not_called()
        response = Response()
        with patch.object(updates.requests, 'get', return_value=response), \
             patch.object(updates.time, 'monotonic', side_effect=(0, 4)):
            self.assertIsNone(updates.fetch_latest_release())
        self.assertTrue(response.closed)


class UpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_updates_test_')
        self.store = AppState(self.temp.name)
        self.window = QWidget()
        self.button = QPushButton(self.window)
        self.controller = updates.install_update_checker(self.window, self.button, self.store,
                                                        delay_ms=100000)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.controller.close()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.store.close()
        self.temp.cleanup()

    def test_only_newer_versions_show_footer_and_ignore_survives_restart(self):
        self.controller._received(updates.parse_release(release_payload('v1.2.2')))
        self.assertFalse(self.button.isVisible())
        self.controller._received(updates.parse_release(release_payload('v1.3.0')))
        self.assertFalse(self.button.isVisible())
        release = updates.parse_release(release_payload())
        self.controller._received(release)
        self.assertTrue(self.button.isVisible())
        self.assertIn('●', self.button.text())
        self.assertIn('v1.4.0', self.button.text())
        self.controller.ignore_version('v1.4.0')
        self.assertFalse(self.button.isVisible())
        reopened = AppState(self.temp.name)
        other = updates.UpdateController(self.window, reopened)
        other._received(release)
        self.assertIsNone(other.release)
        other._received(updates.parse_release(release_payload('v1.5.0')))
        self.assertEqual(other.release.tag, 'v1.5.0')
        other.close()
        reopened.close()

    def test_footer_opens_scrolling_markdown_and_later_keeps_notification(self):
        release = updates.parse_release(release_payload(body='# 修复\n\n'+'说明\n\n'*100))
        self.controller._received(release)
        self.button.click()
        self.app.processEvents()
        dialog = self.controller._dialog
        self.assertIsNotNone(dialog)
        self.assertIn('修复', dialog.notes.toPlainText())
        self.assertGreater(dialog.notes.verticalScrollBar().maximum(), 0)
        dialog.later_button.click()
        self.assertIsNone(self.controller._dialog)
        self.assertTrue(self.button.isVisible())
        self.assertEqual(self.controller.release, release)

    def test_release_markdown_cannot_read_resources_or_open_untrusted_links(self):
        release = updates.parse_release(release_payload(body='![x](file:///C:/private.png)\n\n'
            '<img src="https://evil.test/image.png">\n\n[link](https://evil.test)'))
        dialog = updates.UpdateDialog(self.window, release)
        for url in ('file:///C:/private.png', 'https://evil.test/image.png', 'data:image/png;base64,AA=='):
            self.assertTrue(dialog.notes.loadResource(2, QUrl(url)).isEmpty())
        with patch.object(updates.QDesktopServices, 'openUrl', return_value=True) as open_url:
            dialog._open_notes_link(QUrl('https://evil.test'))
            dialog._open_notes_link(QUrl('file:///C:/private.txt'))
            open_url.assert_not_called()
            dialog.download_button.click()
            self.assertEqual(open_url.call_args.args[0].toString(), release.url)
        dialog.deleteLater()

    def test_worker_result_is_delivered_on_gui_thread_and_start_is_single_shot(self):
        calls, fetch_threads = [], []
        release = updates.parse_release(release_payload())
        def fetch(**kwargs):
            fetch_threads.append(threading.get_ident())
            return release
        self.controller.update_available.connect(lambda value:calls.append((value, QThread.currentThread())))
        with patch.object(updates, 'fetch_latest_release', side_effect=fetch) as request:
            self.controller.start()
            self.controller.start()
            deadline = time.monotonic()+2
            while not calls and time.monotonic()<deadline:
                self.app.processEvents()
                time.sleep(.001)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(calls[0][0], release)
        self.assertEqual(calls[0][1], self.app.thread())
        self.assertNotEqual(fetch_threads[0], threading.get_ident())

    def test_close_does_not_block_socket_timeout_or_update_widgets_after_cancel(self):
        entered, finish = threading.Event(), threading.Event()
        def fetch(**kwargs):
            entered.set()
            finish.wait(2)
            return updates.parse_release(release_payload())
        with patch.object(updates, 'fetch_latest_release', side_effect=fetch):
            self.controller.start()
            self.assertTrue(entered.wait(1))
            worker = self.controller.worker
            start = time.monotonic()
            self.controller.close()
            self.assertLess(time.monotonic()-start, .2)
            finish.set()
            self.assertTrue(worker.wait(1000))
            self.app.processEvents()
        self.assertFalse(self.button.isVisible())
        self.assertIsNone(self.controller.release)


if __name__ == '__main__':
    unittest.main()
