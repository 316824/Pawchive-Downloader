"""Offline checkpoint and HTTP Range integration; never contacts a real server."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from requests.structures import CaseInsensitiveDict

import download_pawchive_images as app
from download_journal import DownloadJournal


PROFILE='https://pawchive.pw/fanbox/user/offline-test'
POST=PROFILE+'/post/one'
FILE='https://file.pawchive.pw/data/ab/cd/'+('a'*64)+'.png?f=one.png'
PNG=b'\x89PNG\r\n\x1a\n'+b'local fixture payload'*15


class Response:
    def __init__(self,body=b'',status=200,headers=None,interrupt=None):
        self.body=body; self.status_code=status; self.headers=CaseInsensitiveDict(headers or {})
        self.interrupt=interrupt
    @property
    def text(self): return self.body.decode()
    def raise_for_status(self):
        if self.status_code>=400:
            import requests
            raise requests.HTTPError(str(self.status_code))
    def iter_content(self,size):
        if self.interrupt:
            yield self.body[:40]
            self.interrupt()
            yield self.body[40:]
        else: yield self.body
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


class Session:
    def __init__(self,worker,mode='full',file_urls=None,interrupt_file=None):
        self.worker=worker; self.mode=mode; self.headers={}; self.calls=[]
        self.file_urls=file_urls or [FILE]; self.interrupt_file=interrupt_file
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        if url==PROFILE:
            html='<article class="post-card"><a href="'+POST+'">post</a><time class="timestamp" datetime="2026-11-01"></time><div class="post-card__header">Fixture</div></article>'
            return Response(html.encode())
        if url==POST:
            html='<h1 class="post__title"><span>Fixture</span></h1><div class="post__files">'+''.join(
                '<a class="fileThumb" href="'+u+'" download="'+str(i)+'.png">image</a>' for i,u in enumerate(self.file_urls))+'</div>'
            return Response(html.encode())
        if url in self.file_urls:
            if url==self.interrupt_file:
                return Response(PNG,headers={'content-length':str(len(PNG)),'etag':'"fixture"'},interrupt=lambda:setattr(self.worker,'interrupted',True))
            offset=int(kwargs.get('headers',{}).get('Range','bytes=0-')[6:-1])
            if offset and self.mode=='range':
                return Response(PNG[offset:],206,{'content-range':f'bytes {offset}-{len(PNG)-1}/{len(PNG)}','content-length':str(len(PNG)-offset),'etag':'"fixture"'})
            if offset and self.mode=='bad_range':
                return Response(PNG[offset:],206,{'content-range':f'bytes {offset+1}-{len(PNG)-1}/{len(PNG)}'})
            if offset and self.mode=='416': return Response(status=416)
            if self.mode=='html': return Response(b'<html>not an image</html>',headers={'content-type':'text/html'})
            return Response(PNG,headers={'content-length':str(len(PNG)),'etag':'"fixture"'})
        raise AssertionError('Unexpected offline URL: '+url)
    def close(self): pass


class DownloadResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_resume_test_')
        self.root=Path(self.temp.name); self.folder=self.root/'downloads'
        self.journal=DownloadJournal(self.root/'records')
    def tearDown(self): self.journal.close(); self.temp.cleanup()
    def worker(self,resume=None,by_title=True,rename=False):
        w=app.Worker(self.folder,PROFILE,'newest',by_title,journal=self.journal,resume_job_id=resume,rename_order=rename)
        w.interrupted=False; w.isInterruptionRequested=lambda:w.interrupted
        w.wait_interruptibly=lambda seconds:None
        w.success=[]; w.errors=[]
        w.finished_ok.connect(w.success.append); w.failed.connect(w.errors.append)
        return w
    def run_worker(self,w,session):
        with patch('resumable_worker.requests.Session',return_value=session): w.run()
    def partial(self,*,urls=None):
        w=self.worker(); w.preserve_on_interrupt=True
        session=Session(w,file_urls=urls,interrupt_file=(urls or [FILE])[-1])
        self.run_worker(w,session)
        job=self.journal.load(w.job_id)
        self.assertEqual(job['status'],'paused')
        partial=Path(job['files'][-1]['part'])
        self.assertEqual(partial.read_bytes(),PNG[:40])
        return w,job

    def test_exit_preserves_partial_and_matching_range_continues_without_duplication(self):
        first,job=self.partial()
        self.assertEqual(self.journal.list_recoverable()[0]['id'],first.job_id)
        w=self.worker(first.job_id); session=Session(w,'range')
        self.run_worker(w,session)
        self.assertEqual(w.success,[1]); self.assertEqual(w.errors,[])
        self.assertEqual((self.folder/'Fixture'/'0.png').read_bytes(),PNG)
        file_request=next(kwargs for url,kwargs in session.calls if url==FILE)
        self.assertEqual(file_request['headers']['Range'],'bytes=40-')
        self.assertEqual(file_request['headers']['If-Range'],'"fixture"')
        self.assertEqual(self.journal.load(w.job_id)['status'],'completed')
        self.assertEqual(self.journal.list_recoverable(),[])

    def test_ignored_or_invalid_range_restarts_safely_instead_of_appending(self):
        for mode in ('full','bad_range','416'):
            with self.subTest(mode=mode):
                first,job=self.partial()
                w=self.worker(first.job_id); self.run_worker(w,Session(w,mode))
                self.assertFalse(w.errors,w.errors)
                self.assertEqual(w.success,[1])
                target=next(p for p in (self.folder/'Fixture').glob('*.png') if p.read_bytes()==PNG)
                self.assertEqual(target.stat().st_size,len(PNG))

    def test_resume_skips_verified_complete_files_and_counts_the_whole_task(self):
        second=FILE.replace('one.png','two.png').replace('a'*64,'b'*64)
        first,job=self.partial(urls=[FILE,second])
        self.assertEqual(job['saved_count'],1)
        w=self.worker(first.job_id); session=Session(w,'range',file_urls=[FILE,second])
        self.run_worker(w,session)
        self.assertEqual(w.success,[2]); self.assertFalse(w.errors)
        self.assertNotIn(FILE,[url for url,kwargs in session.calls])
        self.assertEqual(len(list((self.folder/'Fixture').glob('*.png'))),2)

    def test_manual_stop_removes_only_owned_incomplete_files(self):
        w=self.worker()
        def stop():
            (self.folder/'Fixture'/'user-notes.txt').write_text('keep',encoding='utf-8')
            w.interrupted=True
        session=Session(w,interrupt_file=FILE)
        original=session.get
        def get(url,**kwargs):
            response=original(url,**kwargs)
            if url==FILE: response.interrupt=stop
            return response
        session.get=get; self.run_worker(w,session)
        self.assertEqual(self.journal.load(w.job_id)['status'],'cancelled')
        self.assertEqual((self.folder/'Fixture'/'user-notes.txt').read_text(),'keep')
        self.assertFalse(list(self.folder.rglob('*.part')))
        self.assertFalse(list(self.folder.rglob('*.png')))
        self.assertEqual(self.journal.list_recoverable(),[])

    def test_discard_removes_checkpoint_and_own_partial_but_keeps_completed_original(self):
        second=FILE.replace('one.png','two.png').replace('a'*64,'b'*64)
        first,job=self.partial(urls=[FILE,second])
        completed=self.folder/'Fixture'/'0.png'
        partial=Path(job['files'][-1]['part'])
        self.journal.discard(first.job_id)
        self.assertEqual(completed.read_bytes(),PNG); self.assertFalse(partial.exists())
        with self.assertRaises(KeyError): self.journal.load(first.job_id)

    def test_corrupt_and_outside_paths_are_rejected_without_touching_user_files(self):
        first,job=self.partial()
        outside=self.root/'outside.part'; outside.write_bytes(b'private')
        job['files'][0]['part']=str(outside)
        with self.assertRaises(ValueError): self.journal.save(job)
        with self.journal._db:
            self.journal._db.execute('UPDATE jobs SET document=? WHERE id=?',('{broken',first.job_id))
        self.assertEqual(self.journal.list_recoverable(),[])
        self.journal.discard(first.job_id)
        self.assertEqual(outside.read_bytes(),b'private')

    def test_html_response_is_never_completed_as_an_image(self):
        w=self.worker(); self.run_worker(w,Session(w,'html'))
        self.assertTrue(w.errors); self.assertFalse(w.success)
        self.assertEqual(self.journal.load(w.job_id)['status'],'failed')
        self.assertFalse(list(self.folder.rglob('*.png')))

    def test_large_manifest_records_are_stored_separately_from_task_metadata(self):
        import json
        worker,job=self.partial()
        with self.journal._lock:
            metadata=self.journal._db.execute('SELECT document FROM jobs WHERE id=?',(worker.job_id,)).fetchone()[0]
            records=self.journal._db.execute('SELECT count(*) FROM file_records WHERE job_id=?',(worker.job_id,)).fetchone()[0]
        self.assertNotIn('files',json.loads(metadata))
        self.assertNotIn('posts',json.loads(metadata))
        self.assertEqual(records,1)
        self.assertLess(len(metadata),2048)

    def test_partial_symlink_is_rejected_before_any_target_can_be_deleted(self):
        worker,job=self.partial()
        part=Path(job['files'][0]['part'])
        original=Path.is_symlink
        with patch.object(Path,'is_symlink',lambda path: path==part or original(path)):
            with self.assertRaisesRegex(ValueError,'symbolic'):
                self.journal.load(worker.job_id)
        self.assertEqual(part.read_bytes(),PNG[:40])

    def test_flat_cross_post_direct_name_collisions_keep_both_titles_and_suffix(self):
        second_post=PROFILE+'/post/two'
        second_file=FILE.replace('a'*64,'b'*64)
        w=self.worker(by_title=False)
        session=Session(w)
        original=session.get
        def get(url,**kwargs):
            if url==PROFILE:
                return Response((''.join('<article class="post-card"><a href="'+post+'">post</a><time class="timestamp" datetime="'+date+'"></time><div class="post-card__header">'+title+'</div></article>'
                    for post,date,title in [(POST,'2026-11-02','Alpha'),(second_post,'2026-11-01','Beta')])).encode())
            if url in (POST,second_post):
                title='Alpha' if url==POST else 'Beta'; file=FILE if url==POST else second_file
                return Response(('<h1 class="post__title"><span>'+title+'</span></h1><a class="post__attachment-link" href="'+file+'" download="same.png">Download same.png</a>').encode())
            if url==second_file: return Response(PNG,headers={'content-length':str(len(PNG))})
            return original(url,**kwargs)
        session.get=get
        self.run_worker(w,session)
        self.assertFalse(w.errors,w.errors); self.assertEqual(w.success,[2])
        self.assertEqual({path.name for path in self.folder.iterdir()},{'(Alpha) - same.png','(Beta) - same.png'})

    def test_flat_numbering_keeps_each_extension_and_start_number(self):
        w=self.worker(by_title=False,rename=True)
        w.start_number=7
        self.run_worker(w,Session(w))
        self.assertFalse(w.errors,w.errors)
        self.assertEqual((self.folder/'7.png').read_bytes(),PNG)

    def test_numbering_drops_middle_filename_dots_but_keeps_archive_suffixes(self):
        for filename,expected,payload in [('picture.v2.png','3.png',PNG),
                ('artist.draft.tar.gz','3.tar.gz',b'local compressed archive fixture')]:
            with self.subTest(filename=filename):
                folder=self.root/filename.replace('.','_')
                worker=app.Worker(folder,PROFILE,'newest',False,journal=self.journal,rename_order=True,start_number=3)
                worker.interrupted=False; worker.isInterruptionRequested=lambda:False
                worker.wait_interruptibly=lambda seconds:None
                success=[]; errors=[]; worker.finished_ok.connect(success.append); worker.failed.connect(errors.append)
                session=Session(worker); original=session.get
                def get(url,**kwargs):
                    if url==POST:
                        return Response(('<h1 class="post__title"><span>Fixture</span></h1><div class="post__files"><a class="fileThumb" href="'+FILE+'" download="'+filename+'">image</a></div>').encode())
                    if url==FILE:return Response(payload,headers={'content-length':str(len(payload))})
                    return original(url,**kwargs)
                session.get=get
                self.run_worker(worker,session)
                self.assertFalse(errors,errors); self.assertEqual(success,[1])
                self.assertEqual((folder/expected).read_bytes(),payload)

    def test_crash_after_final_move_recovers_without_downloading_the_file_again(self):
        from resumable_worker import move_exclusive
        w=self.worker(); session=Session(w)
        def move_then_crash(source,target):
            move_exclusive(source,target)
            raise SystemExit('Simulated process termination after rename, before checkpoint commit')
        with patch('resumable_worker.move_exclusive',side_effect=move_then_crash):
            with self.assertRaises(SystemExit): self.run_worker(w,session)
        job=self.journal.load(w.job_id)
        self.assertEqual(job['status'],'running')
        self.assertEqual(job['files'][0]['status'],'ready')
        resumed=self.worker(w.job_id); second=Session(resumed)
        self.run_worker(resumed,second)
        self.assertEqual(resumed.success,[1]); self.assertFalse(resumed.errors)
        self.assertNotIn(FILE,[url for url,kwargs in second.calls])

    def test_incremental_record_save_does_not_rewrite_other_file_records(self):
        worker,job=self.partial()
        record=job['files'][0]; record['etag']='"new-test-validator"'
        statements=[]
        self.journal._db.set_trace_callback(statements.append)
        try: self.journal.save(job,dirty_records=[record])
        finally: self.journal._db.set_trace_callback(None)
        self.assertEqual(sum('INSERT INTO file_records' in sql for sql in statements),1)
        self.assertFalse(any('DELETE FROM file_records' in sql for sql in statements))
        self.assertFalse(any('INSERT INTO post_lists' in sql for sql in statements))

    def test_failed_new_album_does_not_remove_a_previously_finished_album(self):
        second_post=PROFILE+'/post/two'; second_file=FILE.replace('a'*64,'b'*64)
        w=self.worker(); session=Session(w); original=session.get
        def get(url,**kwargs):
            if url==PROFILE:
                return Response((''.join('<article class="post-card"><a href="'+post+'">post</a><time class="timestamp" datetime="'+date+'"></time><div class="post-card__header">'+title+'</div></article>'
                    for post,date,title in [(POST,'2026-11-02','Alpha'),(second_post,'2026-11-01','Beta')])).encode())
            if url in (POST,second_post):
                title='Alpha' if url==POST else 'Beta'; file=FILE if url==POST else second_file
                return Response(('<h1 class="post__title"><span>'+title+'</span></h1><div class="post__files"><a class="fileThumb" href="'+file+'" download="same.png">image</a></div>').encode())
            if url==second_file: return Response(b'<html>limited</html>',headers={'content-type':'text/html'})
            return original(url,**kwargs)
        session.get=get; self.run_worker(w,session)
        self.assertTrue(w.errors)
        self.assertEqual((self.folder/'Alpha'/'same.png').read_bytes(),PNG)
        self.assertFalse((self.folder/'Beta').exists())
        self.assertEqual(self.journal.load(w.job_id)['saved_count'],1)

    def test_multi_gigabyte_progress_is_not_truncated_to_a_qt_32bit_integer(self):
        worker=self.worker(); progress=[]
        worker.progress.connect(lambda name,received,total:progress.append((received,total)))
        worker.progress.emit('large.zip',2**34,2**35)
        self.assertEqual(progress,[(2**34,2**35)])

    def test_real_background_thread_delivers_checkpoint_completion_to_gui(self):
        worker=self.worker(); session=Session(worker)
        with patch('resumable_worker.requests.Session',return_value=session):
            worker.start()
            deadline=time.monotonic()+5
            while worker.isRunning() and time.monotonic()<deadline:
                self.qt.processEvents(); time.sleep(.002)
            self.assertTrue(worker.wait(1000),'Offline worker did not stop')
            self.qt.processEvents()
        self.assertEqual(worker.success,[1]); self.assertFalse(worker.errors)
        self.assertEqual(self.journal.load(worker.job_id)['status'],'completed')


if __name__=='__main__': unittest.main()
