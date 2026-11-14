"""Normal/portrait/maximized transitions must keep a usable window geometry."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QWidget

import download_pawchive_images as desktop


class WindowModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='pawchive_window_test_')
        settings = QSettings(str(Path(self.directory.name)/'settings.ini'), QSettings.Format.IniFormat)
        with patch.object(desktop, 'QSettings', return_value=settings):
            self.window = desktop.App()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.directory.cleanup()

    def test_expanded_window_blocks_portrait_until_restored(self):
        for expand in (self.window.showMaximized, self.window.showFullScreen):
            expand()
            self.app.processEvents()
            self.assertFalse(self.window.layout_button.isEnabled())
            self.window.set_layout_mode(True)
            self.assertFalse(self.window.portrait_mode)
            self.window.showNormal()
            self.app.processEvents()
            self.assertTrue(self.window.layout_button.isEnabled())

    def test_portrait_blocks_buttons_methods_and_native_expansion(self):
        self.window.set_layout_mode(True)
        self.app.processEvents()
        geometry = self.window.geometry()
        self.assertFalse(self.window._pawchive_window_chrome.maximize.isEnabled())
        self.window._pawchive_window_chrome.toggle_maximized()
        self.window.showMaximized()
        self.window.showFullScreen()
        self.assertFalse(self.window.isMaximized() or self.window.isFullScreen())
        # Shell shortcuts can bypass the Python method override entirely.
        QWidget.showMaximized(self.window)
        self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(self.window.isMaximized())
        self.assertEqual(self.window.geometry(), geometry)
        self.window.set_layout_mode(False)
        self.assertTrue(self.window._pawchive_window_chrome.maximize.isEnabled())


if __name__ == '__main__':
    unittest.main()
