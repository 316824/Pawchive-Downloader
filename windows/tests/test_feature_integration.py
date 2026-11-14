"""Offline checks for recommendation/artist workflows crossing the main window."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings,QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication
import download_pawchive_images as desktop
from download_journal import DownloadJournal
from queue_operations import QueuePreparationWorker
from subscriptions import parse_site_datetime

PROFILE='https://pawchive.pw/fanbox/user/123'


class FeatureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_feature_test_')
        self.root=Path(self.temp.name)
        self.window=desktop.App(QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat))
        self.window.path.setText(str(self.root))

    def tearDown(self):
        self.window.busy=False; self.window.close(); self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.qt.processEvents(); self.temp.cleanup()

    def test_artist_download_always_confirms_and_forces_preselection_with_cutoff(self):
        plan=dict(folder=str(self.root),by_title=False,rename_order=True,content_mode='images')
        with patch('download_plan.ask_download_plan',return_value=plan),patch.object(self.window,'download') as start:
            self.assertTrue(self.window.confirm_artist_download(PROFILE,updates_after=100))
        start.assert_called_once()
        self.assertTrue(self.window.select_posts_check.isChecked())
        self.assertEqual(self.window._scan_after_timestamp,100)
        self.assertTrue(self.window.rename_check.isChecked())
        self.assertFalse(self.window.title_check.isChecked())
        self.assertEqual(self.window.content_combo.currentData(),'images')

    def test_recommended_single_post_is_images_only_and_not_an_artist_download(self):
        plan=dict(folder=str(self.root),by_title=True,rename_order=True,content_mode='images')
        with patch('download_plan.ask_download_plan',return_value=plan),patch.object(self.window,'_start_queue',return_value=True) as start:
            self.assertTrue(self.window.confirm_recommended_download(dict(url=PROFILE+'/post/9',title='Post'),'post'))
        self.assertEqual(start.call_args.args[0][0]['content_mode'],'images')
        self.assertFalse(self.window._next_record_artist)
        with closing(DownloadJournal(self.root/'journal')) as journal:
            config=dict(folder=str(self.root),profile=PROFILE,by_title=True,order='newest',
                queue_task=True,queue_rename_order=True,rename_order=False,record_artist=False)
            worker=QueuePreparationWorker(journal,start.call_args.args[0],config)
            results=[]; worker.result.connect(results.append); worker.run()
            job=journal.load(results[0][0]['metadata']['job_id'])
            self.assertFalse(job['config']['record_artist'])
            self.assertFalse(results[0][0]['metadata']['_worker_config']['record_artist'])

    def test_update_scan_filters_by_numeric_date_before_fetching_old_post_details(self):
        calls=[]
        class Response:
            def __init__(self,text): self.text=text
            def raise_for_status(self): pass
        class Session:
            headers={}
            def get(self,url,**kwargs):
                calls.append(url)
                if url==PROFILE:
                    return Response(''.join(f'<article class="post-card"><a href="{PROFILE}/post/{n}"></a><div class="post-card__header">{n}</div><time datetime="{d}"></time></article>'
                        for n,d in ((1,'2026-10-19 12:00'),(2,'2026-10-9 12:00'))))
                return Response('<div class="post__files"></div>')
            def close(self): pass
        worker=desktop.ProfileScanWorker(PROFILE,self.root,after_ts=parse_site_datetime('2026-10-10'))
        result=[]; errors=[]; worker.result.connect(result.append); worker.failed.connect(errors.append)
        with patch.object(desktop.requests,'Session',Session): worker.run()
        self.assertFalse(errors)
        self.assertEqual([i['url'] for i in result[0]],[PROFILE+'/post/1'])
        self.assertNotIn(PROFILE+'/post/2',calls)
        self.assertEqual(result[0][0]['date'],'2026-10-19 12:00')
