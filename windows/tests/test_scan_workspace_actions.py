"""Scan opt-in, terminal taskbar state and safe workspace release regressions."""
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QSettings,QTimer,QThread
from PySide6.QtWidgets import QApplication
from download_scheduler import DownloadScheduler
import download_pawchive_images as desktop


class ScanWorkspaceActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_workspace_actions_')
        self.root=Path(self.temp.name);self.dialogs=[]
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No network in UI actions tests'))
        self.network.start()
        self.window=desktop.App(QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat))
        self.window.path.setText(str(self.root));self.window.show();self.qt.processEvents()

    def tearDown(self):
        from shiboken6 import isValid
        for dialog in self.dialogs:
            if not isValid(dialog):continue
            dialog._allow_close=True;dialog.close();dialog.deleteLater()
        self.window.busy=False
        self.window.close();self.window.deleteLater()
        self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.network.stop();self.temp.cleanup()

    def wait_until(self,condition,timeout=2):
        deadline=time.monotonic()+timeout
        while not condition() and time.monotonic()<deadline:
            self.qt.processEvents();time.sleep(.002)
        self.assertTrue(condition())

    def splash(self):
        dialog=desktop.ScanAnimation(self.window)
        dialog.set_language(self.window.lang);dialog.show();self.dialogs.append(dialog)
        self.qt.processEvents();return dialog

    def scheduler(self,state='completed'):
        scheduler=DownloadScheduler(lambda _:None,self.window)
        scheduler.add_task(dict(id='one',title='Album',url='https://pawchive.pw/fanbox/user/1/post/2',
                                formats=['.jpg'],file_count=1))
        task=scheduler.task('one');task.state=state;task.completed_files=1 if state=='completed' else 0
        task.progress=1 if state=='completed' else 0
        self.window.queue_scheduler=scheduler
        for name,slot in (('task_updated',self.window._queue_task_updated),
                          ('task_finished',self.window._queue_task_finished),
                          ('task_recycled',self.window._queue_task_recycled),
                          ('queue_changed',self.window._queue_view),
                          ('became_idle',self.window._queue_worker_idle)):
            getattr(scheduler,name).connect(slot)
        self.window.download_queue.set_items([task.public_copy()])
        self.qt.processEvents();return scheduler

    def test_scan_checkbox_is_workspace_control_localized_and_remembered(self):
        first=self.splash()
        self.assertIsInstance(first.auto_queue_check,desktop.GlowCheckBox)
        self.assertFalse(first.auto_queue_check.isChecked())
        first.auto_queue_check.setChecked(True)
        self.assertTrue(self.window.settings.value('download/scan_auto_confirm',False,type=bool))
        second=self.splash();self.assertTrue(second.auto_queue_check.isChecked())
        for language in ('简体中文','繁體中文','English','日本語'):
            second.set_language(language)
            self.assertEqual(second.auto_queue_check.text(),desktop.ui_text(language,'扫描结束后自动加入下载队列'))

    def test_automatic_completion_dissolves_without_overlay_or_user_click(self):
        dialog=self.splash();calls=[]
        dialog.show_completion(5,lambda:calls.append(True),automatic=True)
        self.assertIsNone(dialog._completion_overlay)
        self.assertTrue(dialog.game._finishing)
        self.assertFalse(calls)
        dialog._auto_completion_timer.setInterval(30)
        self.wait_until(lambda:bool(calls))
        self.assertEqual(calls,[True]);self.assertFalse(dialog.isVisible())
        dialog.finish_completion();self.assertEqual(calls,[True])

    def test_manual_completion_keeps_click_overlay(self):
        dialog=self.splash();calls=[]
        dialog.show_completion(5,lambda:calls.append(True))
        self.assertIsNotNone(dialog._completion_overlay)
        self.assertFalse(dialog._auto_completion_timer.isActive())
        self.qt.processEvents();self.assertFalse(calls)
        dialog.finish_completion();self.assertEqual(calls,[True])

    def test_automatic_completion_survives_hidden_paused_game(self):
        dialog=self.splash();calls=[]
        dialog.pause_scan();dialog.hide()
        dialog.show_completion(5,lambda:calls.append(True),automatic=True)
        dialog.game.stop()
        dialog._auto_completion_timer.setInterval(30)
        self.wait_until(lambda:bool(calls))
        self.assertEqual(calls,[True])
        self.assertFalse(dialog._auto_completion_timer.isActive())

    def test_auto_scan_result_selects_all_and_keeps_each_entry_options(self):
        for explicit in (True,False):
            with self.subTest(explicit=explicit):
                self.window.select_posts_check.setChecked(explicit)
                dialog=self.splash();self.window.splash=dialog
                dialog.auto_queue_check.setChecked(explicit)
                self.window.scanner=SimpleNamespace(isRunning=lambda:False)
                self.window.profile.setText('https://pawchive.pw/fanbox/user/1')
                titles=[dict(title='One',url='https://pawchive.pw/fanbox/user/1/post/1',content_mode='images'),
                        dict(title='Two',url='https://pawchive.pw/fanbox/user/1/post/2',content_mode='attachments')]
                self.window.set_busy_state(True)
                with (patch.object(self.window,'choose_titles',side_effect=AssertionError('Automatic scan must not open picker')),
                      patch.object(self.window,'_start_queue',return_value=True) as start):
                    self.window.scan_finished(titles)
                    dialog._auto_completion_timer.setInterval(30)
                    self.wait_until(lambda:start.called)
                self.assertEqual(start.call_args.args[0],titles)
                self.assertFalse(self.window.busy)

    def test_final_queue_clears_shell_progress_but_keeps_ui_at_one_hundred(self):
        self.scheduler('completed');self.window._queue_completed_files=1
        self.window.taskbar_progress.set_value(100)
        self.window._settle_queue_idle()
        self.assertEqual(self.window.taskbar_progress._state,0)
        self.assertEqual(self.window.progress.value,100)
        self.assertEqual(self.window.percent_label.text(),'100')

    def test_pending_or_paused_work_does_not_clear_taskbar(self):
        scheduler=self.scheduler('paused')
        self.window.taskbar_progress.set_value(45)
        self.window._settle_queue_idle()
        self.assertNotEqual(self.window.taskbar_progress._state,0)
        scheduler.task('one').state='completed'
        self.window._queue_finalizing=True
        self.window._settle_queue_idle()
        self.assertNotEqual(self.window.taskbar_progress._state,0)
        self.window._queue_finalizing=False

    def test_finalizer_reference_keeps_shell_progress_until_released(self):
        self.scheduler('completed')
        self.window.taskbar_progress.set_value(100)
        self.window._queue_finalizer=SimpleNamespace(isRunning=lambda:False)
        self.window._settle_queue_idle()
        self.assertNotEqual(self.window.taskbar_progress._state,0)
        self.window._queue_finalizer=None
        self.window._settle_queue_idle()
        self.assertEqual(self.window.taskbar_progress._state,0)

    def test_partial_queue_reward_only_acknowledges_successful_post_urls(self):
        scheduler=self.scheduler('completed')
        scheduler.add_task(dict(id='pending',title='Pending',url='https://pawchive.pw/fanbox/user/1/post/3'))
        scheduler.task('pending').state='paused'
        self.window.services=SimpleNamespace(store=SimpleNamespace(record_download=lambda _:None),
            task_finished=lambda *args,**kwargs:None)
        with (patch.object(self.window,'_maybe_artist_card') as card,
              patch.object(self.window,'refresh_reward_badge')):
            self.window._settle_queue_idle()
        self.assertEqual(card.call_args.kwargs['completed_post_urls'],[scheduler.task('one').url])
        self.window.services=None

    def test_direct_download_completion_clears_shell_progress(self):
        self.window.taskbar_progress.set_value(100)
        self.window.done(1)
        self.assertEqual(self.window.taskbar_progress._state,0)
        self.assertEqual(self.window.progress.value,100)

    def test_broom_clears_finished_workspace_but_retains_paths_and_settings(self):
        self.scheduler('completed')
        self.window.profile.setText('https://pawchive.pw/fanbox/user/1')
        self.window.log.setPlainText('completed transfer')
        self.window.title_check.setChecked(False);self.window.rename_check.setChecked(False)
        self.window.settings.setValue('last_download_path',str(self.root))
        saved={name:getattr(self.window,name).isChecked() for name in
               ('title_check','rename_check','select_posts_check','artist_folder_check','default_path_check')}
        self.window.progress.setValue(100)
        artifact=self.window.cache_dir/'unused.tmp';artifact.write_bytes(b'cache')
        self.window.clear_cache_files();self.qt.processEvents()
        self.assertFalse(artifact.exists())
        self.assertEqual(self.window.profile.text(),'');self.assertEqual(self.window.log.toPlainText(),'')
        self.assertIsNone(self.window.queue_scheduler)
        self.assertFalse(self.window.download_queue._rows)
        self.assertFalse(self.window.download_queue._row_timer.isActive())
        self.assertEqual(self.window.progress.value,0)
        self.assertEqual(self.window.path.text(),str(self.root))
        self.assertEqual(self.window.settings.value('last_download_path'),str(self.root))
        self.assertEqual(saved,{name:getattr(self.window,name).isChecked() for name in saved})

    def test_unfinished_or_waiting_batch_prevents_workspace_reset(self):
        scheduler=self.scheduler('queued')
        self.window.profile.setText('https://pawchive.pw/fanbox/user/1')
        self.window.log.setPlainText('preserve')
        for state in ('queued','running','pausing','paused','failed'):
            scheduler.task('one').state=state
            self.assertFalse(self.window._reset_finished_workspace())
            self.assertEqual(self.window.log.toPlainText(),'preserve')
        scheduler.task('one').state='completed'
        self.window._batch_artist_queue=['123']
        self.assertFalse(self.window._reset_finished_workspace())
        self.window._batch_artist_queue=[]

    def test_batch_continuation_timer_blocks_reset_even_when_queue_is_empty(self):
        self.scheduler('completed')
        timer=getattr(self.window,'_batch_artist_timer',None)
        if timer is None:
            timer=QTimer(self.window);self.window._batch_artist_timer=timer
        timer.start(10000)
        self.assertFalse(self.window._reset_finished_workspace())
        timer.stop()
        self.assertTrue(self.window._reset_finished_workspace())

    def test_failed_queue_keeps_workspace_while_existing_cache_cleanup_still_runs(self):
        self.scheduler('failed')
        self.window.profile.setText('https://pawchive.pw/fanbox/user/1')
        self.window.log.setPlainText('failed transfer')
        artifact=self.window.cache_dir/'unused.tmp';artifact.write_bytes(b'cache')
        self.window.clear_cache_files()
        self.assertFalse(artifact.exists())
        self.assertEqual(self.window.profile.text(),'https://pawchive.pw/fanbox/user/1')
        self.assertEqual(self.window.log.toPlainText(),'failed transfer')
        self.assertIsNotNone(self.window.queue_scheduler)

    def test_live_worker_keeps_controls_and_cache_alive(self):
        artifact=self.window.cache_dir/'reading.tmp';artifact.write_bytes(b'active')
        self.window.worker=SimpleNamespace(isRunning=lambda:True)
        self.window.profile.setText('https://pawchive.pw/fanbox/user/1')
        self.window.clear_cache_files()
        self.assertTrue(artifact.exists());self.assertTrue(self.window.profile.text())
        del self.window.worker

    def test_background_thread_does_not_keep_completed_queue_or_scanner_alive(self):
        import threading
        from shiboken6 import isValid
        release=threading.Event()
        class BackgroundRequest(QThread):
            def run(self):release.wait(5)
        background=BackgroundRequest(self.window)
        scheduler=self.scheduler('completed')
        splash=self.splash();self.window.splash=splash
        scanner=QThread(self.window);self.window.scanner=scanner
        background.start()
        try:
            self.wait_until(background.isRunning)
            self.assertTrue(self.window._workspace_has_live_threads())
            self.window.clear_cache_files()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            self.assertIsNone(self.window.queue_scheduler)
            self.assertFalse(self.window.download_queue._rows)
            self.assertFalse(isValid(scheduler));self.assertFalse(isValid(scanner))
            self.assertFalse(isValid(splash));self.assertTrue(background.isRunning())
        finally:
            release.set();background.wait(5000);background.deleteLater()

    def test_broom_rapid_clicks_cooldown_and_nested_call_are_single_operation(self):
        self.scheduler('completed')
        nested=[]
        def sweep(**kwargs):nested.append(self.window.clear_cache_files())
        with patch.object(desktop,'cleanup_stale_cache_dirs',side_effect=sweep) as cleanup:
            self.window.clear_cache_files()
            self.assertEqual(nested,[False]);self.assertFalse(self.window.clear_cache.isEnabled())
            artifact=self.window.cache_dir/'second.tmp';artifact.write_bytes(b'keep during cooldown')
            for _ in range(100):self.assertFalse(self.window.clear_cache_files())
            self.assertEqual(cleanup.call_count,1);self.assertTrue(artifact.exists())
            self.window._cache_cleanup_deadline=0
            self.window._cache_cleanup_timer.stop();self.window._release_cache_cleanup_button()
            self.assertTrue(self.window.clear_cache.isEnabled())
            self.window.clear_cache_files()
            self.assertEqual(cleanup.call_count,2);self.assertFalse(artifact.exists())

    def test_broom_flushes_pending_large_queue_rows_and_finished_scan(self):
        scheduler=self.scheduler('completed')
        for index in range(500):
            task=scheduler.add_task(dict(id=f'album-{index}',title='Done',
                url=f'https://pawchive.pw/fanbox/user/1/post/{index+10}',formats=['.jpg']))
            task.state='completed'
        self.window.download_queue.set_items([task.public_copy() for task in scheduler.tasks])
        self.window._queue_view();self.window.clear_cache_files()
        for _ in range(3):
            self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.assertFalse(self.window.download_queue._rows)
        self.assertFalse(self.window.download_queue._pending_items)
        self.assertIsNone(self.window.queue_scheduler)
        self.assertFalse(self.window._queue_view_timer.isActive())

    def test_logs_show_first_with_visible_hide_button_and_unchanged_eye_icon(self):
        self.assertFalse(self.window.logs_hidden)
        self.assertFalse(self.window.logs_card.isHidden())
        self.assertIs(self.window.activity_body.layout().itemAt(0).widget(),self.window.logs_card)
        self.assertEqual(self.window.focus_button.text(),'收起日志')
        icon_key=self.window.focus_button.icon().cacheKey()
        self.window.toggle_logs()
        self.assertTrue(self.window.logs_card.isHidden());self.assertEqual(self.window.focus_button.text(),'显示日志')
        self.assertTrue(self.window.focus_button.isVisible())
        self.window.toggle_logs()
        self.assertFalse(self.window.logs_card.isHidden())
        self.assertEqual(self.window.focus_button.icon().cacheKey(),icon_key)
