"""Offline pending-update, daily-check and disposable-preview regressions."""
import base64
import ctypes
from datetime import date, datetime, timezone
import os
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
if os.name=='nt': ctypes.windll.kernel32.SetErrorMode(0x8003)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject,QThread,Signal,Slot,QCoreApplication,QEvent
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication,QWidget,QComboBox,QDialog
from app_state import AppState
from artist_migration import export_code,import_code,decode_code,MigrationError
from subscriptions import SubscriptionStore,SubscriptionCheckWorker,fetch_latest,parse_site_datetime
import subscription_preview as preview

PROFILE='https://pawchive.pw/fanbox/user/1234'


def post(number,stamp):
    return dict(url=PROFILE+f'/post/{number}',title=f'Collection {number}',date='',
                timestamp=stamp,image_url=f'https://file.pawchive.pw/thumb/{number}.jpg')


class Response:
    def __init__(self,text):self.text=text;self.closed=False
    def raise_for_status(self):pass
    def close(self):self.closed=True


class Session:
    def __init__(self,*pages):self.pages=list(pages);self.calls=[];self.headers={}
    def get(self,url,**_):self.calls.append(url);return self.pages.pop(0)


def page(entries):
    return '<a class="user-header__profile"><span itemprop="name">Artist A</span></a>'+''.join(
        f'<article class="post-card"><a href="{entry["url"]}"></a>'
        f'<div class="post-card__header">{entry["title"]}</div>'
        f'<time datetime="{datetime.fromtimestamp(entry["timestamp"],timezone.utc).isoformat()}"></time>'
        f'<img class="post-card__image" data-src="{entry["image_url"]}"></article>' for entry in entries)


class ManualImage(QObject):
    result=Signal(str,object,str)
    finished=Signal()
    created=[]
    def __init__(self,url,parent=None):
        super().__init__(parent);self.url=url;self.cancelled=False;self.created.append(self)
    def start(self):pass
    def requestInterruption(self):self.cancelled=True
    def wait(self):pass
    def finish(self,payload=None):
        if payload is not None:self.result.emit(self.url,payload,'')
        self.finished.emit()


class Parent(QWidget):
    lang='简体中文';background_store=None;_dark=True
    _subscription_check_running=False
    def __init__(self,state,cache):
        super().__init__();self.subscription_store=SubscriptionStore(state);self.cache_dir=cache
        self.palette_tokens=dict(panel='#121722',card='#171d29',line='#303849',
            text='#edf3ff',muted='#8995b0',accent='#a5b3fa',button='#a5b3fa',
            button_text='#11151c',hover='#272d41')
        self.content_combo=QComboBox(self);self.content_combo.addItem('all','all')
        self.queued=[];self.plans=[];self.badges=0
    def refresh_subscription_badge(self):self.badges+=1
    def _apply_download_plan(self,*values):self.plans.append(values)
    def _start_queue(self,entries,profile):self.queued.append((entries,profile));return True


class SubscriptionUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_subscription_test_')
        self.root=Path(self.temp.name);self.state=AppState(self.root/'state')
        self.store=SubscriptionStore(self.state);self.dialogs=[];self.parents=[]
        ManualImage.created=[]
        self.images=patch.object(preview,'ImageBytesWorker',ManualImage);self.images.start()
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test'))
        self.requests=self.network.start()
    def tearDown(self):
        for dialog in self.dialogs:dialog.reject();dialog.deleteLater()
        for pool in tuple(preview._POOLS):
            pool.close()
            for worker in tuple(pool.workers):worker.finished.emit()
        for parent in self.parents:parent.deleteLater()
        self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.state.close();self.images.stop();self.requests.assert_not_called();self.network.stop()
        self.temp.cleanup()
    def parent(self):
        parent=Parent(self.state,self.root);self.parents.append(parent);return parent
    def dialog(self,parent=None):
        dialog=preview.UpdatePreviewDialog(parent or self.parent(),PROFILE)
        self.dialogs.append(dialog);return dialog
    def seed(self):
        self.store.add(PROFILE,baseline_ts=100)
        self.store.set_baseline(PROFILE,300,posts=[post(3,300),post(2,200)])
    def pump(self,condition,timeout=3):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            self.qt.processEvents()
            if condition():return True
            time.sleep(.002)
        return condition()

    def test_initial_remote_read_sets_baseline_without_notifying_old_posts(self):
        self.store.add(PROFILE)
        self.store.set_baseline(PROFILE,300,posts=[post(3,300),post(2,200)])
        record=self.store.get(PROFILE)
        self.assertEqual(record['baseline_ts'],300);self.assertFalse(record['pending_posts'])
        self.assertFalse(record['unread']);self.assertEqual(record['last_check_date'],date.today().isoformat())

    def test_incremental_checks_union_deduplicate_and_keep_unselected_older_posts(self):
        self.seed();self.store.set_baseline(PROFILE,400,posts=[post(4,400),post(3,300)])
        self.assertEqual(self.store.get(PROFILE)['update_count'],3)
        self.store.advance_download(PROFILE,400,post_urls=[post(4,400)['url']])
        record=self.store.get(PROFILE)
        self.assertEqual(record['baseline_ts'],400);self.assertEqual(record['update_count'],2)
        self.assertEqual([item['timestamp'] for item in record['pending_posts']],[300,200])
        self.assertTrue(record['unread'])
        self.store.set_baseline(PROFILE,400,posts=[post(4,400),post(3,300),post(2,200)])
        self.assertEqual(self.store.get(PROFILE)['update_count'],2)
        self.store.advance_download(PROFILE,300,post_urls=[post(3,300)['url']])
        self.store.advance_download(PROFILE,200,post_urls=[post(2,200)['url']])
        record=self.store.get(PROFILE)
        self.assertEqual(record['baseline_ts'],400);self.assertFalse(record['unread'])
        self.assertEqual(record['update_count'],0)

    def test_explicit_empty_invalid_completion_lists_do_not_clear_or_advance(self):
        self.seed();before=self.store.get(PROFILE)
        for urls in ([],['https://evil.example/post/3'],[PROFILE+'/post/..'],post(3,300)['url']):
            self.store.advance_download(PROFILE,999,post_urls=urls)
            self.assertEqual(self.store.get(PROFILE),before)
        self.store.advance_download(PROFILE,300)
        self.assertEqual(self.store.get(PROFILE)['update_count'],2)

    def test_pending_state_strips_local_previews_and_invalid_network_targets(self):
        self.store.add(PROFILE,baseline_ts=100)
        self.store.set_baseline(PROFILE,300,posts=[{**post(3,300),'image':'C:/private.png',
            'image_url':'https://user:secret@file.pawchive.pw/x.jpg'},
            {**post(2,200),'url':PROFILE+'/post/..'}])
        pending=self.store.get(PROFILE)['pending_posts']
        self.assertEqual(len(pending),1);self.assertIsNone(pending[0]['image'])
        self.assertEqual(pending[0]['image_url'],'')

    def test_feed_reads_only_incremental_pages_and_never_post_details(self):
        entries=[post(n,1000+n) for n in range(100,50,-1)]
        responses=[Response(page(entries)),Response(page([post(50,1050),post(49,1049)]))]
        session=Session(*responses)
        with patch('subscriptions.time.sleep') as sleep:
            result=fetch_latest(PROFILE,session=session,baseline_ts=1050)
        self.assertEqual(session.calls,[PROFILE,PROFILE+'?o=50'])
        self.assertTrue(all(response.closed for response in responses))
        self.assertEqual(len(result['posts']),50);self.assertEqual(result['latest_ts'],1100)
        sleep.assert_called_once();self.assertTrue(1<=sleep.call_args.args[0]<=3)
        self.assertEqual(result['name'],'Artist A')

    def test_new_subscription_fetch_reads_one_page_and_cancel_reads_none(self):
        session=Session(Response(page([post(n,1000+n) for n in range(100)])))
        result=fetch_latest(PROFILE,session=session)
        self.assertEqual(len(session.calls),1);self.assertEqual(len(result['posts']),100)
        with self.assertRaisesRegex(OSError,'cancelled'):
            fetch_latest(PROFILE,session=session,interrupted=lambda:True)
        self.assertEqual(len(session.calls),1)

    def test_daily_success_is_skipped_but_failed_check_remains_retryable(self):
        self.seed();results=[]
        worker=SubscriptionCheckWorker(self.store);worker.finished_all.connect(results.append)
        with patch('subscriptions.fetch_latest') as fetch:worker.run()
        fetch.assert_not_called();self.assertTrue(results[0][0]['skipped'])
        records=self.state.get_setting('artist_subscriptions');records[0]['last_check_date']='2020-01-01'
        self.state.set_setting('artist_subscriptions',records)
        with patch('subscriptions.fetch_latest',side_effect=OSError('offline')):worker.run()
        self.assertEqual(self.store.get(PROFILE)['last_check_date'],'2020-01-01')
        self.assertIn('offline',results[-1][0]['error'])
        with patch('subscriptions.fetch_latest',return_value=dict(profile=PROFILE,latest_ts=400,posts=[post(4,400)])):
            worker.run()
        self.assertEqual(self.store.get(PROFILE)['last_check_date'],date.today().isoformat())
        self.assertEqual(self.store.get(PROFILE)['update_count'],3)

    def test_worker_has_three_request_limit_and_gui_thread_result_delivery(self):
        for n in range(6):self.store.add(PROFILE+str(n),baseline_ts=100)
        gate=threading.Event();three=threading.Event();lock=threading.Lock();active=0;maximum=0
        def fetch(profile,**_):
            nonlocal active,maximum
            with lock:
                active+=1;maximum=max(maximum,active)
                if active==3:three.set()
            gate.wait(2)
            with lock:active-=1
            return dict(profile=profile,latest_ts=100,posts=[])
        class Probe(QObject):
            def __init__(self):super().__init__();self.threads=[]
            @Slot(str,object)
            def progress(self,*_):self.threads.append(QThread.currentThread())
        probe=Probe();worker=SubscriptionCheckWorker(self.store,seed=1)
        worker.progress.connect(probe.progress)
        with patch('subscriptions.fetch_latest',side_effect=fetch),patch('subscriptions.random.Random.uniform',return_value=.001) as delay:
            worker.start()
            try:
                self.assertTrue(three.wait(1));gate.set()
                self.assertTrue(self.pump(lambda:not worker.isRunning() and len(probe.threads)==6))
            finally:gate.set();worker.stop();worker.wait(3000)
        self.assertEqual(maximum,3);self.assertEqual(delay.call_count,3)
        self.assertTrue(all(thread is self.qt.thread() for thread in probe.threads))

    def test_browsing_and_selecting_preview_never_marks_pending_posts_read(self):
        self.seed();before=self.store.get(PROFILE);dialog=self.dialog()
        self.assertEqual(len(dialog.picker.titles),2)
        dialog.picker._select_all(False);dialog.picker._set_selected(1,True)
        dialog.accept()
        self.assertEqual([entry['url'] for entry in dialog.download_entries],[post(2,200)['url']])
        self.assertEqual(dialog.download_entries[0]['metadata']['subscription_profile'],PROFILE)
        self.assertEqual(self.store.get(PROFILE),before)
        self.assertFalse(dialog.pool.root.exists())

    def test_cover_requests_are_bounded_deferred_and_late_bytes_cannot_recreate_cache(self):
        parent=self.parent();parent._subscription_check_running=True
        pool=preview._PreviewPool(parent);root=pool.root;received=[];pool.cover.connect(lambda *v:received.append(v))
        pool.request([f'https://file.pawchive.pw/thumb/{n}.jpg' for n in range(8)])
        self.assertEqual(len(ManualImage.created),0)
        parent._subscription_check_running=False;pool._pump()
        self.assertEqual(len(ManualImage.created),3)
        first=ManualImage.created[0];pool.close()
        self.assertFalse(root.exists());self.assertTrue(first.cancelled)
        image=QImage(64,64,QImage.Format.Format_RGB32);image.fill(QColor('#abcd34'))
        image.save(str(self.root/'image.png'));payload=(self.root/'image.png').read_bytes()
        first.result.emit(first.url,payload,'');self.assertFalse(root.exists());self.assertFalse(received)
        for worker in tuple(pool.workers):worker.finished.emit()
        self.assertNotIn(pool,preview._POOLS)

    def test_confirm_queues_only_selected_posts_preserving_their_content_choices(self):
        self.seed();parent=self.parent();before=self.store.get(PROFILE)
        def select(dialog):
            dialog.picker._select_all(False);dialog.picker._set_selected(1,True)
            dialog.picker.titles[1]['content_mode']='attachments'
            dialog.accept();return QDialog.DialogCode.Accepted
        with patch.object(preview.UpdatePreviewDialog,'exec',new=select),patch(
                'download_plan.ask_download_plan',return_value={'content_mode':'all'}) as plan:
            self.assertTrue(preview.open_update_preview(parent,PROFILE))
        self.assertEqual(len(parent.queued),1)
        entries,profile=parent.queued[0]
        self.assertEqual(profile,PROFILE);self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['url'],post(2,200)['url'])
        self.assertEqual(entries[0]['content_mode'],'attachments')
        self.assertEqual(self.store.get(PROFILE),before);plan.assert_called_once()

    def test_cancelled_settings_after_preview_does_not_queue_or_clear(self):
        self.seed();parent=self.parent();before=self.store.get(PROFILE)
        def select(dialog):
            image=QImage(64,64,QImage.Format.Format_RGB32);image.fill(QColor('#abcd34'))
            path=dialog.pool.root/'cover.png';image.save(str(path))
            dialog._cover_ready(post(3,300)['image_url'],str(path))
            dialog.accept()
            self.assertEqual(len(list(self.root.glob('subscription-*.png'))),1)
            return QDialog.DialogCode.Accepted
        with patch.object(preview.UpdatePreviewDialog,'exec',new=select),patch(
                'download_plan.ask_download_plan',return_value=None):
            self.assertFalse(preview.open_update_preview(parent,PROFILE))
        self.assertFalse(parent.queued);self.assertEqual(self.store.get(PROFILE),before)
        self.assertFalse(list(self.root.glob('subscription-*.png')))

    def test_migration_roundtrip_preserves_pending_day_and_completed_urls(self):
        self.seed();self.store.advance_download(PROFILE,300,post_urls=[post(3,300)['url']])
        record=self.store.get(PROFILE);code=export_code(self.state)
        exported=decode_code(code)[0]['subscription']
        self.assertEqual(exported['last_check_date'],date.today().isoformat())
        self.assertEqual(exported['update_count'],1)
        self.assertNotIn('image',exported['pending_posts'][0])
        target=AppState(self.root/'target')
        try:
            import_code(target,code);import_code(target,code)
            restored=SubscriptionStore(target).get(PROFILE)
            for key in ('last_check_date','pending_posts','downloaded_post_urls','update_count','unread','baseline_ts'):
                self.assertEqual(restored[key],record[key])
        finally:target.close()

    def test_migration_merge_unions_pending_but_completed_urls_win(self):
        self.seed();code=export_code(self.state)
        self.store.set_baseline(PROFILE,400,posts=[post(4,400)])
        self.store.advance_download(PROFILE,300,post_urls=[post(3,300)['url']])
        import_code(self.state,code)
        record=self.store.get(PROFILE)
        self.assertEqual([item['url'] for item in record['pending_posts']],[post(4,400)['url'],post(2,200)['url']])
        self.assertEqual(record['update_count'],2)

    def test_legacy_migration_is_accepted_and_new_pending_fields_are_strict(self):
        self.seed();artist=decode_code(export_code(self.state))[0]
        def envelope(item):
            raw=json.dumps({'version':1,'artists':[item]}).encode()
            body=zlib.compress(raw)
            return 'PAWCHIVE_V1:'+base64.b64encode(body).decode()+f':{zlib.crc32(body)&0xffffffff:08X}'
        legacy={**artist,'subscription':{key:artist['subscription'][key] for key in
            ('baseline_ts','latest_ts','last_checked_ts','enabled','initialized','unread')}}
        self.assertEqual(decode_code(envelope(legacy))[0]['subscription']['pending_posts'],[])
        for changed in (
            {'last_check_date':'invalid'},
            {'downloaded_post_urls':['https://evil.example/post/1']},
            {'pending_posts':[{**artist['subscription']['pending_posts'][0],'image':'C:/private.png'}]},
            {'pending_posts':[{**artist['subscription']['pending_posts'][0],'image_url':'file:///C:/private.png'}]},
        ):
            invalid={**artist,'subscription':{**artist['subscription'],**changed}}
            with self.subTest(fields=list(changed)),self.assertRaises(MigrationError):
                decode_code(envelope(invalid))


if __name__=='__main__':unittest.main()
