"""Main input integration stays offline and keeps one artist queue intact."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QSettings
from PySide6.QtWidgets import QApplication,QWidget,QPushButton
import download_pawchive_images as desktop

FANBOX='https://pawchive.pw/fanbox/user/123'
PATREON='https://pawchive.pw/patreon/user/456'


class MainArtistInputsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='pawchive_main_input_')
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test'))
        self.request=self.network.start()
        settings=QSettings(str(Path(self.temporary.name)/'settings.ini'),QSettings.Format.IniFormat)
        self.window=desktop.App(settings=settings)
        self.window.path.setText(self.temporary.name)

    def tearDown(self):
        self.window.cancel_batch_artists()
        self.window._artist_lookup_operation=None
        self.window.queue_scheduler=None
        if hasattr(self.window,'scanner'):del self.window.scanner
        self.window.busy=False
        self.window.close();self.window.deleteLater();self.app.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.request.assert_not_called();self.network.stop();self.temporary.cleanup()

    def test_numeric_id_validation_and_paste_do_not_guess_a_platform(self):
        self.app.clipboard().setText('123')
        self.window.paste_profile()
        self.assertEqual(self.window.profile.text(),'123')
        self.assertTrue(self.window.start.isEnabled())
        self.assertIn('Fanbox',self.window.profile_hint.text())
        self.assertIn('Patreon',self.window.profile_hint.text())

    def test_lookup_finished_does_not_clear_the_scan_busy_state(self):
        callbacks={}
        def resolve(parent,value,on_selected,on_finished):
            self.assertEqual(value,'123')
            callbacks.update(selected=on_selected,finished=on_finished)
            return Mock()
        self.window.profile.setText('123')
        with patch('artist_lookup.resolve_artist_input',side_effect=resolve):self.window.download()
        self.assertTrue(self.window.busy)
        def begin_scan():
            self.window.set_busy_state(True);self.window.set_workflow('scan')
        with patch.object(self.window,'download',side_effect=begin_scan) as download:
            callbacks['selected'](FANBOX);callbacks['finished'](FANBOX)
            download.assert_called_once()
        self.assertTrue(self.window.busy);self.assertEqual(self.window.workflow_state,'scan')
        self.assertEqual(self.window.profile.text(),FANBOX)

    def test_close_generation_suppresses_a_late_lookup_selection(self):
        callbacks={}
        with patch('artist_lookup.resolve_artist_input',side_effect=lambda p,v,s,f:(callbacks.update(selected=s,finished=f) or Mock())):
            self.window._lookup_artist_input('123')
        self.window._artist_lookup_generation+=1
        with patch.object(self.window,'download') as download:
            callbacks['selected'](FANBOX);callbacks['finished'](FANBOX)
        download.assert_not_called()

    def test_unchecked_preselection_still_starts_manifest_scanner(self):
        class Splash(QWidget):
            def __init__(self,parent):
                super().__init__(parent)
                self.pause_button=QPushButton(self);self.resume_button=QPushButton(self);self.finish_button=QPushButton(self)
            def set_language(self,language):pass
            def update_counts(self,*args):pass
        scanner=Mock();scanner.isRunning.return_value=False
        self.window.profile.setText(FANBOX);self.window.select_posts_check.setChecked(False)
        with patch.object(desktop,'ScanAnimation',Splash),patch.object(desktop,'ProfileScanWorker',return_value=scanner) as create,patch.object(self.window,'launch_worker') as launch:
            self.window.download()
        create.assert_called_once();scanner.start.assert_called_once();launch.assert_not_called()
        self.assertTrue(self.window.busy)

    def test_batch_uses_one_snapshot_and_waits_for_failed_paused_and_live_tasks(self):
        self.window.select_posts_check.setChecked(False)
        self.assertTrue(self.window.start_batch_artists([FANBOX,PATREON]))
        self.assertIs(self.window._batch_artist_timer.parent(),self.window)
        with patch.object(self.window,'download') as download:
            self.window._advance_batch_artists()
            download.assert_called_once();self.assertEqual(self.window.profile.text(),FANBOX)
            self.window.path.setText('changed');self.window.select_posts_check.setChecked(True)
            scheduler=SimpleNamespace(tasks=[SimpleNamespace(state='failed')],active_tasks=[],_workers={})
            self.window.queue_scheduler=scheduler
            self.window._advance_batch_artists();self.assertEqual(download.call_count,1)
            scheduler.tasks[0].state='paused'
            self.window._advance_batch_artists();self.assertEqual(download.call_count,1)
            scheduler.tasks[0].state='completed';scheduler._workers={'retiring':object()}
            self.window._advance_batch_artists();self.assertEqual(download.call_count,1)
            scheduler._workers={}
            self.window._advance_batch_artists();self.assertEqual(download.call_count,2)
            self.assertEqual(self.window.profile.text(),PATREON)
            self.assertEqual(self.window.path.text(),self.temporary.name)
            self.assertFalse(self.window.select_posts_check.isChecked())
            self.window._advance_batch_artists();self.assertFalse(self.window._batch_artist_timer.isActive())

    def test_unread_profile_opens_update_preview_other_profiles_use_plan(self):
        self.window.subscription_store=Mock()
        self.window.subscription_store.get.return_value={'unread':True,'baseline_ts':123}
        with patch('subscription_preview.open_update_preview',return_value=True) as preview,patch.object(self.window,'confirm_artist_download') as confirm:
            self.assertTrue(self.window.open_artist_profile(FANBOX))
            preview.assert_called_once_with(self.window,FANBOX);confirm.assert_not_called()
            self.window.subscription_store.get.return_value={'unread':False}
            self.window.open_artist_profile(PATREON);confirm.assert_called_once_with(PATREON)

    def test_archive_count_and_red_badge_have_separate_unclipped_geometry(self):
        button=self.window.artist_cards_button
        button.set_archive_state(12345,True)
        self.assertEqual(button.text(),'✦ 12345')
        self.assertGreaterEqual(button.width(),button.fontMetrics().horizontalAdvance(button.text())+32)
        self.assertTrue(button.rect().contains(button.unread_badge.geometry()))
        self.assertIn('#f05d75',button.unread_badge.styleSheet())
        button.set_archive_state(12345,True,'◐')
        self.assertIn('12345',button.text());self.assertFalse(button.unread_badge.isHidden())
