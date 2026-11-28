"""Incremental download preserves reserved paths and skips existing nonempty files off-thread."""
import copy,os,sys,tempfile,time,threading,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from PySide6.QtCore import QThread,QCoreApplication,QEvent,QTimer
from PySide6.QtWidgets import QApplication
from download_journal import DownloadJournal,local_file_snapshot,matches
from queue_operations import QueuePreparationWorker
from resumable_worker import remove_empty_destination
from static_download_plan import assign_plans
from test_download_resume import PROFILE,FILE,PNG,Session
import download_pawchive_images as desktop

class IncrementalDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_incremental_')
        self.root=Path(self.temp.name);self.folder=self.root/'下载'/'画师';self.folder.mkdir(parents=True)
        self.journal=DownloadJournal(self.root/'journal')
        self.config=dict(folder=str(self.folder),profile=PROFILE,by_title=False,order='newest',
            queue_task=True,queue_rename_order=True,rename_order=False,content_mode='images',layout_version=3)
    def tearDown(self):self.journal.close();self.temp.cleanup()
    def entries(self,count=3,title='Album',key='one'):
        return [dict(url=PROFILE+'/post/'+key,title=title,date='2026-11-28',assets=[dict(
            url=FILE.replace('a'*64,f'{index:064x}').replace('one.png',f'image{index}.png'),
            name=f'original{index}.png',direct=False,index=index) for index in range(count)])]
    def prepare(self,entries=None,config=None):
        config=config or self.config
        plans=assign_plans(entries or self.entries(),config)
        worker=QueuePreparationWorker(self.journal,plans,config)
        results=[];errors=[];worker.result.connect(results.append);worker.failed.connect(errors.append);worker.run()
        self.assertFalse(errors,errors);return results[0]
    def transfer(self,entry):
        worker=desktop.Worker(self.folder,PROFILE,'newest',False,journal=self.journal,
            resume_job_id=entry['metadata']['job_id'])
        worker.interrupted=False;worker.isInterruptionRequested=lambda:worker.interrupted
        worker.wait_interruptibly=lambda _:None;errors=[];success=[]
        worker.failed.connect(errors.append);worker.finished_ok.connect(success.append)
        urls=[asset['url'] for asset in self.journal.load(worker.job_id)['config']['static_plan']]
        session=Session(worker,file_urls=urls)
        with patch('resumable_worker.requests.Session',return_value=session):worker.run()
        self.assertFalse(errors,errors);self.assertEqual(success,[len(urls)])
        return session
    def test_mixed_existing_zero_and_missing_only_requests_missing_without_reindexing(self):
        existing=self.folder/'1.png';existing.write_bytes(b'existing user bytes')
        (self.folder/'2.png').write_bytes(b'')
        before=existing.stat();entry=self.prepare()[0]
        self.assertEqual(entry['metadata']['existing_files'],1);self.assertEqual(entry['metadata']['pending_files'],2)
        self.assertEqual([a['save_name'] for a in entry['static_plan']],['1.png','2.png','3.png'])
        session=self.transfer(entry)
        urls=[a['url'] for a in entry['static_plan']]
        self.assertEqual([url for url,_ in session.calls],urls[1:])
        self.assertEqual(existing.read_bytes(),b'existing user bytes');self.assertEqual(existing.stat().st_mtime_ns,before.st_mtime_ns)
        self.assertEqual((self.folder/'2.png').read_bytes(),PNG);self.assertEqual((self.folder/'3.png').read_bytes(),PNG)
    def test_all_existing_album_is_completed_during_preparation_without_transfer(self):
        for index in range(1,4):(self.folder/f'{index}.png').write_bytes(b'exists')
        entry=self.prepare()[0];job=self.journal.load(entry['metadata']['job_id'])
        self.assertTrue(entry['metadata']['all_files_existing']);self.assertEqual(entry['metadata']['pending_files'],0)
        self.assertEqual(job['status'],'completed');self.assertEqual(job['saved_count'],3)
        self.assertFalse(self.journal.list_recoverable())
    def test_discard_checkpoint_retains_completed_files_and_next_plan_skips_them(self):
        entry=self.prepare()[0];self.transfer(entry)
        self.journal.discard(entry['metadata']['job_id'])
        self.assertEqual(len(list(self.folder.glob('*.png'))),3)
        next_entry=self.prepare()[0]
        self.assertTrue(next_entry['metadata']['all_files_existing'])
        session=self.transfer(next_entry);self.assertFalse(session.calls)
    def test_abandon_interrupted_plan_then_same_directory_downloads_only_missing(self):
        entry=self.prepare()[0];worker=desktop.Worker(self.folder,PROFILE,'newest',False,
            journal=self.journal,resume_job_id=entry['metadata']['job_id'])
        worker.interrupted=False;worker.isInterruptionRequested=lambda:worker.interrupted
        worker.preserve_on_interrupt=True;worker.wait_interruptibly=lambda _:None
        urls=[asset['url'] for asset in entry['static_plan']]
        session=Session(worker,file_urls=urls,interrupt_file=urls[1])
        with patch('resumable_worker.requests.Session',return_value=session):worker.run()
        job=self.journal.load(worker.job_id);self.assertEqual(job['status'],'paused')
        part=Path(job['files'][1]['part']);self.assertTrue(part.exists())
        self.assertEqual((self.folder/'1.png').read_bytes(),PNG)
        self.journal.discard(worker.job_id);self.assertFalse(part.exists())
        replacement=self.prepare()[0];self.assertEqual(replacement['metadata']['existing_files'],1)
        second=self.transfer(replacement)
        self.assertEqual([url for url,_ in second.calls],urls[1:])
        self.assertEqual(sorted(path.name for path in self.folder.glob('*.png')),['1.png','2.png','3.png'])
    def test_by_title_paths_restart_numbers_without_extra_image_directory(self):
        config={**self.config,'by_title':True}
        entries=self.entries(2,title='图集甲',key='a')+self.entries(2,title='图集乙',key='b')
        path=self.folder/'图集甲'/'1.png';path.parent.mkdir();path.write_bytes(b'existing')
        result=self.prepare(entries,config)
        self.assertEqual([e['metadata']['existing_files'] for e in result],[1,0])
        for entry in result:self.transfer(entry)
        self.assertEqual(path.read_bytes(),b'existing')
        self.assertEqual((self.folder/'图集乙'/'1.png').read_bytes(),PNG)
        self.assertFalse((self.folder/'图片文件').exists())
    def test_deleted_file_after_preparation_downloads_its_original_slot(self):
        target=self.folder/'1.png';target.write_bytes(b'exists')
        entry=self.prepare()[0];target.unlink();session=self.transfer(entry)
        self.assertEqual(len(session.calls),3);self.assertEqual(target.read_bytes(),PNG)
    def test_new_nonempty_file_after_preparation_is_skipped_without_network(self):
        entry=self.prepare()[0];target=self.folder/'2.png';target.write_bytes(b'appeared meanwhile')
        session=self.transfer(entry)
        self.assertEqual(len(session.calls),2);self.assertEqual(target.read_bytes(),b'appeared meanwhile')
    def test_empty_replacement_never_removes_a_nonempty_file(self):
        target=self.folder/'empty.png';target.write_bytes(b'')
        remove_empty_destination(target);self.assertFalse(target.exists())
        target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):remove_empty_destination(target)
        self.assertEqual(target.read_bytes(),b'keep')
    def test_record_snapshot_is_stat_only_and_detects_modified_identity(self):
        path=self.folder/'1.png';path.write_bytes(b'original');snapshot=local_file_snapshot(path)
        record=dict(target=str(path),existing_local=True,local_snapshot=snapshot)
        self.assertTrue(matches(record));path.write_bytes(b'modified and longer');self.assertFalse(matches(record))
    def test_existing_check_does_not_hash_or_read_full_local_files(self):
        (self.folder/'1.png').write_bytes(b'large existing file stand-in')
        with patch('download_journal.fingerprint',side_effect=AssertionError('Existing-file scan must stay stat-only')):
            entry=self.prepare(self.entries(1))[0]
            session=self.transfer(entry)
        self.assertFalse(session.calls)
    def test_directory_at_reserved_filename_is_not_treated_as_a_downloaded_file(self):
        (self.folder/'1.png').mkdir()
        plans=assign_plans(self.entries(1),self.config)
        worker=QueuePreparationWorker(self.journal,plans,self.config);errors=[]
        worker.failed.connect(errors.append);worker.run()
        self.assertTrue(errors);self.assertTrue((self.folder/'1.png').is_dir())
        self.assertFalse(self.journal.list_recoverable())
    def test_recycled_journal_is_not_auto_resumed_and_discard_keeps_local_files(self):
        (self.folder/'1.png').write_bytes(b'keep')
        entry=self.prepare()[0];job=self.journal.load(entry['metadata']['job_id'])
        job['status']='recycled';self.journal.save(job)
        self.assertFalse(self.journal.list_recoverable());self.assertEqual(len(self.journal.list_recycled()),1)
        self.journal.discard(job['id']);self.assertEqual((self.folder/'1.png').read_bytes(),b'keep')
    def test_recycled_restore_stays_bin_owned_until_transfer_really_starts(self):
        entry=self.prepare()[0];job=self.journal.load(entry['metadata']['job_id'])
        job['status']='recycled';self.journal.save(job)
        restored=[];errors=[]
        worker=QueuePreparationWorker(self.journal,[entry],{**self.config,'by_title':True})
        worker.result.connect(restored.append);worker.failed.connect(errors.append);worker.run()
        self.assertFalse(errors);self.assertEqual(self.journal.load(job['id'])['status'],'recycled')
        self.assertEqual(len(self.journal.list_recycled()),1)
        self.assertFalse(self.journal.list_recoverable())
        self.assertFalse(self.journal.load(job['id'])['config']['by_title'])
        self.transfer(restored[0][0])
        self.assertEqual(self.journal.load(job['id'])['status'],'completed')
        self.assertFalse(self.journal.list_recycled())
    def test_static_new_and_restored_jobs_need_only_one_full_durable_commit(self):
        (self.folder/'1.png').write_bytes(b'previous file')
        statements=[];self.journal._db.set_trace_callback(statements.append)
        try:entry=self.prepare()[0]
        finally:self.journal._db.set_trace_callback(None)
        self.assertEqual(sum(sql.strip().upper()=='COMMIT' for sql in statements),1)
        self.assertEqual(self.journal._db.execute('PRAGMA synchronous').fetchone()[0],2)
        job_id=entry['metadata']['job_id']
        self.journal.close();self.journal=DownloadJournal(self.root/'journal')
        job=self.journal.load(job_id)
        self.assertEqual(job['saved_count'],1);self.assertEqual(len(job['files']),3)
        self.assertEqual(job['status'],'paused')
        statements=[];self.journal._db.set_trace_callback(statements.append)
        worker=QueuePreparationWorker(self.journal,[entry],self.config);errors=[]
        worker.failed.connect(errors.append)
        try:worker.run()
        finally:self.journal._db.set_trace_callback(None)
        self.assertFalse(errors)
        self.assertEqual(sum(sql.strip().upper()=='COMMIT' for sql in statements),1)
    def test_failed_restore_batch_keeps_original_recycle_checkpoint(self):
        entry=self.prepare()[0];job=self.journal.load(entry['metadata']['job_id'])
        job['status']='recycled';self.journal.save(job)
        bad=dict(url=PROFILE+'/post/bad',title='Bad',static_plan=[dict(
            url=FILE,post_url=PROFILE+'/post/bad',save_name='../escape.png',relative_dir='.')])
        worker=QueuePreparationWorker(self.journal,[entry,bad],self.config);errors=[]
        worker.failed.connect(errors.append);worker.run()
        self.assertTrue(errors);self.assertEqual(self.journal.load(job['id'])['status'],'recycled')
        self.assertFalse(self.journal.list_recoverable())
    def test_thousands_of_path_checks_use_native_preparation_thread_and_gui_heartbeat(self):
        plans=assign_plans(self.entries(1600),self.config)
        checks=[];original=local_file_snapshot
        def snapshot(path):checks.append(QThread.currentThread());return original(path)
        worker=QueuePreparationWorker(self.journal,plans,self.config);results=[];errors=[];heartbeat=[]
        worker.result.connect(results.append);worker.failed.connect(errors.append)
        with patch('queue_operations.local_file_snapshot',side_effect=snapshot):
            worker.start();QTimer.singleShot(0,lambda:heartbeat.append(True))
            deadline=time.monotonic()+15
            while worker.isRunning() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.001)
            self.assertTrue(worker.wait(1000));self.app.processEvents()
        self.assertFalse(errors,errors);self.assertTrue(results);self.assertTrue(heartbeat)
        self.assertEqual(len(checks),1600);self.assertTrue(all(thread!=self.app.thread() for thread in checks))
        worker.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

if __name__=='__main__':unittest.main()
