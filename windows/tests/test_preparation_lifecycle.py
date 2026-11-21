"""Stop/close at discovery-to-queue boundaries must not start transfers."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QSettings
from PySide6.QtWidgets import QApplication
import download_pawchive_images as desktop
from download_journal import DownloadJournal
from queue_operations import QueuePreparationWorker

PROFILE='https://pawchive.pw/fanbox/user/123'


class PreparationLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_preparation_lifecycle_')
        self.root=Path(self.temp.name)
        self.window=desktop.App(QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat))
        self.window.path.setText(str(self.root))
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test'))
        self.network.start()
    def tearDown(self):
        self.window._queue_preparation=None;self.window._manifest_worker=None
        self.window.busy=False;self.window.close();self.window.deleteLater()
        self.app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.network.stop();self.temp.cleanup()
    def test_stop_during_preparation_interrupts_worker_and_blocks_late_result(self):
        window=self.window;worker=Mock();worker.isRunning.return_value=False
        window._queue_preparation=worker;window.set_busy_state(True);window.set_workflow('download')
        with patch.object(window,'_queue_worker_factory') as factory:
            window.stop_download();worker.requestInterruption.assert_called_once()
            window._queue_prepared([dict(url=PROFILE+'/post/1',title='Album')])
            factory.assert_not_called()
        self.assertIsNone(window.queue_scheduler)
        self.assertFalse(window.busy);self.assertEqual(window.workflow_state,'stopped')
    def test_new_manifest_discovery_resets_old_stop_without_stale_restart(self):
        window=self.window;window.stop_requested=True
        with patch('static_download_plan.ManifestFetchWorker.start') as start:
            self.assertTrue(window._start_queue([dict(url=PROFILE+'/post/1')],PROFILE))
            start.assert_called_once();self.assertFalse(window.stop_requested)
            window.stop_download()
            with patch.object(window,'_start_queue') as restart:
                window._manifest_ready([dict(url=PROFILE+'/post/1',assets=[])],PROFILE)
                restart.assert_not_called()
        self.assertEqual(window.workflow_state,'stopped');self.assertFalse(window.busy)
    def test_interrupted_preparation_rolls_back_new_checkpoints(self):
        journal=DownloadJournal(self.root/'journal')
        try:
            entries=[dict(url=PROFILE+'/post/'+str(i),title=str(i),assets=[]) for i in range(10)]
            config=dict(folder=str(self.root/'downloads'),profile=PROFILE,by_title=True)
            worker=QueuePreparationWorker(journal,entries,config)
            original=journal.create;created=[];interrupted=[False]
            worker.isInterruptionRequested=lambda:interrupted[0]
            def interrupt_after_first(*args,**kwargs):
                identifier=original(*args,**kwargs);created.append(identifier)
                interrupted[0]=True;return identifier
            errors=[];worker.failed.connect(errors.append)
            with patch.object(journal,'create',side_effect=interrupt_after_first):worker.run()
            self.assertTrue(errors);self.assertEqual(len(created),1)
            self.assertFalse(journal.list_recoverable())
        finally:journal.close()
