"""OTA shutdown consumes the production download/journal path without changing saved plans."""
import ctypes,hashlib,os,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from PySide6.QtWidgets import QApplication
from ota_update import StagedUpdate
import download_pawchive_images as desktop
from test_artist_workflow_soak import OfflineSite,WorkflowHarness,PNG

class UpdateDownloadCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)
        if os.name=='nt':ctypes.windll.kernel32.SetErrorMode(0x8003)
    def test_update_during_partial_transfer_preserves_targets_settings_and_resumes(self):
        for by_title in (False,True):
            with self.subTest(by_title=by_title),tempfile.TemporaryDirectory(prefix='pawchive_update_download_') as folder:
                site=OfflineSite()
                original_wait=desktop.Worker.wait_interruptibly
                def fast_wait(worker,seconds):return original_wait(worker,min(seconds,.001))
                with patch('requests.Session',side_effect=site.session), \
                     patch('requests.sessions.Session.request',side_effect=AssertionError('No real network')), \
                     patch.object(desktop.Worker,'wait_interruptibly',fast_wait):
                    harness=WorkflowHarness(folder,self.qt,site)
                    try:
                        harness.window.show();self.qt.processEvents()
                        harness.window.title_check.setChecked(by_title)
                        harness.window.content_combo.setCurrentIndex(harness.window.content_combo.findData('images'))
                        profile=site.add(999999,1,2)
                        site.pause_url=site.file_url('999999',0,0)
                        site.pause_gate=(threading.Event(),threading.Event())
                        harness.begin_artist(profile)
                        harness.pump_until(site.pause_gate[0].is_set,label='partial before OTA')
                        task=harness.window.queue_scheduler.tasks[0]
                        job=harness.journal.load(task.metadata['job_id'])
                        plan=job['config']['static_plan']
                        self.assertEqual([asset['save_name'] for asset in plan],['1.png','2.png'])
                        self.assertEqual(job['config']['by_title'],by_title)
                        target_folder=Path(job['config']['folder'])
                        unrelated=target_folder/'existing-user-file.txt';unrelated.write_bytes(b'preserve existing data')
                        staged=StagedUpdate(Path(folder),Path(folder)/'never-executed.exe','v1.3.3')
                        def handoff(*args):
                            self.assertFalse(harness.window._workspace_has_live_threads())
                            persisted=harness.journal.load(job['id'])
                            self.assertEqual(persisted['status'],'paused')
                            self.assertEqual(persisted['config']['static_plan'],plan)
                            return SimpleNamespace(pid=123)
                        with patch.object(sys,'frozen',True,create=True),patch('ota_update.launch_updater',side_effect=handoff) as launch:
                            self.assertTrue(harness.window.request_update_install(staged))
                            launch.assert_not_called()
                            site.pause_gate[1].set()
                            harness.pump_until(lambda:launch.called and not harness.window.isVisible(),label='OTA checkpoint-safe shutdown')
                            launch.assert_called_once()
                        paused=harness.journal.list_recoverable()
                        self.assertEqual(len(paused),1)
                        part=Path(paused[0]['files'][0]['part']);self.assertEqual(part.read_bytes(),PNG[:24])
                        harness.close();harness.open()
                        jobs=harness.journal.list_recoverable()
                        self.assertEqual(jobs[0]['config']['static_plan'],plan)
                        self.assertTrue(harness.window.resume_recoverable_jobs(jobs))
                        harness.pump_until(harness.settled,label='resume after simulated OTA')
                        self.assertEqual(harness.window.workflow_state,'complete',harness.window.log.toPlainText())
                        self.assertFalse(harness.journal.list_recoverable())
                        images=list(target_folder.rglob('*.png'))
                        self.assertEqual(sorted(path.name for path in images),['1.png','2.png'])
                        self.assertTrue(all(hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(PNG).digest() for path in images))
                        self.assertEqual(unrelated.read_bytes(),b'preserve existing data')
                        self.assertFalse(part.exists())
                        self.assertEqual(harness.window.title_check.isChecked(),by_title)
                    finally:
                        if site.pause_gate:site.pause_gate[1].set()
                        harness.close()

if __name__=='__main__':unittest.main()
