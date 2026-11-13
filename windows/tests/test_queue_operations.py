"""Durable batch preparation/finalization across real files and SQLite journals."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication

from download_journal import DownloadJournal, fingerprint
from download_scheduler import normalize_task
from queue_operations import QueuePreparationWorker, QueueFinalizeWorker
from resumable_worker import move_exclusive

PROFILE='https://pawchive.pw/fanbox/user/1234'


def unique_path(directory, name):
    result=Path(directory)/name
    n=1
    while result.exists():
        result=Path(directory)/(Path(name).stem+f'_{n}'+Path(name).suffix)
        n+=1
    return result


class QueueOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_queue_operations_')
        self.root=Path(self.temp.name)
        self.folder=self.root/'downloads'
        self.folder.mkdir()
        self.journal=DownloadJournal(self.root/'journal')
        self.helpers={'unique_path':unique_path}
        self.config=dict(folder=str(self.folder),profile=PROFILE,order='oldest',
            by_title=False,rename_order=False,queue_task=True,queue_rename_order=True,
            content_mode='all',batch_id='fixture-batch')

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def job(self, status, files, **changes):
        config={**self.config,**changes}
        job_id=self.journal.create(config)
        job=self.journal.load(job_id)
        job['status']=status
        job['files']=[]
        for index,(relative,payload,mtime,direct) in enumerate(files):
            path=self.folder/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(payload)
            os.utime(path,ns=(mtime,mtime))
            post=PROFILE+'/post/'+str(index)
            key=f'{index:020x}'
            job['files'].append(dict(url='https://file.pawchive.pw/data/'+('a'*60)+f'{index:04x}',
                post_url=post,target=str(path),part=str(path.parent/f'.pawchive-{job_id}-{key}.part'),
                name=path.name,title='fixture',status='complete',date='2026-11-01',sequence=index,
                size=len(payload),sha256=fingerprint(path),direct=direct))
        job['saved_count']=len(files)
        self.journal.save(job)
        return job_id

    def finalize(self, ids):
        worker=QueueFinalizeWorker(self.journal,ids,self.helpers)
        worker.results=[]; worker.errors=[]
        worker.result.connect(worker.results.append)
        worker.failed.connect(worker.errors.append)
        worker.run()
        return worker

    def test_resumed_batch_includes_finished_sibling_and_only_numbers_images_by_mtime(self):
        first=self.job('completed',[
            ('图片文件/old.jpg',b'older',100,False),
            ('其它文件/fixture/painting.psd',b'project',125,True)])
        second=self.job('downloaded',[
            ('图片文件/new.png',b'newer',200,False),
            ('视频文件/fixture/movie.mp4',b'video',225,True)])
        worker=self.finalize([second])
        self.assertFalse(worker.errors,worker.errors)
        self.assertEqual((self.folder/'图片文件/1.jpg').read_bytes(),b'older')
        self.assertEqual((self.folder/'图片文件/2.png').read_bytes(),b'newer')
        self.assertTrue((self.folder/'其它文件/fixture/painting.psd').exists())
        self.assertTrue((self.folder/'视频文件/fixture/movie.mp4').exists())
        for job_id in (first,second):
            job=self.journal.load(job_id)
            self.assertEqual(job['status'],'completed')
            self.assertTrue(all(Path(record['target']).exists() for record in job['files']))
        self.assertEqual(self.journal.list_recoverable(),[])

    def test_title_layout_numbers_each_album_independently_and_never_overwrites_other_files(self):
        a=self.job('downloaded', [('A/x.jpg',b'a',100,False),('A/y.png',b'b',200,False)],by_title=True)
        b=self.job('downloaded', [('B/x.png',b'c',100,False)],by_title=True)
        (self.folder/'A/1.jpg').write_bytes(b'user image')
        worker=self.finalize([a,b])
        self.assertFalse(worker.errors,worker.errors)
        self.assertEqual((self.folder/'A/1.jpg').read_bytes(),b'user image')
        self.assertEqual((self.folder/'A/2.jpg').read_bytes(),b'a')
        self.assertEqual((self.folder/'A/3.png').read_bytes(),b'b')
        self.assertEqual((self.folder/'B/1.png').read_bytes(),b'c')

    def test_downloaded_manifest_is_recoverable_after_reopening_database(self):
        job_id=self.job('downloaded',[('图片文件/a.jpg',b'a',100,False)])
        self.journal.close()
        self.journal=DownloadJournal(self.root/'journal')
        self.assertEqual([job['id'] for job in self.journal.list_recoverable()],[job_id])
        self.assertFalse(self.finalize([job_id]).errors)

    def test_crash_between_os_move_and_checkpoint_commit_recovers_both_staging_phases(self):
        for crash_on in (1,3):
            with self.subTest(crash_on=crash_on):
                directory=f'images{crash_on}'
                job_id=self.job('downloaded',[
                    (directory+'/source_a.jpg',b'a',100,False),
                    (directory+'/source_b.jpg',b'b',200,False)],batch_id=f'crash{crash_on}')
                moves=[]
                def crash(source,target):
                    move_exclusive(source,target)
                    moves.append(str(target))
                    if len(moves)==crash_on: raise SystemExit('simulated process exit')
                with patch('resumable_worker.move_exclusive',side_effect=crash):
                    with self.assertRaises(SystemExit): self.finalize([job_id])
                self.assertEqual(self.journal.load(job_id)['status'],'downloaded')
                self.journal.close()
                self.journal=DownloadJournal(self.root/'journal')
                worker=self.finalize([job_id])
                self.assertFalse(worker.errors,worker.errors)
                self.assertEqual((self.folder/directory/'1.jpg').read_bytes(),b'a')
                self.assertEqual((self.folder/directory/'2.jpg').read_bytes(),b'b')
                job=self.journal.load(job_id)
                self.assertTrue(all(not r.get('move_to') for r in job['files']))
                self.assertFalse(list((self.folder/directory).glob('.pawchive-rename-*')))

    def test_missing_or_modified_image_does_not_silently_mark_finalization_successful(self):
        for missing in (False,True):
            with self.subTest(missing=missing):
                job_id=self.job('downloaded',[(f'images{missing}/a.jpg',b'a',100,False)],batch_id=str(missing))
                path=Path(self.journal.load(job_id)['files'][0]['target'])
                if missing: path.unlink()
                else: path.write_bytes(b'external edit')
                worker=self.finalize([job_id])
                self.assertTrue(worker.errors)
                self.assertEqual(self.journal.load(job_id)['status'],'downloaded')

    def prepare(self, entries, config=None):
        worker=QueuePreparationWorker(self.journal,entries,config or self.config)
        result=[]; errors=[]
        worker.result.connect(result.append); worker.failed.connect(errors.append)
        worker.run()
        self.assertFalse(errors,errors)
        return result[0]

    def test_preparation_restores_saved_options_and_filters_scan_counts_and_formats(self):
        job_id=self.job('paused',[],by_title=True,queue_rename_order=True,content_mode='images')
        result=self.prepare([dict(url=PROFILE+'/post/1',title='new title',formats=['.jpg','.zip'],
            image_formats=['.jpg'],attachment_formats=['.zip'],image_count=7,attachment_count=2,
            file_count=9,metadata={'job_id':job_id})],{**self.config,'by_title':False,
            'queue_rename_order':False,'content_mode':'attachments'})[0]
        self.assertEqual(result['content_mode'],'images')
        self.assertEqual(result['file_count'],7)
        self.assertEqual(result['formats'],['.jpg'])
        self.assertFalse(normalize_task(result,0).direct)
        self.assertTrue(result['metadata']['by_title'])
        self.assertTrue(result['metadata']['queue_rename_order'])
        self.assertIn('_journal_records',result['metadata'])
        config=self.journal.load(job_id)['config']
        self.assertEqual(config['task_file_count'],7)
        self.assertEqual(config['task_image_count'],7)
        self.assertEqual(config['task_attachment_count'],2)

    def test_old_rename_setting_and_attachment_selection_are_retained(self):
        old={**self.config,'rename_order':True,'by_title':True,'content_mode':'attachments'}
        old.pop('queue_rename_order')
        job_id=self.journal.create(old)
        result=self.prepare([dict(url=PROFILE+'/post/1',formats=['.jpg','.zip'],image_count=7,
            attachment_count=2,file_count=9,metadata={'job_id':job_id})])[0]
        self.assertTrue(result['metadata']['queue_rename_order'])
        self.assertEqual(result['formats'],['.zip'])
        self.assertEqual(result['file_count'],2)
        self.assertTrue(normalize_task(result,0).direct)

    def test_new_per_album_image_choice_overrides_global_selection(self):
        result=self.prepare([dict(url=PROFILE+'/post/1',formats=[],image_count=3,
            attachment_count=0,file_count=3,content_mode='images')])[0]
        self.assertFalse(normalize_task(result,0).direct)
        self.assertEqual(result['file_count'],3)
        self.assertEqual(self.journal.load(result['metadata']['job_id'])['config']['content_mode'],'images')


if __name__=='__main__': unittest.main()
