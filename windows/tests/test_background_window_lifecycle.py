"""Real Qt regressions for independent dialog/parent background lifetimes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QSettings
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QDialog, QWidget

import background_ui
from background_store import BackgroundStore


class _KeepOpenBackgroundDialog(QDialog):
    def closeEvent(self, event):
        event.ignore()


class BackgroundWindowLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive-background-lifecycle-')
        self.root = Path(self.temp.name)
        self.store = BackgroundStore(self.root / 'state')
        self.windows = []
        self._save_background('workspace', '#f04020')
        self.store.set_mode('custom')
        self.host = self._window('workspace')
        self.host.show()
        self.qt.processEvents()
        self.assertFalse(self.host._background_layer.pixmap.isNull())

    def tearDown(self):
        for window in reversed(self.windows):
            window.hide()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qt.processEvents()
        self.temp.cleanup()

    def _save_background(self, page, color):
        image = QImage(80, 60, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        source = self.root / f'{page}-source.png'
        self.assertTrue(image.save(str(source)))
        buffer = QBuffer()
        self.assertTrue(buffer.open(QIODevice.OpenModeFlag.WriteOnly))
        self.assertTrue(image.save(buffer, 'JPG'))
        self.store.save_page(page, source=source, crop={},
                             preview_bytes=bytes(buffer.data()), source_size=(80, 60),
                             preview_size=(80, 60), aspect=4/3)

    def _window(self, page, parent=None, window_type=None):
        window_type = window_type or (QDialog if parent else QWidget)
        window = window_type(parent)
        window.resize(320, 240)
        self.windows.append(window)
        background_ui.install_background_host(window, self.store, page)
        return window

    def test_unconfigured_dialogs_never_remove_visible_workspace_image(self):
        original = self.host._background_layer.pixmap.cacheKey()
        # These are the four entry points from the user report, plus nested
        # chooser/scan windows using the same common background installer.
        for page in ('artist_cards', 'schedules', 'calendar', 'subscriptions',
                     'picker', 'scan', 'resume_choice'):
            with self.subTest(page=page):
                dialog = self._window(page, self.host)
                self.assertEqual(self.host._background_layer.pixmap.cacheKey(), original,
                                 'Constructing a hidden child must not clear its parent')
                dialog.show()
                self.qt.processEvents()
                self.assertTrue(dialog._background_layer.pixmap.isNull())
                self.assertEqual(self.host._background_layer.pixmap.cacheKey(), original)
                dialog.close()
                self.qt.processEvents()
                self.assertEqual(self.host._background_layer.pixmap.cacheKey(), original)

    def test_actual_workspace_and_four_reported_dialogs_preserve_custom_background(self):
        from app_state import AppState
        from desktop_runtime import DesktopRuntime
        from download_journal import DownloadJournal
        from feature_dialogs import ArtistCardsDialog, ScheduleDialog, CheckInDialog
        from subscription_ui import SubscriptionDialog
        import download_pawchive_images as desktop

        class NoSystemTasks:
            def install(self, *_):
                raise AssertionError('No operating-system changes in background tests')

            remove = install

        state = AppState(self.root / 'app-state')
        runtime = DesktopRuntime(self.qt, state, DownloadJournal(state.data_dir),
                                 NoSystemTasks(), tray_enabled=False)
        window = None
        dialogs = []
        with patch.object(desktop.App, 'start_subscription_check', return_value=None), \
             patch('requests.sessions.Session.request',
                   side_effect=AssertionError('Background tests must not access the network')) as network:
            try:
                settings = QSettings(str(self.root / 'preferences.ini'), QSettings.Format.IniFormat)
                window = desktop.App(settings, services=runtime)
                runtime.attach(window)
                runtime.timer.stop()
                window.background_store = self.store
                self.store.register_layer('workspace', window.background_layer)
                window.refresh_background()
                window.show()
                self.qt.processEvents()
                original = window.background_layer.pixmap.cacheKey()
                self.assertNotEqual(original, 0)
                constructors = (
                    lambda: ArtistCardsDialog(window, state),
                    lambda: ScheduleDialog(window, runtime),
                    lambda: CheckInDialog(window, state),
                    lambda: SubscriptionDialog(window, state),
                )
                for constructor in constructors:
                    dialog = constructor()
                    dialogs.append(dialog)
                    self.assertEqual(window.background_layer.pixmap.cacheKey(), original)
                    dialog.show()
                    self.qt.processEvents()
                    self.assertEqual(window.background_layer.pixmap.cacheKey(), original)
                    window.refresh_background()
                    self.assertEqual(window.background_layer.pixmap.cacheKey(), original)
                    dialog.reject()
                    self.qt.processEvents()
                    self.assertEqual(window.background_layer.pixmap.cacheKey(), original)
                network.assert_not_called()
            finally:
                for dialog in dialogs:
                    dialog.reject()
                    dialog.deleteLater()
                runtime.prepare_exit()
                if window is not None:
                    window.busy = False
                    window.close()
                    window.deleteLater()
                runtime.close()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.qt.processEvents()

    def test_visible_parent_and_dialog_keep_independent_previews_without_reloading(self):
        self._save_background('artist_cards', '#20c060')
        real_loader = background_ui.load_preview
        with patch.object(background_ui, 'load_preview', wraps=real_loader) as loader:
            dialog = self._window('artist_cards', self.host)
            self.assertEqual(loader.call_count, 0, 'Hidden windows should not decode previews')
            for _ in range(3):
                dialog.show()
                self.qt.processEvents()
                self.assertFalse(dialog._background_layer.pixmap.isNull())
                self.assertFalse(self.host._background_layer.pixmap.isNull())
                self.assertNotEqual(dialog._background_layer.pixmap.cacheKey(),
                                    self.host._background_layer.pixmap.cacheKey())
                # A refresh in either window must leave the other visible one
                # intact and reuse the already-decoded preview.
                dialog.refresh_background()
                self.host.refresh_background()
                dialog.resize(dialog.width() + 1, dialog.height())
                self.qt.processEvents()
                self.assertLessEqual(len(self.store._cache), self.store.cache_limit)
                dialog.hide()
                self.qt.processEvents()
                self.assertTrue(dialog._background_layer.pixmap.isNull())
                self.assertFalse(self.host._background_layer.pixmap.isNull())
                self.assertIn('workspace', self.store._cache)
                self.assertNotIn('artist_cards', self.store._cache)
            self.assertEqual(loader.call_count, 3, 'Only each reopened dialog reloads its small preview')

    def test_nested_same_page_windows_are_registered_and_released_independently(self):
        self._save_background('picker', '#3060f0')
        first = self._window('picker', self.host)
        first.show()
        second = self._window('picker', first)
        second.show()
        self.qt.processEvents()
        self.assertEqual(len(self.store._layers['picker']), 2)
        first_key = first._background_layer.pixmap.cacheKey()
        second.hide()
        self.qt.processEvents()
        self.assertTrue(second._background_layer.pixmap.isNull())
        self.assertEqual(first._background_layer.pixmap.cacheKey(), first_key)
        self.assertIn('picker', self.store._cache)
        second.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.windows.remove(second)
        self.store.activate_page('workspace')
        self.assertEqual(len(self.store._layers['picker']), 1)
        first.hide()
        self.qt.processEvents()
        self.assertNotIn('picker', self.store._cache)

    def test_ignored_close_keeps_dialog_image(self):
        self._save_background('calendar', '#d0a040')
        dialog = self._window('calendar', self.host, _KeepOpenBackgroundDialog)
        dialog.show()
        self.qt.processEvents()
        original = dialog._background_layer.pixmap.cacheKey()
        dialog.close()
        self.qt.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog._background_layer.pixmap.cacheKey(), original)

    def test_hidden_windows_release_layers_and_default_theme_clears_all_images(self):
        self._save_background('subscriptions', '#70c0d0')
        dialog = self._window('subscriptions', self.host)
        dialog.show()
        self.qt.processEvents()
        self.host.hide()
        self.qt.processEvents()
        self.assertTrue(self.host._background_layer.pixmap.isNull())
        self.assertNotIn('workspace', self.store._cache)
        self.assertFalse(dialog._background_layer.pixmap.isNull())
        self.host.show()
        self.qt.processEvents()
        self.store.set_mode('default')
        self.host.refresh_background()
        self.assertTrue(self.host._background_layer.pixmap.isNull())
        self.assertTrue(dialog._background_layer.pixmap.isNull())
        self.assertFalse(self.store._cache)


if __name__ == '__main__':
    unittest.main()
