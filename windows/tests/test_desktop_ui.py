"""Offline interaction checks for the desktop layout; never contacts Pawchive."""
import gc
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QScrollArea

import download_pawchive_images as desktop
from post_picker import PostPicker


class DesktopUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        if os.name == 'nt':
            fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
            for name in ('segoeui.ttf', 'msyh.ttc', 'msyhbd.ttc'):
                font = fonts / name
                if font.is_file():
                    QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pawchive_ui_test_')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                          str(self.directory / 'settings'))
        # QSettings(organization, application) always selects NativeFormat on
        # Windows. Supply a real INI file explicitly instead of relying on the
        # process-wide default, which only changes the zero-argument overload.
        self.settings_file = self.directory / 'settings.ini'
        self.settings_factory = patch.object(
            desktop, 'QSettings',
            side_effect=lambda *args, **kwargs: QSettings(str(self.settings_file), QSettings.Format.IniFormat))
        self.settings_factory.start()
        self.addCleanup(self.settings_factory.stop)
        self.network = patch('requests.sessions.Session.request',
                             side_effect=AssertionError('UI tests must not access the network'))
        self.request = self.network.start()
        self.addCleanup(self.network.stop)
        self.windows = []
        self.window = self.open_window()

    def open_window(self):
        window = desktop.App()
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def tearDown(self):
        remaining_caches = []
        for window in reversed(self.windows):
            # No test starts a worker. Request interruption if a future regression
            # does so, rather than leave a thread running after the failure.
            for name in ('worker', 'scanner'):
                worker = getattr(window, name, None)
                if worker is not None and worker.isRunning():
                    worker.requestInterruption()
                    worker.wait(3000)
            window.busy = False
            cache = window.cache_dir
            window.close()
            if cache.exists():
                remaining_caches.append(str(cache))
            window.deleteLater()
        self.windows.clear()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()
        self.request.assert_not_called()
        self.assertFalse(remaining_caches, 'Test window caches were not removed: ' + ', '.join(remaining_caches))

    def fill_valid_inputs(self):
        self.window.profile.setText('https://pawchive.pw/fanbox/user/offline-artist')
        self.window.path.setText(str(self.directory))
        self.window.validate_profile()

    @staticmethod
    def post_entries():
        return [dict(title=title, url=f'https://pawchive.pw/fanbox/user/offline-artist/post/{index}',
                     image=None, formats=['.zip', '.mp4'] if index == 1 else [])
                for index, title in enumerate(('Spring study', 'Night palette', 'Spring archive', 'Unsorted'))]

    def picker(self, entries=None):
        dialog = PostPicker(self.window, entries or self.post_entries(), desktop.GlowCheckBox,
                            desktop.AUX_TEXT[self.window.lang])
        dialog.show()
        self.app.processEvents()
        return dialog

    def test_defaults_validation_and_busy_controls_preserve_existing_rules(self):
        window = self.window
        self.assertTrue(window.title_check.isChecked())
        self.assertTrue(window.select_posts_check.isChecked())
        self.assertFalse(window.default_path_check.isChecked())
        self.assertFalse(window.start.isEnabled())
        window.profile.setText('https://pawchive.pw/fanbox/user/offline-artist')
        self.assertFalse(window.start.isEnabled(), 'A valid link alone must not enable download')
        self.fill_valid_inputs()
        self.assertTrue(window.start.isEnabled())
        window.profile.setText('https://pawchive.pw/fanbox/user/offline-artist/post/1')
        self.assertFalse(window.start.isEnabled(), 'The scan entry accepts creator pages only')
        self.fill_valid_inputs()
        window.set_busy_state(True)
        for control in (window.start, window.profile, window.browse, window.order_combo,
                        window.title_check, window.select_posts_check, window.clear_cache):
            self.assertFalse(control.isEnabled(), type(control).__name__)
        window.set_busy_state(False)
        self.assertTrue(window.start.isEnabled())
        self.assertTrue(window.browse.isEnabled())

    def test_paste_and_log_toggle_are_single_click_actions_without_network(self):
        window = self.window
        QApplication.clipboard().setText('  https://pawchive.pw/fanbox/user/offline-artist  ')
        QTest.mouseClick(window.paste_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(window.profile.text(), 'https://pawchive.pw/fanbox/user/offline-artist')
        self.assertFalse(window.start.isEnabled())
        visible = window.logs_card.isVisible()
        QTest.mouseClick(window.focus_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(window.logs_card.isVisible(), not visible)
        QTest.mouseClick(window.focus_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(window.logs_card.isVisible(), visible)

    def test_theme_switch_responds_to_each_click_and_keeps_overlay_mouse_through(self):
        window = self.window
        self.assertTrue(window.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        original = window.theme.dark
        QTest.mouseClick(window.theme, Qt.MouseButton.LeftButton)
        self.assertEqual(window.theme.dark, not original)
        QTest.qWait(450)
        QTest.mouseClick(window.theme, Qt.MouseButton.LeftButton)
        self.assertEqual(window.theme.dark, original)
        QTest.qWait(450)
        self.assertFalse(window.grab().isNull())
        self.assertEqual(window.overlay.geometry(), window.rect())

    def test_renaming_options_and_default_path_survive_layout_changes(self):
        window = self.window
        self.assertFalse(window.rename_container.isVisible())
        window.title_check.setChecked(False)
        self.app.processEvents()
        self.assertTrue(window.rename_container.isVisible())
        self.assertEqual(window.start_number.value(), 1)
        window.title_check.setChecked(True)
        self.assertFalse(window.rename_container.isVisible())
        window.path.setText(str(self.directory))
        window.default_path_check.setChecked(True)
        window.settings.sync()
        reopened = self.open_window()
        self.assertEqual(reopened.path.text(), str(self.directory))

    def test_copying_logs_keeps_completed_folder_available(self):
        window = self.window
        self.fill_valid_inputs()
        window.log.append('Offline diagnostic line')
        window.done(1)
        self.assertTrue(window.open_folder_button.isVisible())
        window.language.setCurrentText('English')
        self.assertEqual(window.workflow_state, 'complete')
        self.assertTrue(window.open_folder_button.isVisible())
        QTest.mouseClick(window.copy_log, Qt.MouseButton.LeftButton)
        self.assertEqual(QApplication.clipboard().text(), 'Offline diagnostic line')
        self.assertEqual(Path(window.completed_path), self.directory)
        self.assertTrue(window.open_folder_button.isVisible())

    def test_stop_requests_interruption_once_and_restores_controls_after_cancellation(self):
        class HeldWorker:
            def __init__(self):
                self.running = True
                self.interruptions = 0

            def isRunning(self):
                return self.running

            def requestInterruption(self):
                self.interruptions += 1

            def wait(self, *args):
                self.running = False
                return True

        window = self.window
        self.fill_valid_inputs()
        window.worker = HeldWorker()
        window.set_busy_state(True)
        window.set_workflow('download')
        cached = window.cache_dir / 'active-cover.tmp'
        cached.write_bytes(b'offline test cache')
        QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
        self.assertEqual(window.worker.interruptions, 1)
        self.assertEqual(window.workflow_state, 'stopping')
        self.assertTrue(window.busy)
        self.assertFalse(window.start.isEnabled())
        self.assertFalse(window.stop_button.isEnabled())
        self.assertTrue(cached.exists(), 'Stopping must wait for the active task before clearing caches')
        QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
        self.assertEqual(window.worker.interruptions, 1)
        window.error(desktop.AUX_TEXT[window.lang]['cancelled'])
        self.assertEqual(window.workflow_state, 'stopped')
        self.assertFalse(window.busy)
        self.assertTrue(window.start.isEnabled())
        self.assertTrue(window.browse.isEnabled())
        self.assertFalse(window.open_folder_button.isVisible())
        self.assertIsNone(window.completed_path)

    def test_download_overview_counts_files_without_mistaking_them_for_albums(self):
        window = self.window
        long_name = 'Offline_file_with_a_long_name_' * 12 + '.png'
        window.update_progress('(2/8) Offline album / ' + long_name, 45, 100)
        self.assertEqual(window.progress.value, 45)
        self.assertEqual(window.file_name.full_text, long_name)
        self.assertEqual(window.file_name.toolTip(), long_name)
        self.assertIn('45', window.percent_label.text())
        self.assertRegex(window.album_value.text(), r'2\s*/\s*8')
        window.update_progress('✓  (2/8) Offline album / first.png', 1, 1)
        window.update_progress('✓  (2/8) Offline album / second.png', 1, 1)
        self.assertEqual(re.findall(r'\d+', window.files_value.text()), ['2'])
        self.assertRegex(window.album_value.text(), r'2\s*/\s*8')
        window.update_progress('(3/8) Next album / third.png', 0, 0)
        self.assertTrue(window.progress.indeterminate)
        self.assertNotIn('100', window.percent_label.text())
        self.assertRegex(window.album_value.text(), r'3\s*/\s*8')
        self.assertEqual(re.findall(r'\d+', window.files_value.text()), ['2'])

    def test_compact_window_does_not_grow_and_wrapped_inputs_remain_reachable(self):
        window = self.window
        requested = window.minimumSize()
        for language in desktop.TEXT:
            with self.subTest(language=language):
                window.language.setCurrentText(language)
                window.resize(requested)
                self.app.processEvents()
                QTest.qWait(30)
                self.assertEqual(window.size(), requested,
                                 'Showing or translating the window must not force a taller size')
                for button in (window.focus_button, window.theme, window.clear_cache, window.language):
                    position = button.mapTo(window, QPoint())
                    self.assertTrue(window.rect().contains(QRect(position, button.size())),
                                    'A header control is outside the compact window')
                for control in (window.profile_label, window.profile, window.folder_label, window.path,
                                window.default_path_check, window.order_combo, window.title_check,
                                window.select_posts_check, window.start):
                    scroll = control.parentWidget()
                    while scroll is not None and not isinstance(scroll, QScrollArea):
                        scroll = scroll.parentWidget()
                    if isinstance(scroll, QScrollArea):
                        scroll.ensureWidgetVisible(control, 4, 4)
                        self.app.processEvents()
                        viewport = scroll.viewport()
                    else:
                        viewport = window
                    position = control.mapTo(viewport, QPoint())
                    self.assertTrue(viewport.rect().contains(QRect(position, control.size())),
                                    f'Input or heading is clipped: {control.objectName()}')
                    if isinstance(control, QLabel) and control.wordWrap():
                        self.assertGreaterEqual(control.height(), control.heightForWidth(control.width()))
                window.set_busy_state(True)
                window.set_workflow('download')
                self.app.processEvents()
                for control in (window.current_label, window.file_name, window.percent_label,
                                window.progress, window.album_value, window.speed_value,
                                window.files_value, window.stop_button):
                    window.activity_scroll.ensureWidgetVisible(control, 4, 4)
                    self.app.processEvents()
                    viewport = window.activity_scroll.viewport()
                    position = control.mapTo(viewport, QPoint())
                    self.assertTrue(viewport.rect().contains(QRect(position, control.size())),
                                    f'Task overview control is clipped: {control.objectName()}')
                self.assertEqual(window.size(), requested)
                window.set_busy_state(False)
                window.set_workflow('setup')

    def test_picker_search_keeps_hidden_selections_and_bulk_controls_update_counts(self):
        dialog = self.picker()
        try:
            dialog.checks[1].setChecked(False)
            dialog.search.setText('spring')
            self.app.processEvents()
            self.assertEqual([i for i, card in enumerate(dialog.cards) if not card.isHidden()], [0, 2])
            self.assertEqual(dialog.selected, {0, 2, 3})
            self.assertEqual(re.findall(r'\d+', dialog.selection_count.text()), ['3', '4'])
            self.assertEqual(re.findall(r'\d+', dialog.visible_count.text()), ['2', '4'])
            dialog.search.setText('a title that does not exist')
            self.app.processEvents()
            self.assertTrue(dialog.empty.isVisible())
            self.assertEqual(dialog.selected, {0, 2, 3})
            self.assertTrue(dialog.confirm_button.isEnabled())
            QTest.mouseClick(dialog.none_button, Qt.MouseButton.LeftButton)
            self.assertFalse(dialog.selected)
            self.assertFalse(dialog.confirm_button.isEnabled())
            self.assertTrue(all(not check.isChecked() for check in dialog.checks))
            QTest.mouseClick(dialog.all_button, Qt.MouseButton.LeftButton)
            self.assertEqual(dialog.selected, {0, 1, 2, 3})
            self.assertTrue(dialog.confirm_button.isEnabled())
            self.assertTrue(all(check.isChecked() for check in dialog.checks))
            self.assertEqual(re.findall(r'\d+', dialog.selection_count.text()), ['4', '4'])
            dialog.search.clear()
            self.app.processEvents()
            self.assertTrue(all(not card.isHidden() for card in dialog.cards))
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_picker_reflows_at_narrow_width_and_each_card_or_checkbox_click_toggles_once(self):
        entries = self.post_entries() * 3
        dialog = self.picker(entries)
        try:
            dialog.resize(540, 700)
            self.app.processEvents()
            QTest.qWait(20)
            self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(), 0)
            self.assertLessEqual(dialog.body.width(), dialog.scroll.viewport().width())
            card = dialog.cards[0]
            dialog.scroll.ensureWidgetVisible(card, 0, 4)
            self.app.processEvents()
            transitions = []
            card.toggled.connect(transitions.append)
            QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(card.width() // 2, 70))
            self.assertEqual(transitions, [False])
            self.assertNotIn(0, dialog.selected)
            self.assertFalse(dialog.checks[0].isChecked())
            QTest.mouseClick(dialog.checks[0], Qt.MouseButton.LeftButton)
            self.assertEqual(transitions, [False, True])
            self.assertIn(0, dialog.selected)
            self.assertTrue(dialog.checks[0].isChecked())
            self.assertEqual(re.findall(r'\d+', dialog.selection_count.text()), ['12', '12'])
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_picker_wrapper_returns_selected_urls_and_cancel_returns_none(self):
        entries = self.post_entries()

        def accept_subset(dialog):
            dialog.checks[0].setChecked(False)
            dialog.checks[3].setChecked(False)
            return QDialog.DialogCode.Accepted

        with patch.object(PostPicker, 'exec', new=accept_subset):
            selected = self.window.choose_titles(entries)
        self.assertEqual(selected, {entries[1]['url'], entries[2]['url']})
        with patch.object(PostPicker, 'exec', new=lambda dialog: QDialog.DialogCode.Rejected):
            self.assertIsNone(self.window.choose_titles(entries))


if __name__ == '__main__':
    unittest.main()
