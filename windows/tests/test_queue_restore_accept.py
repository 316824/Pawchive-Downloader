"""A restored bin entry is consumed only after a durable normal checkpoint."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from download_journal import DownloadJournal
from queue_operations import QueueRestoreAcceptWorker
from recycle_bin import RecycleBin

PROFILE='https://pawchive.pw/fanbox/user/1234'


class QueueRestoreAcceptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_restore_accept_')
        self.root=Path(self.temp.name);self.folder=self.root/'downloads';self.folder.mkdir()
        self.journal=DownloadJournal(self.root/'state');self.bin=RecycleBin(self.root/'state')

    def tearDown(self):
        self.bin.close();self.journal.close();self.temp.cleanup()

    def entry(self,index=1,*,completed=False,payload=None):
        config=dict(folder=str(self.folder),profile=PROFILE,by_title=False,
            rename_order=False,queue_rename_order=False,content_mode='images',start_number=1)
        job_id=self.journal.create(config,status='paused')
        entry=self.bin.add(title='Album '+str(index),profile_url=PROFILE,
            post_url=PROFILE+'/post/'+str(index),metadata={'job_id':job_id},payload_path=payload)
        job=self.journal.load(job_id);job['status']='completed' if completed else 'recycled'
        job['config']['recycle_entry_id']=entry.id;self.journal.save(job,dirty_records=[])
        return dict(title=entry.title,url=entry.post_url,metadata=dict(job_id=job_id,
            recycled_from=entry.id,all_files_existing=completed))

    def worker(self,entries):
        worker=QueueRestoreAcceptWorker(self.journal,self.bin,entries)
        worker.results=[];worker.errors=[]
        worker.result.connect(worker.results.append);worker.failed.connect(worker.errors.append)
        return worker

    def test_journal_save_failure_does_not_consume_any_bin_entry(self):
        entries=[self.entry(1),self.entry(2)];worker=self.worker(entries)
        with (patch.object(self.journal,'save',side_effect=sqlite3.OperationalError('disk full')),
                patch.object(self.bin,'take',wraps=self.bin.take) as take):
            worker.run()
        self.assertTrue(worker.errors);self.assertFalse(worker.results);take.assert_not_called()
        self.assertEqual(len(self.bin.list_entries()),2)
        self.assertTrue(all(self.journal.load(entry['metadata']['job_id'])['status']=='recycled' for entry in entries))

    def test_later_promotion_failure_preserves_all_bins_and_prior_normal_checkpoint(self):
        entries=[self.entry(1),self.entry(2)];worker=self.worker(entries);save=self.journal.save;calls=[]
        def fail_second(job,**options):
            calls.append(job['id'])
            if len(calls)==2:raise sqlite3.OperationalError('disk full')
            return save(job,**options)
        with patch.object(self.journal,'save',side_effect=fail_second),patch.object(self.bin,'take',wraps=self.bin.take) as take:
            worker.run()
        self.assertTrue(worker.errors);take.assert_not_called()
        self.assertEqual(len(self.bin.list_entries()),2)
        self.assertEqual(self.journal.load(entries[0]['metadata']['job_id'])['status'],'paused')
        self.assertEqual(self.journal.load(entries[1]['metadata']['job_id'])['status'],'recycled')

    def test_take_sqlite_failure_keeps_restore_recoverable_and_returns_warning(self):
        entry=self.entry();worker=self.worker([entry])
        with patch.object(self.bin,'take',side_effect=sqlite3.OperationalError('database locked')):worker.run()
        self.assertFalse(worker.errors);meta=worker.results[0][0]['metadata']
        self.assertTrue(meta['_restore_accepted']);self.assertIn('database locked',meta['_restore_cleanup_warning'])
        self.assertEqual(self.journal.load(meta['job_id'])['status'],'paused')
        self.assertIsNotNone(self.bin.get(meta['recycled_from']))
        self.assertEqual([job['id'] for job in self.journal.list_recoverable()],[meta['job_id']])

    def test_acceptance_before_any_download_survives_database_reopen(self):
        entries=[self.entry(1),self.entry(2)];worker=self.worker(entries);worker.run()
        self.assertFalse(worker.errors);self.assertFalse(self.bin.list_entries())
        self.assertTrue(all(entry['metadata']['_restore_accepted'] for entry in worker.results[0]))
        self.assertTrue(all('_restore_accepted' not in entry['metadata'] for entry in entries))
        self.journal.close();self.journal=DownloadJournal(self.root/'state')
        recovered=self.journal.list_recoverable()
        self.assertEqual({job['id'] for job in recovered},{entry['metadata']['job_id'] for entry in entries})
        self.assertTrue(all(job['status']=='paused' for job in recovered))
        self.assertTrue(all(job['config']['folder']==str(self.folder) and not job['config']['by_title'] for job in recovered))

    def test_all_existing_completed_job_is_not_changed_to_paused(self):
        entry=self.entry(completed=True);worker=self.worker([entry])
        with patch.object(self.journal,'save',wraps=self.journal.save) as save:worker.run()
        self.assertFalse(worker.errors);save.assert_not_called();self.assertFalse(self.bin.list_entries())
        self.assertEqual(self.journal.load(entry['metadata']['job_id'])['status'],'completed')

    def test_legacy_moved_payload_is_restored_instead_of_deleted(self):
        payload=self.folder/'old album';payload.mkdir();(payload/'original.png').write_bytes(b'legacy download')
        entry=self.entry(payload=payload);self.assertFalse(payload.exists());worker=self.worker([entry])
        with patch.object(self.bin,'take',wraps=self.bin.take) as take:worker.run()
        self.assertFalse(worker.errors);take.assert_not_called()
        self.assertEqual((payload/'original.png').read_bytes(),b'legacy download')
        self.assertFalse(self.bin.list_entries())
        self.assertEqual(self.journal.load(entry['metadata']['job_id'])['status'],'paused')

    def test_legacy_restore_collision_keeps_both_original_and_new_local_files(self):
        payload=self.folder/'old album';payload.mkdir();(payload/'original.png').write_bytes(b'legacy download')
        entry=self.entry(payload=payload);payload.mkdir();(payload/'new.png').write_bytes(b'keep local')
        worker=self.worker([entry]);worker.run()
        self.assertFalse(worker.errors);self.assertEqual((payload/'new.png').read_bytes(),b'keep local')
        self.assertEqual((self.folder/'old album (1)'/'original.png').read_bytes(),b'legacy download')
        self.assertFalse(self.bin.list_entries())

    def test_interruption_before_acceptance_keeps_bin_ownership(self):
        entry=self.entry();worker=self.worker([entry]);worker.requestInterruption()
        # QThread ignores requestInterruption before start; exercise the same
        # requested condition without relying on that lifecycle detail.
        worker.isInterruptionRequested=lambda:True;worker.run()
        self.assertTrue(worker.errors);self.assertIsNotNone(self.bin.get(entry['metadata']['recycled_from']))
        self.assertEqual(self.journal.load(entry['metadata']['job_id'])['status'],'recycled')

    def test_acceptance_native_thread_never_saves_on_gui_thread(self):
        entry=self.entry();worker=self.worker([entry]);threads=[];save=self.journal.save
        def observe(job,**options):threads.append(QThread.currentThread());return save(job,**options)
        with patch.object(self.journal,'save',side_effect=observe):
            worker.start();deadline=time.monotonic()+5
            while worker.isRunning() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.001)
            self.assertTrue(worker.wait(1000));self.app.processEvents()
        self.assertFalse(worker.errors);self.assertTrue(worker.results)
        self.assertEqual(len(threads),1);self.assertNotEqual(threads[0],self.app.thread())


if __name__=='__main__':unittest.main()
