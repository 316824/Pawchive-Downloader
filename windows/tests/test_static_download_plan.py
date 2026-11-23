"""Completion order, queue order and retries cannot change reserved filenames."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from PySide6.QtWidgets import QApplication
import download_pawchive_images as desktop
from static_download_plan import assign_plans,extract_assets
from queue_operations import QueuePreparationWorker,QueueFinalizeWorker
from download_journal import DownloadJournal
from test_download_resume import Session,FILE,PNG,PROFILE

FILE2=FILE.replace('a'*64,'b'*64).replace('one.png','two.png')


class StaticPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_static_plan_')
        self.root=Path(self.temp.name);self.folder=self.root/'downloads'
        self.journal=DownloadJournal(self.root/'journal')
        self.config=dict(folder=str(self.folder),profile=PROFILE,by_title=False,order='newest',
            queue_task=True,queue_rename_order=True,rename_order=False,content_mode='images',layout_version=3)
    def tearDown(self):self.journal.close();self.temp.cleanup()
    def entries(self):
        return [dict(url=PROFILE+'/post/new',date='2026-11-20',title='New',assets=[dict(url=FILE,name='one.png',direct=False,index=0)]),
                dict(url=PROFILE+'/post/old',date='2026-10-9',title='Old',assets=[dict(url=FILE2,name='two.png',direct=False,index=0)])]
    def prepare(self,entries=None):
        plans=assign_plans(entries or self.entries(),self.config)
        worker=QueuePreparationWorker(self.journal,plans,self.config)
        results=[];errors=[];worker.result.connect(results.append);worker.failed.connect(errors.append);worker.run()
        self.assertFalse(errors,errors);return results[0]
    def worker(self,entry):
        worker=desktop.Worker(self.folder,PROFILE,'newest',False,journal=self.journal,resume_job_id=entry['metadata']['job_id'])
        worker.interrupted=False;worker.isInterruptionRequested=lambda:worker.interrupted
        worker.wait_interruptibly=lambda seconds:None;worker.success=[];worker.errors=[]
        worker.finished_ok.connect(worker.success.append);worker.failed.connect(worker.errors.append)
        return worker
    def test_old_to_new_numbers_are_preallocated_before_any_worker_starts(self):
        plans=assign_plans(self.entries(),self.config)
        self.assertEqual([p['static_plan'][0]['save_name'] for p in plans],['2.png','1.png'])
        self.assertFalse(self.folder.exists())
    def test_reverse_completion_and_finalizer_never_renumber_or_create_image_folder(self):
        entries=self.prepare();all_calls=[]
        for entry in entries:  # newest completes first, contrary to reserved order
            worker=self.worker(entry);session=Session(worker,file_urls=[FILE,FILE2])
            with patch('resumable_worker.requests.Session',return_value=session):worker.run()
            self.assertFalse(worker.errors,worker.errors);all_calls.extend(url for url,_ in session.calls)
        self.assertEqual(set(all_calls),{FILE,FILE2})
        self.assertEqual((self.folder/'1.png').read_bytes(),PNG)
        self.assertEqual((self.folder/'2.png').read_bytes(),PNG)
        self.assertFalse((self.folder/'图片文件').exists())
        finalizer=QueueFinalizeWorker(self.journal,[e['metadata']['job_id'] for e in entries],
            dict(unique_path=desktop.unique_path,safe_path_component=desktop.safe_path_component))
        errors=[];finalizer.failed.connect(errors.append);finalizer.run();self.assertFalse(errors,errors)
        for entry in entries:
            job=self.journal.load(entry['metadata']['job_id'])
            self.assertEqual(Path(job['files'][0]['target']).name,job['config']['static_plan'][0]['save_name'])
    def test_pause_resume_keeps_exact_name_and_resumes_partial(self):
        entries=self.prepare();entry=entries[0];first=self.worker(entry);first.preserve_on_interrupt=True
        session=Session(first,file_urls=[FILE],interrupt_file=FILE)
        with patch('resumable_worker.requests.Session',return_value=session):first.run()
        job=self.journal.load(first.job_id);self.assertEqual(job['status'],'paused')
        self.assertTrue(Path(job['files'][0]['part']).exists())
        resumed=self.worker(entry);session=Session(resumed,'range',file_urls=[FILE])
        with patch('resumable_worker.requests.Session',return_value=session):resumed.run()
        self.assertFalse(resumed.errors,resumed.errors)
        self.assertTrue(any('Range' in kwargs.get('headers',{}) for _,kwargs in session.calls))
        self.assertEqual((self.folder/'2.png').read_bytes(),PNG)
    def test_title_folders_restart_numbers_and_formats_share_one_sequence(self):
        entries=self.entries();entries[0]['assets'].append(dict(url=FILE2,name='two.jpeg',direct=False,index=1))
        self.config['by_title']=True
        plans=assign_plans(entries,self.config)
        self.assertEqual([a['save_name'] for a in plans[0]['static_plan']],['1.png','2.jpeg'])
        self.assertEqual(plans[1]['static_plan'][0]['save_name'],'1.png')
    def test_mixed_layout_keeps_attachments_out_of_image_numbering(self):
        entries=self.entries();entries[0]['assets'].append(dict(url=FILE2,name='archive.zip',direct=True,index=1))
        self.config['content_mode']='all';plans=assign_plans(entries,self.config)
        self.assertEqual(plans[0]['static_plan'][0]['relative_dir'],'.')
        attachment=plans[0]['static_plan'][1]
        self.assertIsNone(attachment['number']);self.assertEqual(attachment['save_name'],'New.zip')
        self.assertEqual(attachment['relative_dir'],'.')
    def test_existing_file_is_protected_instead_of_worker_allocating_a_new_number(self):
        self.folder.mkdir();(self.folder/'1.png').write_bytes(b'private user file')
        plans=assign_plans(self.entries(),self.config);worker=QueuePreparationWorker(self.journal,plans,self.config)
        errors=[];worker.failed.connect(errors.append);worker.run()
        self.assertTrue(errors);self.assertEqual((self.folder/'1.png').read_bytes(),b'private user file')
        self.assertFalse(self.journal.list_recoverable())
