"""Update handoff must preserve download checkpoints and wait for native threads."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import sys,tempfile,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QSettings,QThread,QTimer
from PySide6.QtWidgets import QApplication
from download_scheduler import DownloadScheduler
from ota_update import StagedUpdate
import download_pawchive_images as desktop

class UpdateShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_update_shutdown_')
        self.root=Path(self.temp.name)
        self.net=patch('requests.sessions.Session.request',side_effect=AssertionError('offline'));self.net.start()
        self.window=desktop.App(QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat))
        self.window.path.setText(str(self.root));self.window.show();self.qt.processEvents()
    def tearDown(self):
        self.window.services=None;self.window.busy=False;self.window._staged_update=None
        self.window.close();self.window.deleteLater();self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.net.stop();self.temp.cleanup()
    def test_required_gate_pauses_queue_without_changing_download_settings(self):
        scheduler=DownloadScheduler(lambda _:None,self.window)
        scheduler.add_task(dict(id='one',title='One',url='https://pawchive.pw/fanbox/user/1/post/2',formats=['.png']))
        self.window.queue_scheduler=scheduler
        original=(self.window.path.text(),self.window.title_check.isChecked(),self.window.rename_check.isChecked())
        self.window.set_update_required(True)
        self.assertEqual(scheduler.task('one').state,'paused')
        with patch.object(desktop,'ProfileScanWorker') as scan:
            self.assertFalse(self.window.download())
            self.assertFalse(self.window._start_queue([dict(url='x')],'x'))
            self.assertFalse(self.window.start_batch_artists(['1']))
            self.assertFalse(self.window.resume_recoverable_jobs([{}]))
            scan.assert_not_called()
        self.assertEqual(original,(self.window.path.text(),self.window.title_check.isChecked(),self.window.rename_check.isChecked()))
    def test_source_launch_does_not_replace_python(self):
        staged=StagedUpdate(self.root,self.root/'new.exe','v1.4.1')
        with patch.object(sys,'frozen',False,create=True),patch.object(self.window,'_prepare_update_shutdown') as shutdown:
            self.assertFalse(self.window.request_update_install(staged));shutdown.assert_not_called()
        self.assertIsNone(self.window._staged_update)
    def test_mandatory_exit_during_batched_queue_insertion_finishes_closing(self):
        scheduler=DownloadScheduler(lambda _:None,self.window)
        self.window.queue_scheduler=scheduler
        self.window._queue_pending_entries=[dict(id='pending',title='Pending',
            url='https://pawchive.pw/fanbox/user/1/post/2')]
        self.window.set_busy_state(True)
        self.window._queue_insert_timer.start(0)
        self.window.set_update_required(True)
        self.window.request_update_exit()
        deadline=time.monotonic()+3
        while self.window.isVisible() and time.monotonic()<deadline:
            self.qt.processEvents();time.sleep(.002)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window._queue_pending_entries)
        self.assertFalse(scheduler._workers)
    def test_native_worker_finishes_before_updater_handoff(self):
        class Transfer(QThread):
            def run(self):
                while not self.isInterruptionRequested():self.msleep(1)
        worker=Transfer(self.window);self.window.worker=worker;worker.start()
        self.window.busy=True;exits=[]
        runtime=SimpleNamespace(timer=QTimer(),clipboard_timer=QTimer(),exit_requested=False,
            task_finished=lambda *a,**k:None,request_close=lambda:True,finish_exit=lambda:exits.append(True))
        runtime.prepare_exit=lambda:setattr(runtime,'exit_requested',True)
        self.window.services=runtime
        staged=StagedUpdate(self.root,self.root/'new.exe','v1.4.1')
        def launch(*args):
            self.assertFalse(worker.isRunning());self.assertTrue(worker.preserve_on_interrupt)
            return SimpleNamespace(pid=123)
        with patch.object(sys,'frozen',True,create=True),patch('ota_update.launch_updater',side_effect=launch) as updater:
            self.assertTrue(self.window.request_update_install(staged))
            updater.assert_not_called()
            self.assertTrue(worker.wait(3000));self.window.busy=False;self.window.close()
            updater.assert_called_once();self.assertEqual(exits,[True])
        self.assertEqual(self.window.path.text(),str(self.root))
    def test_launch_failure_reopens_controls_and_retains_verified_stage_for_retry(self):
        failures=[];self.window._staged_update=StagedUpdate(self.root,self.root/'new.exe','v1.4.1')
        self.window._update_installing=True;self.window._close_when_worker_stops=True
        with patch('ota_update.launch_updater',side_effect=PermissionError('folder locked')), \
             patch.object(self.window.update_controller,'install_failed',side_effect=failures.append):
            self.assertFalse(self.window._launch_pending_update())
        self.assertEqual(failures,['folder locked']);self.assertFalse(self.window._update_installing)
        self.assertFalse(self.window._close_when_worker_stops);self.assertFalse(self.window.update_controller._closed)

if __name__=='__main__':unittest.main()
