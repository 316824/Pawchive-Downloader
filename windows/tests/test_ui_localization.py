"""Real four-language widgets, without translating downloaded/user content."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings,QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QDialog,QDialogButtonBox,QMessageBox
from ui_i18n import COPY,t,install_qt_language,localize_dialog,language_for
import download_pawchive_images as desktop
from background_store import BackgroundStore
from background_ui import BackgroundEditorDialog,BackgroundSettingsDialog
from artist_migration import ArtistMigrationDialog
from app_state import AppState


class UiLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_i18n_test_')
        self.root=Path(self.temp.name)
        self.window=desktop.App(QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat))

    def tearDown(self):
        self.window.busy=False
        self.window.close(); self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.qt.processEvents()
        self.temp.cleanup()
        install_qt_language('简体中文')

    def test_all_shared_copy_has_three_complete_translations(self):
        for source,translations in COPY.items():
            self.assertEqual(len(translations),3,source)
            self.assertTrue(all(isinstance(text,str) and text for text in translations),source)

    def test_qt_translator_and_explicit_buttons_follow_app_language(self):
        for language,expected in [('简体中文','取消'),('繁體中文','取消'),('English','Cancel'),('日本語','キャンセル')]:
            self.window.set_lang(language)
            box=QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel,self.window)
            self.assertEqual(box.button(QDialogButtonBox.StandardButton.Cancel).text().replace('&',''),expected)
            dialog=QDialog(self.window)
            message=QMessageBox(dialog)
            message.setStandardButtons(QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No)
            localize_dialog(message,language)
            self.assertEqual(message.button(QMessageBox.StandardButton.No).text(),t(language,'否'))
            dialog.deleteLater();box.deleteLater()

    def test_custom_download_button_caption_is_not_replaced_by_ok(self):
        dialog=QDialog(self.window)
        box=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel,dialog)
        button=box.button(QDialogButtonBox.StandardButton.Ok)
        button.setText('Start scan');button.setProperty('pawchiveCustomCaption',True)
        localize_dialog(dialog,'English')
        self.assertEqual(button.text(),'Start scan')
        self.assertEqual(box.button(QDialogButtonBox.StandardButton.Cancel).text(),'Cancel')
        dialog.deleteLater()

    def test_main_controls_and_nested_dialogs_use_selected_language(self):
        for language in ('English','繁體中文','日本語','简体中文'):
            self.window.set_lang(language)
            self.assertEqual(self.window.content_combo.itemText(1),t(language,'仅图片'))
            self.assertEqual(self.window.queue_pause_all_button.text(),t(language,'全部暂停'))
            editor=BackgroundEditorDialog(self.window,BackgroundStore(self.root/'backgrounds'))
            self.assertEqual(editor.cancel_button.text(),t(language,'取消'))
            self.assertEqual(language_for(editor),language)
            settings=BackgroundSettingsDialog(self.window,BackgroundStore(self.root/'backgrounds'),language=language)
            self.assertEqual(settings.windowTitle(),t(language,'背景与界面主题'))
            self.assertEqual(settings.page_combo.itemText(settings.page_combo.findData('subscriptions')),t(language,'画师更新订阅'))
            editor.close();editor.deleteLater();settings.close();settings.deleteLater()

    def test_migration_feedback_is_localized_and_plain_code_is_preserved(self):
        self.window.set_lang('English')
        store=AppState(self.root/'state')
        try:
            dialog=ArtistMigrationDialog(self.window,store)
            self.assertEqual(dialog.tabs.tabText(1),'Restore')
            code=dialog.output.toPlainText()
            self.assertTrue(code.startswith('PAWCHIVE_V1:'))
            dialog.copy_code();self.assertEqual(dialog.copy_button.text(),'Copied')
            self.assertEqual(QApplication.clipboard().text(),code)
            dialog.input.setPlainText('invalid');dialog.restore_code()
            self.assertEqual(dialog.status.text(),t('English','无效迁移代码，请完整复制后重试。'))
            dialog.close();dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        finally:store.close()

    def test_background_validation_errors_follow_selected_language(self):
        source='Choose an image file, not a folder.'
        for language in ('简体中文','繁體中文','English','日本語'):
            self.window.set_lang(language)
            editor=BackgroundEditorDialog(self.window,BackgroundStore(self.root/'backgrounds'))
            with patch('background_ui.QFileDialog.getOpenFileName',return_value=(str(self.root),'')), \
                 patch('background_ui.QMessageBox.warning') as warning:
                editor.choose_source()
            warning.assert_called_once()
            self.assertEqual(warning.call_args.args[2],t(language,source))
            editor.close();editor.deleteLater()
