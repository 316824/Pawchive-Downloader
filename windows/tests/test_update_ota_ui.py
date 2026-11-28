"""Desktop release tag compatibility, mandatory gates and nonblocking manual checks."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QSettings,Qt,Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton,QWidget
import update_checker as updates


def payload(tag='Windows-v1.3.2',body='## Fixes\n\n- Download fixes',**extra):
    result=dict(tag_name=tag,name='Pawchive '+tag,body=body,
        html_url=updates.RELEASE_ROOT+tag,draft=False,prerelease=False,assets=[])
    result.update(extra);return result


class FakeWorker(QObject):
    result=Signal(object);finished=Signal()
    instances=[]
    def __init__(self):
        super().__init__();self.cancelled=False;self.started=False;self.instances.append(self)
    def start(self):self.started=True
    def requestInterruption(self):self.cancelled=True


class ReleasePolicyTests(unittest.TestCase):
    def test_real_windows_tag_is_versioned_and_android_tags_excluded(self):
        release=updates.parse_release(payload('Windows-v1.3.1'))
        self.assertEqual(release.version,updates.parse_version('v1.3.1'))
        self.assertEqual(release.tag,'Windows-v1.3.1')
        self.assertTrue(release.url.endswith('/Windows-v1.3.1'))
        self.assertIsNone(updates.parse_release(payload('Android-v1.9.0')))
        self.assertIsNone(updates.parse_version('not-Windows-v1.3.1'))

    def test_patch_hot_minor_and_major_mandatory_with_body_override(self):
        current=updates.parse_version('Windows-v1.3')
        self.assertFalse(updates.requires_update(updates.parse_release(payload('Windows-v1.3.1')),current))
        for tag in ('v1.4','Windows-v1.4.0','v2.0.0'):
            self.assertTrue(updates.requires_update(updates.parse_release(payload(tag)),current))
        self.assertTrue(updates.requires_update(updates.parse_release(payload(body='[MUST_UPDATE] fix')),current))

    def test_marker_after_display_truncation_still_enforces_update(self):
        body='x'*(updates.MAX_BODY_CHARS+20)+'[MUST_UPDATE]'
        release=updates.parse_release(payload(body=body))
        self.assertNotIn('[MUST_UPDATE]',release.body)
        self.assertTrue(updates.requires_update(release,updates.parse_version('1.3.1')))

    def test_only_exact_official_release_assets_enter_payload(self):
        tag='Windows-v1.3.2';name='PawchiveDownloader-Windows-x64.exe'
        valid=dict(name=name,browser_download_url='https://github.com/316824/Pawchive-Downloader/releases/download/'+tag+'/'+name,
                   size=120,digest='sha256:'+'a'*64)
        release=updates.parse_release(payload(tag,assets=[valid,{**valid,'browser_download_url':'https://evil.test/update.exe'},
            {**valid,'name':'../evil.exe'},{**valid,'size':True},{**valid,'size':-1}]))
        self.assertEqual(len(release.assets),1)
        self.assertEqual(release.assets[0].digest,'sha256:'+'a'*64)
        self.assertTrue(release.assets[0].url.endswith('/'+name))
        self.assertIsNone(updates.safe_asset_url(valid['browser_download_url']+'?x=1',tag,name))
        self.assertIsNone(updates.safe_asset_url(valid['browser_download_url'], 'v1.3.1',name))


class UpdateUiFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_ota_ui_')
        self.window=QWidget();self.window.resize(760,550);self.window.lang='简体中文'
        self.window.settings=QSettings(str(Path(self.temp.name)/'preferences.ini'),QSettings.Format.IniFormat)
        self.gates=[];self.exits=[]
        self.window.set_update_required=self.gates.append
        self.window.request_update_exit=lambda:self.exits.append(True)
        self.footer=QPushButton(self.window);self.manual=QPushButton(self.window)
        self.controller=updates.install_update_checker(self.window,self.footer,current_version='v1.3.1',manual_button=self.manual,delay_ms=None)
        self.window.show();self.qt.processEvents();FakeWorker.instances=[]
    def tearDown(self):
        self.controller.close();self.window.close();self.window.deleteLater()
        self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.temp.cleanup()
    def check(self,release):
        self.controller.manual_check();worker=FakeWorker.instances[-1]
        worker.result.emit(release);worker.finished.emit();self.qt.processEvents()


class UpdateDualTrackUiTests(UpdateUiFixture):
    def test_manual_no_new_version_toast_does_not_claim_network_error_is_latest(self):
        with patch.object(updates,'ReleaseCheckWorker',FakeWorker):
            self.check(updates.parse_release(payload('v1.3.1')))
            self.assertIn('当前已是最新版本',self.controller._toast.text())
            self.assertIsNone(self.controller._dialog)
            self.check(None)
            self.assertIn('暂时无法检查更新',self.controller._toast.text())
            self.assertTrue(self.manual.isEnabled())
            self.assertEqual(self.manual.text(),'检查更新')
    def test_concurrent_auto_manual_checks_never_start_second_request(self):
        with patch.object(updates,'ReleaseCheckWorker',FakeWorker):
            self.controller.start();self.qt.processEvents()
            self.assertFalse(self.manual.isEnabled());self.assertEqual(self.manual.text(),'正在检查...')
            self.controller.manual_check();self.controller.start()
            self.assertEqual(len(FakeWorker.instances),1)
            FakeWorker.instances[0].result.emit(None);FakeWorker.instances[0].finished.emit();self.qt.processEvents()
            self.assertIsNone(self.controller._toast)
            self.assertTrue(self.controller._retry_timer.isActive())
            self.controller.manual_check();self.assertEqual(len(FakeWorker.instances),2)
            self.assertFalse(self.controller._retry_timer.isActive())
    def test_background_hot_update_only_shows_red_dot(self):
        self.controller._received(updates.parse_release(payload()))
        self.assertTrue(self.footer.isVisible());self.assertIsNone(self.controller._dialog)
        self.assertFalse(self.gates)
    def test_new_release_replaces_old_notes_before_update_action(self):
        previous=updates.parse_release(payload())
        self.controller._received(previous);self.controller.show_dialog()
        old_dialog=self.controller._dialog
        latest=updates.parse_release(payload('v1.3.3',body='Latest notes'))
        self.controller._manual=True;self.controller._received(latest)
        self.assertIsNot(self.controller._dialog,old_dialog)
        self.assertEqual(self.controller._dialog.release,latest)
        self.assertIn('Latest notes',self.controller._dialog.notes.toPlainText())
    def test_thread_start_failure_releases_manual_check_button_and_reports_failure(self):
        class FailedWorker(FakeWorker):
            def start(self):raise RuntimeError('No more threads')
        with patch.object(updates,'ReleaseCheckWorker',FailedWorker):self.controller.manual_check()
        self.assertIsNone(self.controller.worker);self.assertFalse(self.controller._checking)
        self.assertTrue(self.manual.isEnabled());self.assertIn('暂时无法检查更新',self.controller._toast.text())
    def test_toast_and_failure_fallback_follow_theme_and_official_release(self):
        self.window.palette_tokens=dict(card='#ffffff',field='#fafafa',text='#123456',glow='#654321')
        self.controller.toast('Latest')
        self.assertIn('background:#ffffff',self.controller._toast.styleSheet())
        release=updates.parse_release(payload());self.controller._received(release);self.controller.show_dialog()
        dialog=self.controller._dialog
        dialog.download_button.click()
        self.assertFalse(dialog.fallback_link.isHidden())
        with patch.object(updates.webbrowser,'open') as browser:
            dialog.fallback_link.linkActivated.emit('https://evil.test')
            browser.assert_called_once_with(release.url)
    def test_mandatory_modal_reject_escape_caption_and_close_cannot_bypass_gate(self):
        self.controller._received(updates.parse_release(payload('Windows-v1.4')));self.qt.processEvents()
        dialog=self.controller._dialog
        self.assertTrue(dialog.mandatory);self.assertEqual(self.gates,[True])
        self.assertEqual(dialog.windowModality(),Qt.WindowModality.ApplicationModal)
        self.assertIsNone(dialog.ignore_button);self.assertIsNone(dialog.later_button)
        self.assertTrue(dialog._pawchive_window_chrome.close.isHidden())
        dialog.reject();dialog.accept();dialog.close();QTest.keyClick(dialog,Qt.Key.Key_Escape)
        self.qt.processEvents();self.assertTrue(dialog.isVisible())
        with patch.object(updates.webbrowser,'open') as browser:
            dialog.download_button.click();browser.assert_called_once_with(dialog.release.url)
        self.assertTrue(dialog.isVisible());dialog.exit_button.click();self.assertEqual(self.exits,[True])
    def test_ignore_uses_qsettings_and_manual_check_can_show_ignored_hot_version(self):
        release=updates.parse_release(payload())
        self.controller._received(release);self.controller.ignore_version(release.tag)
        self.assertEqual(self.window.settings.value(updates.IGNORED_VERSION_KEY),'1.3.2')
        self.assertFalse(self.footer.isVisible())
        other=updates.UpdateController(self.window,current_version='v1.3.1')
        other._received(release);self.assertIsNone(other.release)
        other.close()
        with patch.object(updates,'ReleaseCheckWorker',FakeWorker):self.check(release)
        self.assertIsNotNone(self.controller._dialog)
        self.assertEqual(self.controller._dialog.download_button.text(),'立即更新')
    def test_ignored_version_never_suppresses_forced_release(self):
        self.controller._ignored='1.3.2'
        self.controller._received(updates.parse_release(payload(body='[MUST_UPDATE]')))
        self.assertTrue(self.controller._dialog.mandatory)
        self.controller.ignore_version('1.3.2');self.assertIsNotNone(self.controller.release)
    def test_higher_version_after_ignore_has_notification(self):
        self.controller._received(updates.parse_release(payload()));self.controller.ignore_version('1.3.2')
        self.controller._received(updates.parse_release(payload('v1.3.3')))
        self.assertTrue(self.footer.isVisible());self.assertIn('v1.3.3',self.footer.text())
    def test_source_mode_never_starts_installer(self):
        self.controller._received(updates.parse_release(payload()));self.controller.show_dialog()
        self.controller._dialog.download_button.click()
        self.assertIsNone(self.controller._ota_worker)
        self.assertIn('脚本运行模式',self.controller._dialog.status.text())
    def test_manual_and_hot_actions_translate_all_supported_languages(self):
        for language in ('简体中文','繁體中文','English','日本語'):
            self.window.lang=language;self.controller.refresh_language()
            self.assertEqual(self.manual.text(),updates.t(language,'检查更新'))
            dialog=updates.UpdateDialog(self.window,updates.parse_release(payload()))
            self.assertEqual(dialog.download_button.text(),updates.t(language,'立即更新'))
            dialog.deleteLater()
        self.window.lang='English'
        release=updates.parse_release(payload(body='x'*(updates.MAX_BODY_CHARS+1)))
        dialog=updates.UpdateDialog(self.window,release)
        self.assertIn('Read the full notes',dialog.notes.toPlainText())
        self.assertNotIn('完整内容请',dialog.notes.toPlainText())
        dialog.deleteLater()


class OtaSignalUiTests(UpdateUiFixture):
    def start_download(self):
        import ota_update
        class OtaWorker(QObject):
            progress=Signal(object,object);ready=Signal(object);failed=Signal(str);finished=Signal()
            instances=[]
            def __init__(self,release):
                super().__init__();self.release=release;self.cancelled=False;self.started=False;self.instances.append(self)
            def start(self):self.started=True
            def requestInterruption(self):self.cancelled=True
        self.ota_patch=patch.object(ota_update,'OtaDownloadWorker',OtaWorker);self.ota_patch.start()
        self.addCleanup(self.ota_patch.stop)
        self.controller._received(updates.parse_release(payload()));self.controller.show_dialog()
        with patch.object(updates.sys,'frozen',True,create=True):self.controller._dialog.download_button.click()
        return OtaWorker.instances[-1]
    def flush(self):self.qt.processEvents()
    def test_progress_uses_bounded_integer_range_for_multi_gigabyte_count(self):
        worker=self.start_download();worker.progress.emit(2**33,2**34);self.flush()
        dialog=self.controller._dialog
        self.assertEqual(dialog.progress.value(),500);self.assertFalse(dialog.download_button.isEnabled())
        self.assertIn('正在下载更新',dialog.status.text())
        worker.failed.emit('Read timed out');worker.finished.emit();self.flush()
        self.assertIn('Read timed out',dialog.status.text());self.assertTrue(dialog.download_button.isEnabled())
    def test_cancelled_dialog_drops_late_ready_and_cleans_only_its_staged_update(self):
        from types import SimpleNamespace
        import ota_update
        worker=self.start_download();self.controller._dialog.reject()
        self.assertTrue(worker.cancelled)
        staged=SimpleNamespace(root=Path(self.temp.name),exe=Path(self.temp.name)/'verified.exe',version='v1.3.2')
        with patch.object(ota_update,'cleanup_staged') as cleanup:
            worker.ready.emit(staged);worker.finished.emit();self.flush()
            cleanup.assert_called_once_with(staged)
        self.assertIsNone(self.controller._ota_worker)
    def test_successful_handoff_preserves_stage_during_final_close(self):
        from types import SimpleNamespace
        import ota_update
        worker=self.start_download();calls=[]
        self.window.request_update_install=lambda stage:calls.append(stage) or True
        stage=SimpleNamespace(root=Path(self.temp.name),exe=Path(self.temp.name)/'verified.exe',version='v1.3.2')
        with patch.object(ota_update,'cleanup_staged') as cleanup:
            worker.ready.emit(stage);self.flush()
            self.assertEqual(calls,[stage]);self.assertTrue(self.controller._install_pending)
            self.assertFalse(self.controller._dialog.later_button.isEnabled())
            self.controller.close();worker.finished.emit();self.flush();cleanup.assert_not_called()
    def test_rejected_handoff_cleans_stage_and_reenables_update(self):
        from types import SimpleNamespace
        import ota_update
        worker=self.start_download();self.window.request_update_install=lambda _:False
        stage=SimpleNamespace(root=Path(self.temp.name),exe=Path(self.temp.name)/'verified.exe',version='v1.3.2')
        with patch.object(ota_update,'cleanup_staged') as cleanup:
            worker.ready.emit(stage);worker.finished.emit();self.flush();cleanup.assert_called_once_with(stage)
        self.assertFalse(self.controller._install_pending)
        self.assertTrue(self.controller._dialog.download_button.isEnabled())
    def test_install_launch_failure_can_retry_same_verified_stage_without_downloading(self):
        from types import SimpleNamespace
        import ota_update
        worker=self.start_download();calls=[]
        self.window.request_update_install=lambda stage:calls.append(stage) or True
        stage=SimpleNamespace(root=Path(self.temp.name),exe=Path(self.temp.name)/'verified.exe',version='v1.3.2')
        with patch.object(ota_update,'cleanup_staged') as cleanup:
            worker.ready.emit(stage);worker.finished.emit();self.flush()
            self.controller.install_failed('No permission')
            self.assertIn('No permission',self.controller._dialog.status.text())
            with patch.object(updates.sys,'frozen',True,create=True):self.controller._dialog.download_button.click()
            self.assertEqual(calls,[stage,stage]);cleanup.assert_not_called()
    def test_update_download_blocks_manual_check_and_late_metadata_replacement(self):
        worker=self.start_download();release=self.controller.release
        with patch.object(updates,'ReleaseCheckWorker',FakeWorker):self.controller.manual_check()
        self.assertFalse(self.manual.isEnabled());self.assertEqual(FakeWorker.instances,[])
        self.controller._received(updates.parse_release(payload('v1.3.3')))
        self.assertEqual(self.controller.release,release)
        worker.failed.emit('cancelled');worker.finished.emit();self.flush()
        self.assertTrue(self.manual.isEnabled())
    def test_cancel_reopen_waits_for_old_worker_and_drops_its_ready_signal(self):
        from types import SimpleNamespace
        import ota_update
        worker=self.start_download();self.controller._dialog.reject();self.controller.show_dialog()
        current=self.controller._dialog
        self.assertFalse(current.download_button.isEnabled());self.assertTrue(worker.cancelled)
        staged=SimpleNamespace(root=Path(self.temp.name),version='v1.3.2')
        with patch.object(ota_update,'cleanup_staged') as cleanup:
            worker.ready.emit(staged);worker.finished.emit();self.flush()
            cleanup.assert_called_once_with(staged)
        self.assertTrue(current.download_button.isEnabled());self.assertFalse(self.controller._install_pending)
    def test_wrong_version_stage_never_reaches_install_hook(self):
        from types import SimpleNamespace
        import ota_update
        worker=self.start_download();calls=[]
        self.window.request_update_install=calls.append
        staged=SimpleNamespace(root=Path(self.temp.name),version='v1.3.3')
        with patch.object(ota_update,'cleanup_staged') as cleanup:
            worker.ready.emit(staged);worker.finished.emit();self.flush()
            cleanup.assert_called_once_with(staged)
        self.assertFalse(calls);self.assertFalse(self.controller._install_pending)
        self.assertIn('版本已变化',self.controller._dialog.status.text())
    def test_failed_install_stage_discarded_on_ignore_or_newer_release(self):
        from types import SimpleNamespace
        import ota_update
        for ignore in (True,False):
            self.controller._ignored=''
            worker=self.start_download();self.window.request_update_install=lambda _:True
            staged=SimpleNamespace(root=Path(self.temp.name),version='v1.3.2')
            with patch.object(ota_update,'cleanup_staged') as cleanup:
                worker.ready.emit(staged);worker.finished.emit();self.flush()
                self.controller.install_failed('Permission denied')
                if ignore:self.controller._dialog.ignore_button.click()
                else:self.controller._received(updates.parse_release(payload('v1.3.3')))
                cleanup.assert_called_once_with(staged)
            self.assertIsNone(self.controller._staged_update)
            if self.controller._dialog is not None:self.controller._dialog.force_close()
    def test_cancel_cleanup_error_is_not_raised_from_qt_ready_slot(self):
        from types import SimpleNamespace
        import ota_update
        worker=self.start_download();self.controller._dialog.reject()
        staged=SimpleNamespace(root=Path(self.temp.name),version='v1.3.2')
        with patch.object(ota_update,'cleanup_staged',side_effect=PermissionError('busy file')):
            self.controller._update_ready(staged)
        worker.finished.emit();self.flush()
        self.assertIsNone(self.controller._ota_worker)


if __name__=='__main__':unittest.main()
