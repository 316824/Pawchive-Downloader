"""Actual modal controls for all four recommendation/download languages."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication,QEvent,QObject,QTimer,Signal
from PySide6.QtWidgets import (QApplication,QCheckBox,QComboBox,QDialog,
    QDialogButtonBox,QLabel,QLineEdit,QPushButton,QWidget)
from download_pawchive_images import GlowCheckBox
from feature_text import LANGUAGES
from ui_i18n import localize_dialog,t
import download_plan
import popular_features as popular


class ManualAssets(QObject):
    result=Signal(object,object,str)
    finished=Signal()
    def __init__(self,*args):
        super().__init__(); self.running=False
    def start(self): self.running=True
    def requestInterruption(self): pass
    def finish(self):
        self.running=False; self.finished.emit()


class RecommendationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='pawchive_plan_ui_')
        self.parent=QWidget()
        self.parent.path=QLineEdit(self.temporary.name,self.parent)
        self.parent.title_check=GlowCheckBox(self.parent)
        self.parent.rename_check=GlowCheckBox(self.parent)
        self.parent.title_check.setChecked(True)
        self.parent.content_combo=QComboBox(self.parent)
        for value in ('all','images','attachments'):self.parent.content_combo.addItem(value,value)
        self.parent.checkbox_factory=GlowCheckBox
        self.parent._dark=False
        self.dialogs=[]
        self.network=patch('requests.sessions.Session.request',
            side_effect=AssertionError('No network in UI localization tests'))
        self.network_mock=self.network.start()

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            for worker in list(getattr(dialog,'_workers',[])):
                worker.finish()
            dialog.deleteLater()
        self.parent.close();self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.network_mock.assert_not_called();self.network.stop()
        self.temporary.cleanup()

    def _plan(self,language,*,post=None,updates_after=None,accept=True):
        self.parent.lang=language
        inspected=[]; failures=[]
        def inspect():
            dialog=self.app.activeModalWidget()
            try:
                self.assertIsInstance(dialog,QDialog)
                self.assertEqual(download_plan._copy(language,'confirm'),dialog.windowTitle())
                checks=dialog.findChildren(QCheckBox)
                self.assertEqual(3,len(checks))
                self.assertTrue(all(isinstance(check,GlowCheckBox) for check in checks))
                self.assertTrue(all(check.property('lightTheme') for check in checks))
                self.assertTrue(checks[0].isChecked())
                self.assertEqual(t(language,'按画师名创建主文件夹'),checks[0].text())
                self.assertTrue(checks[1].isChecked())
                self.assertFalse(checks[2].isChecked())
                buttons=dialog.findChild(QDialogButtonBox)
                localize_dialog(dialog,language) # same pass as the main App Show hook
                self.assertEqual(t(language,'取消'),buttons.button(QDialogButtonBox.StandardButton.Cancel).text())
                okay=buttons.button(QDialogButtonBox.StandardButton.Ok)
                self.assertEqual(download_plan._copy(language,'download' if post else 'scan'),okay.text())
                combo=dialog.findChild(QComboBox)
                self.assertEqual('images' if post else 'all',combo.currentData())
                self.assertEqual(t(language,'图片与附件'),combo.itemText(0))
                self.assertEqual(not bool(post),combo.isEnabled())
                checks[2].setChecked(True)
                inspected.append(True)
                (okay if accept else buttons.button(QDialogButtonBox.StandardButton.Cancel)).click()
            except BaseException as error:
                failures.append(error)
                if dialog:dialog.reject()
        QTimer.singleShot(0,inspect)
        plan=download_plan.ask_download_plan(self.parent,'https://pawchive.pw/fanbox/user/123',
            post=post,updates_after=updates_after)
        if failures:raise failures[0]
        self.assertEqual([True],inspected)
        return plan

    def test_download_plan_real_modal_uses_workspace_checkbox_and_four_language_buttons(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                plan=self._plan(language)
                self.assertTrue(plan['by_title']);self.assertTrue(plan['rename_order'])
                self.assertTrue(plan['artist_folder'])
                self.assertEqual('all',plan['content_mode'])
                self.assertEqual(self.temporary.name,plan['folder'])

    def test_single_post_uses_image_only_plan_and_cancel_returns_without_download(self):
        self.assertEqual('images',self._plan('日本語',post='https://pawchive.pw/fanbox/user/123/post/4')['content_mode'])
        self.assertIsNone(self._plan('简体中文',updates_after=1,accept=False))

    def test_checkbox_class_falls_back_to_the_existing_control_without_entry_import(self):
        del self.parent.checkbox_factory
        self.assertIsNotNone(self._plan('English'))

    def test_empty_folder_cannot_be_confirmed_until_a_folder_is_selected(self):
        self.parent.lang='简体中文';self.parent.path.clear()
        failures=[]
        def inspect():
            dialog=self.app.activeModalWidget()
            try:
                okay=dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
                self.assertFalse(okay.isEnabled())
                dialog.findChild(QLineEdit).setText(self.temporary.name)
                self.assertTrue(okay.isEnabled())
            except BaseException as error:failures.append(error)
            finally:dialog.reject()
        QTimer.singleShot(0,inspect)
        self.assertIsNone(download_plan.ask_download_plan(self.parent,'https://pawchive.pw/fanbox/user/123'))
        if failures:raise failures[0]

    def test_recommendation_labels_status_and_standard_close_are_localized(self):
        for language in LANGUAGES:
            with self.subTest(language=language),patch.object(popular.PopularRecommendationsDialog,'load'):
                dialog=popular.PopularRecommendationsDialog(language=language)
                self.dialogs.append(dialog)
                self.assertEqual(popular._copy(language,'recommendations'),dialog.windowTitle())
                box=dialog.findChild(QDialogButtonBox)
                self.assertEqual(t(language,'关闭'),box.button(QDialogButtonBox.StandardButton.Close).text())
                dialog._populate([dict(title='User-provided title',artist_id='3439325')])
                self.assertEqual(popular._copy(language,'picks',count=1),dialog.status.text())
                tag=dialog.findChild(popular.RecommendationCard).artist_tag
                self.assertEqual(popular._copy(language,'artist',artist_id='3439325'),tag.text())
                dialog._failed('timeout')
                self.assertEqual(popular._copy(language,'interrupted'),dialog.status.text())

    def test_preview_actions_counts_and_attachment_errors_follow_the_selected_language(self):
        for language in LANGUAGES:
            with self.subTest(language=language),patch.object(popular,'PostAssetsWorker',ManualAssets):
                dialog=popular.PostPreviewDialog(None,{'title':'关闭','url':'https://pawchive.pw/fanbox/user/123/post/4'},language=language)
                self.dialogs.append(dialog)
                self.assertEqual('关闭',dialog.windowTitle(),'User content must not be translated')
                self.assertEqual(popular._copy(language,'post'),dialog.download.text())
                self.assertEqual(popular._copy(language,'creator'),dialog.creator.text())
                self.assertEqual(popular._copy(language,'attachments'),dialog.attachments.text())
                dialog._worker.result.emit([],[{'url':'https://file.pawchive.pw/attach','name':'file.zip'}],'')
                dialog._worker.finish()
                self.assertEqual(popular._copy(language,'images',count=0)+popular._copy(language,'attachment_count',count=1),dialog.status.text())
                dialog._attachment_done(1,1,'user-name.zip: incomplete_attachment')
                self.assertNotIn('incomplete_attachment',dialog.status.text())
                self.assertIn('user-name.zip',dialog.status.text())
                self.assertIn(popular._copy(language,'incomplete_attachment'),dialog.status.text())

    def test_attachment_folder_picker_is_non_native_and_uses_the_preview_language(self):
        with patch.object(popular,'PostAssetsWorker',ManualAssets):
            dialog=popular.PostPreviewDialog(None,{'url':'https://pawchive.pw/fanbox/user/123/post/4'},language='English')
        self.dialogs.append(dialog)
        with patch.object(popular.QFileDialog,'getExistingDirectory',return_value='') as choose:
            dialog._start_attachments([])
        self.assertEqual(popular._copy('English','attachment_folder'),choose.call_args.args[1])
        self.assertTrue(choose.call_args.kwargs['options'] & popular.QFileDialog.Option.DontUseNativeDialog)

    def test_closing_preview_cancels_owned_lazy_timer_and_ignores_late_callbacks(self):
        with patch.object(popular,'PostAssetsWorker',ManualAssets):
            dialog=popular.PostPreviewDialog(None,{'url':'https://pawchive.pw/fanbox/user/123/post/4'})
        self.dialogs.append(dialog)
        dialog._worker.result.emit([],[],'');dialog._worker.finish()
        self.assertIs(dialog,dialog._visible_timer.parent())
        self.assertTrue(dialog._visible_timer.isActive())
        dialog.reject()
        self.assertFalse(dialog._visible_timer.isActive())
        with patch.object(dialog,'_pump_images') as pump:
            dialog._assets_ready([{'url':'https://file.pawchive.pw/late'}],[],'')
            dialog._load_visible()
            pump.assert_not_called()
        self.assertEqual(0,dialog.list.count())


if __name__=='__main__':unittest.main()
