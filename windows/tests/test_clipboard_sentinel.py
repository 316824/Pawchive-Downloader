import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLineEdit

from app_state import AppState
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal


class FakeTray:
    def __init__(self):
        self.messages = []

    def hide(self):
        pass

    def showMessage(self, title, message, *_):
        self.messages.append((title, message))


class FakeWindow:
    def __init__(self):
        self.lang = '简体中文'
        self.busy = False
        self.profile = QLineEdit()
        self.path = QLineEdit()
        self.downloads = 0

    def isVisible(self):
        return False

    def showNormal(self):
        pass

    def raise_(self):
        pass

    def activateWindow(self):
        pass

    def set_busy_state(self, _value):
        pass

    def download(self):
        self.downloads += 1


class FakeApp:
    def activeModalWidget(self):
        return None


class ClipboardSentinelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_clipboard_test_')
        root = Path(self.temp.name)
        self.state = AppState(root / 'state')
        self.journal = DownloadJournal(root / 'state')
        self.runtime = DesktopRuntime(self.qt_app, self.state, self.journal,
                                      task_scheduler=type('Tasks', (), {})(),
                                      tray_enabled=False)
        self.runtime.app = FakeApp()
        self.runtime.window = FakeWindow()
        self.runtime.tray = FakeTray()
        self.runtime._clipboard_last_text = self.runtime._clipboard_text()

    def tearDown(self):
        self.runtime.close()
        self.journal.close()
        self.state.close()
        self.temp.cleanup()

    def test_new_creator_link_notifies_once_and_click_starts_when_folder_exists(self):
        clipboard = QApplication.clipboard()
        clipboard.setText('https://pawchive.pw/fanbox/user/demo-artist?from=copy')
        self.runtime._clipboard_last_text = ''
        self.runtime.poll_clipboard()
        self.assertEqual(len(self.runtime.tray.messages), 1)
        self.assertIn('检测到新图集', self.runtime.tray.messages[0][1])
        self.runtime.poll_clipboard()
        self.assertEqual(len(self.runtime.tray.messages), 1)
        self.runtime.window.path.setText(str(Path(self.temp.name) / 'saved'))
        self.runtime.accept_clipboard_download()
        self.assertEqual(self.runtime.window.profile.text(), 'https://pawchive.pw/fanbox/user/demo-artist')
        self.assertEqual(self.runtime.window.downloads, 1)

    def test_startup_baseline_does_not_replay_existing_clipboard(self):
        clipboard = QApplication.clipboard()
        value = 'https://pawchive.pw/fanbox/user/demo-artist'
        clipboard.setText(value)
        self.runtime._clipboard_last_text = value
        self.runtime.poll_clipboard()
        self.assertEqual(self.runtime.tray.messages, [])

    def test_cooldown_defers_the_latest_link_until_notifications_are_allowed(self):
        clipboard = QApplication.clipboard()
        first = 'https://pawchive.pw/fanbox/user/first-artist'
        second = 'https://pawchive.pw/fanbox/user/second-artist'
        clipboard.setText(first)
        self.runtime._clipboard_last_text = ''
        self.runtime.poll_clipboard()
        clipboard.setText(second)
        self.runtime.poll_clipboard()
        self.assertEqual(len(self.runtime.tray.messages), 1)
        self.runtime._clipboard_last_notice_at = time.monotonic() - self.runtime.CLIPBOARD_NOTICE_COOLDOWN_SECONDS - 1
        self.runtime.poll_clipboard()
        self.assertEqual(len(self.runtime.tray.messages), 2)
        self.assertIn(second, self.runtime._clipboard_seen)

    def test_visible_link_handling_can_suppress_the_sentinel(self):
        value = 'https://pawchive.pw/fanbox/user/handled-artist'
        clipboard = QApplication.clipboard()
        clipboard.setText(value)
        self.runtime._clipboard_last_text = ''
        self.runtime.suppress_clipboard_text(value)
        self.runtime.poll_clipboard()
        self.assertEqual(self.runtime.tray.messages, [])

    def test_completion_uses_friendly_tray_copy_when_hidden(self):
        self.runtime.task_finished(True)
        self.assertEqual(self.runtime.tray.messages[-1][1], '主人，你的图集下载好了哦～')


if __name__ == '__main__':
    unittest.main()
