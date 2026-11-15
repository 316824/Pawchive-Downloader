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

    def legacy_empty_job(self, title, **changes):
        config={**self.config,'task_title':title,'task_post_url':PROFILE+'/post/old',**changes}
        config.pop('batch_id',None)
        job_id=self.journal.create(config)
        job=self.journal.load(job_id)
        job.update(status='completed',files=[],posts=None,done_posts=[])
        self.journal.save(job)
        return job_id

    def write_local(self, relative, payload, mtime):
        path=self.folder/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(payload)
        os.utime(path,ns=(mtime,mtime))
        return path

    def test_old_empty_completed_manifest_is_migrated_with_existing_flat_images(self):
        old=self.legacy_empty_job('old')
        self.write_local('图片文件/legacy.jpg',b'legacy',100)
        self.write_local('图片文件/nested/ignored.jpg',b'nested',50)
        self.write_local('图片文件/preserve.psd',b'project',50)
        current=self.job('downloaded',[('图片文件/new.png',b'new',200,False)])
        worker=self.finalize([current])
        self.assertFalse(worker.errors,worker.errors)
        self.assertEqual((self.folder/'图片文件/1.jpg').read_bytes(),b'legacy')
        self.assertEqual((self.folder/'图片文件/2.png').read_bytes(),b'new')
        self.assertEqual((self.folder/'图片文件/nested/ignored.jpg').read_bytes(),b'nested')
        self.assertTrue((self.folder/'图片文件/preserve.psd').exists())
        host=self.journal.load(current)
        self.assertEqual(len(host['local_numbering']),1)
        self.assertEqual(host['local_numbering'][0]['target'],str(self.folder/'图片文件/1.jpg'))
        self.assertNotIn('url',host['local_numbering'][0])
        self.assertEqual(self.journal.load(old)['files'],[])
        self.assertFalse(self.finalize([current]).errors)
        self.assertFalse(list((self.folder/'图片文件').glob('.pawchive-rename-*')))

    def test_old_title_directories_are_scanned_precisely_without_touching_attachments(self):
        self.legacy_empty_job('Old title',by_title=True)
        self.write_local('Old title/untracked.jpg',b'old',100)
        self.write_local('Old title/附件文件/attachment.jpg',b'attachment',50)
        self.write_local('Unrelated/image.jpg',b'unrelated',50)
        current=self.job('downloaded',[('New title/new.png',b'new',200,False)],
            by_title=True,task_title='New title')
        worker=self.finalize([current])
        self.assertFalse(worker.errors,worker.errors)
        self.assertEqual((self.folder/'Old title/1.jpg').read_bytes(),b'old')
        self.assertEqual((self.folder/'New title/1.png').read_bytes(),b'new')
        self.assertEqual((self.folder/'Old title/附件文件/attachment.jpg').read_bytes(),b'attachment')
        self.assertEqual((self.folder/'Unrelated/image.jpg').read_bytes(),b'unrelated')

    def test_local_migration_survives_process_exit_after_staging_or_final_os_move(self):
        import queue_operations
        root=self.folder
        for crash_on in (1,2):
            with self.subTest(crash_on=crash_on):
                self.folder=root/f'crash-{crash_on}'
                self.folder.mkdir()
                self.legacy_empty_job('old',folder=str(self.folder))
                self.write_local('图片文件/legacy.jpg',b'legacy',100)
                current=self.job('downloaded',[('图片文件/new.png',b'new',200,False)],folder=str(self.folder))
                original=queue_operations.move_exclusive
                moves=[]
                def crash(source,target):
                    original(source,target)
                    moves.append(str(target))
                    if len(moves)==crash_on: raise SystemExit('simulated local rename interruption')
                with patch('queue_operations.move_exclusive',side_effect=crash):
                    with self.assertRaises(SystemExit): self.finalize([current])
                self.assertEqual(self.journal.load(current)['status'],'downloaded')
                self.journal.close()
                self.journal=DownloadJournal(self.root/'journal')
                worker=self.finalize([current])
                self.assertFalse(worker.errors,worker.errors)
                self.assertEqual((self.folder/'图片文件/1.jpg').read_bytes(),b'legacy')
                self.assertEqual((self.folder/'图片文件/2.png').read_bytes(),b'new')
                record=self.journal.load(current)['local_numbering'][0]
                self.assertFalse(record.get('move_to'))
                self.assertTrue(Path(record['target']).exists())
                self.assertFalse(list((self.folder/'图片文件').glob('.pawchive-rename-*')))
        self.folder=root

    def test_duplicate_remote_references_follow_one_shared_file_through_renaming(self):
        first=self.job('completed',[('图片文件/shared.jpg',b'shared',100,False)])
        second=self.job('downloaded',[('图片文件/shared.jpg',b'shared',100,False),
            ('图片文件/new.png',b'new',200,False)])
        worker=self.finalize([second])
        self.assertFalse(worker.errors,worker.errors)
        a=self.journal.load(first)['files'][0]
        b=self.journal.load(second)['files'][0]
        self.assertEqual(a['target'],b['target'])
        self.assertEqual(Path(a['target']).read_bytes(),b'shared')
        self.assertEqual(len(list((self.folder/'图片文件').iterdir())),2)
        self.assertFalse(self.finalize([second]).errors)

    def test_shared_alias_recovers_exit_during_journal_intent_writes_before_os_move(self):
        first=self.job('completed',[('图片文件/shared.jpg',b'shared',100,False)])
        second=self.job('downloaded',[('图片文件/shared.jpg',b'shared',100,False)])
        original=self.journal.save
        saves=[]
        def stop_after_intent(job,dirty_records=None):
            original(job,dirty_records=dirty_records)
            if any(record.get('move_to') for record in job['files']):
                saves.append(job['id'])
                if len(saves)==1: raise SystemExit('interrupted alias checkpoint writes')
        with patch.object(self.journal,'save',side_effect=stop_after_intent):
            with self.assertRaises(SystemExit): self.finalize([second])
        self.journal.close()
        self.journal=DownloadJournal(self.root/'journal')
        worker=self.finalize([second])
        self.assertFalse(worker.errors,worker.errors)
        a=self.journal.load(first)['files'][0]
        b=self.journal.load(second)['files'][0]
        self.assertEqual(a['target'],b['target'])
        self.assertEqual(Path(a['target']).read_bytes(),b'shared')

    def test_local_manifest_outside_path_and_symlinks_are_not_followed(self):
        current=self.job('downloaded',[('图片文件/new.jpg',b'new',200,False)])
        outside=self.write_local('outside.jpg',b'outside',100)
        link=self.folder/'图片文件/link.jpg'
        try: link.symlink_to(outside)
        except OSError:
            # Windows may deny creating symlinks without Developer Mode.
            original=Path.is_symlink
            with patch.object(Path,'is_symlink',lambda path:path==link or original(path)):
                link.write_bytes(b'skip fixture')
                self.assertFalse(self.finalize([current]).errors)
                self.assertEqual(link.read_bytes(),b'skip fixture')
        else:
            self.assertFalse(self.finalize([current]).errors)
            self.assertTrue(link.is_symlink())
        job=self.journal.load(current)
        job['local_numbering']=[dict(id='a'*32,target=str(self.root/'escape.jpg'),size=1,sha256='b'*64)]
        with self.assertRaises(ValueError): self.journal.save(job,dirty_records=[])

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

    def test_initial_checkpoint_status_is_durable_and_default_remains_running(self):
        running=self.journal.create(self.config)
        paused=self.journal.create(self.config,status='paused')
        for invalid in ('completed','failed','',None,[] ):
            with self.assertRaises(ValueError):
                self.journal.create(self.config,status=invalid)
        self.journal.close()
        self.journal=DownloadJournal(self.root/'journal')
        records={job['id']:job for job in self.journal.list_recoverable()}
        self.assertEqual(set(records),{running,paused})
        self.assertEqual(records[running]['status'],'running')
        self.assertEqual(records[paused]['status'],'paused')

    def test_new_queue_checkpoints_use_one_full_sync_transaction_per_album(self):
        statements=[]
        self.journal._db.set_trace_callback(statements.append)
        entries=[dict(url=PROFILE+'/post/'+str(index),title='Album '+str(index),
            image_count=2,attachment_count=1,file_count=3,formats=['.jpg','.zip'],
            content_mode='images') for index in range(12)]
        try:
            results=self.prepare(entries)
        finally:
            self.journal._db.set_trace_callback(None)
        self.assertEqual(sum(sql.strip().upper()=='COMMIT' for sql in statements),len(entries))
        self.assertEqual(self.journal._db.execute('PRAGMA synchronous').fetchone()[0],2)
        self.assertEqual(self.journal._db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
        self.journal.close()
        self.journal=DownloadJournal(self.root/'journal')
        jobs=self.journal.list_recoverable()
        self.assertEqual(len(jobs),len(entries))
        for result in results:
            job=self.journal.load(result['metadata']['job_id'])
            self.assertEqual(job['status'],'paused')
            self.assertEqual(job['config']['content_mode'],'images')
            self.assertEqual(job['config']['task_file_count'],2)
            self.assertTrue(job['config']['queue_rename_order'])
            self.assertEqual(result['metadata']['_worker_config']['folder'],job['config']['folder'])

    def test_abrupt_preparation_interruption_leaves_created_album_recoverable(self):
        entries=[dict(url=PROFILE+'/post/'+str(index),title='Album '+str(index),
            formats=['.jpg'],image_count=2,attachment_count=0,file_count=2) for index in range(2)]
        create=self.journal.create
        calls=[]

        def interrupted(config,**options):
            if calls:
                # Simulate termination between albums, outside the normal
                # Exception cleanup path, without terminating this test.
                raise SystemExit('simulated interruption')
            job_id=create(config,**options)
            calls.append(job_id)
            return job_id

        worker=QueuePreparationWorker(self.journal,entries,self.config)
        with patch.object(self.journal,'create',side_effect=interrupted),self.assertRaises(SystemExit):
            worker.run()
        self.journal.close()
        self.journal=DownloadJournal(self.root/'journal')
        jobs=self.journal.list_recoverable()
        self.assertEqual([job['id'] for job in jobs],calls)
        self.assertEqual(jobs[0]['status'],'paused')
        self.assertEqual(jobs[0]['config']['task_file_count'],2)
        resumed=self.prepare([{**entries[0],'metadata':{'job_id':calls[0]}}])[0]
        self.assertEqual(resumed['metadata']['job_id'],calls[0])
        self.assertEqual(len(self.journal.list_recoverable()),1)


if __name__=='__main__': unittest.main()
