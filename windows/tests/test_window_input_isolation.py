"""Exercise actual dialog events without allowing them to reach the workspace."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if os.name == 'nt':
    import ctypes
    # Keep an unexpected native regression in the captured test log instead
    # of leaving a Windows Error Reporting dialog on the user's desktop.
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, QSettings, QTimer, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QVBoxLayout

import download_pawchive_images as desktop
from window_chrome import install_window_chrome


class WindowInputIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_dialog_input_')
        self.addCleanup(self.temp.cleanup)
        self.network = patch('requests.sessions.Session.request',
            side_effect=AssertionError('Dialog tests must stay offline'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.dialogs = []
        self.window = desktop.App(QSettings(str(Path(self.temp.name)/'settings.ini'), QSettings.Format.IniFormat))
        self.addCleanup(self.cleanup_window)
        self.window.show()
        self.qt.processEvents()

    def cleanup_window(self):
        # In particular run this after a failed setUp. Native Qt filters may
        # outlive their Python window if only tearDown performs removal.
        self.qt.removeEventFilter(self.window)
        for dialog in reversed(self.dialogs):
            dialog.close()
            dialog.deleteLater()
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def dialog(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle('Tool dialog')
        dialog.resize(300, 180)
        self.dialogs.append(dialog)
        return dialog

    def test_click_and_move_in_dialog_never_toggle_theme_or_spawn_main_effects(self):
        dialog = self.dialog()
        # Deliberately cover the extra clickable margin around the main
        # theme switch. The old global filter treated this child dialog's
        # click as a main-window theme click and swallowed its own action.
        origin = self.window.theme.mapToGlobal(QPoint())
        target = origin + QPoint(-1, self.window.theme.height()//2)
        dialog.move(target-QPoint(50, 90))
        label = QLabel('Foreground surface', dialog)
        label.setGeometry(10, 55, 260, 100)
        dialog.show()
        self.qt.processEvents()
        self.window.particles.clear()
        self.window.fx.stop()
        before = self.window.theme.dark
        point = label.mapFromGlobal(target)
        with patch.object(desktop.random, 'random', return_value=0):
            # Send the foreground event directly; QTest.mouseMove also moves
            # the system cursor across unrelated portions of the workspace.
            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(target),
                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(label, move)
            QTest.mouseClick(label, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.window.theme.dark, before)
        self.assertEqual(self.window.particles, [])
        self.assertTrue(dialog.testAttribute(Qt.WidgetAttribute.WA_NoMousePropagation))

    def test_modal_dialog_blocks_explicit_mouse_delivery_to_main_controls(self):
        dialog = self.dialog()
        dialog.setModal(True)
        dialog.show()
        self.qt.processEvents()
        self.assertIs(self.qt.activeModalWidget(), dialog)
        self.window.particles.clear()
        before = self.window.theme.dark
        QTest.mouseClick(self.window.theme, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.theme.dark, before)
        self.assertEqual(self.window.particles, [])

    def test_url_drops_stay_with_their_own_top_level_window(self):
        dialog = self.dialog()
        dialog.setAcceptDrops(True)
        dialog.show()
        self.qt.processEvents()
        mime = QMimeData()
        mime.setText('https://pawchive.pw/fanbox/user/3439325')
        def enter(widget):
            event = QDragEnterEvent(QPoint(20, 20), Qt.DropAction.CopyAction,
                mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(widget, event)
            return event
        def drop(widget):
            event = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction,
                mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(widget, event)
            return event
        with patch.object(self.window, 'handle_link_text', return_value={'profile':'test'}) as dispatch:
            self.assertFalse(enter(dialog).isAccepted())
            drop(dialog)
            dispatch.assert_not_called()
            self.assertTrue(enter(self.window).isAccepted())
            self.assertTrue(drop(self.window).isAccepted())
            dispatch.assert_called_once()

    def test_modal_exec_accept_does_not_queue_a_stale_reopen(self):
        dialog = self.dialog()
        QVBoxLayout(dialog).addWidget(QLabel('Confirm once'))
        QTimer.singleShot(0, dialog.accept)
        self.assertEqual(dialog.exec(), QDialog.DialogCode.Accepted)
        for _ in range(3):
            self.qt.processEvents()
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)

    def test_tool_caption_is_close_only_and_reserves_top_margin_once(self):
        dialog = self.dialog()
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addWidget(QLabel('Settings form'))
        dialog.ensurePolished()
        dialog.show()
        self.qt.processEvents()
        bar = dialog._pawchive_window_chrome
        self.assertTrue(bar.minimize.isHidden())
        self.assertTrue(bar.maximize.isHidden())
        self.assertTrue(bar.close.isVisible())
        self.assertEqual(layout.contentsMargins().top(), 8+bar.height())
        for _ in range(3):
            install_window_chrome(dialog)
            dialog.resize(dialog.width()+5, dialog.height()+5)
            self.qt.processEvents()
        self.assertEqual(layout.contentsMargins().top(), 8+bar.height())
        QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=QPoint(90, 16))
        self.assertFalse(dialog.isMaximized())
        self.assertFalse(self.window._pawchive_window_chrome.minimize.isHidden())
        self.assertFalse(self.window._pawchive_window_chrome.maximize.isHidden())
        QTest.mouseClick(bar.close, Qt.MouseButton.LeftButton)
        self.assertFalse(dialog.isVisible())

    def test_image_viewer_can_opt_into_resize_without_minimize(self):
        dialog = self.dialog()
        dialog.setProperty('pawchiveChromeControls', 'resize')
        QVBoxLayout(dialog).addWidget(QLabel('Image preview'))
        dialog.show()
        self.qt.processEvents()
        bar = dialog._pawchive_window_chrome
        self.assertTrue(bar.minimize.isHidden())
        self.assertTrue(bar.maximize.isVisible())
        bar.toggle_maximized()
        self.assertTrue(dialog.isMaximized())
        bar.toggle_maximized()
        self.assertFalse(dialog.isMaximized())

    def test_caption_tooltips_inherit_the_selected_app_language(self):
        self.window.set_lang('English')
        dialog = self.dialog()
        dialog.show()
        self.qt.processEvents()
        self.assertEqual(dialog._pawchive_window_chrome.close.toolTip(), 'Close window')
        self.assertEqual(self.window._pawchive_window_chrome.minimize.toolTip(), 'Minimize window')
        self.window.set_lang('日本語')
        dialog._pawchive_window_chrome.refresh_language()
        self.assertEqual(dialog._pawchive_window_chrome.close.toolTip(), 'ウィンドウを閉じる')


if __name__ == '__main__':
    unittest.main()
