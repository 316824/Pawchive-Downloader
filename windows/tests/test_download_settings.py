"""Unicode destinations, settings restarts and history recovery without user data."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QSettings
from PySide6.QtWidgets import QApplication
from app_state import AppState,normalize_snapshot
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal
from download_settings import (browse_directory,create_destination,sanitize_artist_name,
                               artist_name_from_html,CHECKBOX_SETTINGS)
from queue_operations import QueuePreparationWorker
import download_pawchive_images as desktop

PROFILE='https://pawchive.pw/fanbox/user/3439325'


class NoTasks:
    def install(self,*args): raise AssertionError('No scheduler changes')
    def remove(self,*args): raise AssertionError('No scheduler changes')


class DownloadSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_preferences_')
        self.root=Path(self.temp.name);self.windows=[];self.runtimes=[]
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No network'))
        self.network.start()

    def tearDown(self):
        for window in self.windows:
            worker=window._destination_worker
            if worker is not None:
                worker.requestInterruption();worker.wait(5000)
            if window.services:window.services.prepare_exit()
            window._queue_pending_entries=[];window._queue_insert_timer.stop()
            window.busy=False;window.close();window.deleteLater()
        self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        for runtime in self.runtimes:runtime.close()
        self.network.stop();self.temp.cleanup()

    def settings(self):
        return QSettings(str(self.root/'preferences.ini'),QSettings.Format.IniFormat)

    def window(self,services=False):
        runtime=None
        if services:
            state=AppState(self.root/'state')
            runtime=DesktopRuntime(self.qt,state,DownloadJournal(state.data_dir),NoTasks(),tray_enabled=False)
            self.runtimes.append(runtime)
        with patch.object(desktop.App,'start_subscription_check',return_value=None):
            window=desktop.App(self.settings(),services=runtime)
        if runtime:runtime.attach(window);runtime.timer.stop()
        window.path.setText(str(self.root));self.windows.append(window)
        return window

    def wait_until(self,condition):
        deadline=time.monotonic()+5
        while not condition() and time.monotonic()<deadline:
            self.qt.processEvents();time.sleep(.003)
        self.assertTrue(condition())

    def test_sanitizer_preserves_unicode_but_cannot_escape_or_use_reserved_names(self):
        self.assertEqual(sanitize_artist_name('秋宫/白晔:猫\\犬*?"<>|'),'秋宫_白晔_猫_犬______')
        for value in ('CON','con.txt','LPT1','COM¹.txt','..',' . ', '\x00'*3):
            name=sanitize_artist_name(value)
            self.assertNotIn(name.upper().split('.')[0],{'CON','LPT1','COM¹'})
            self.assertNotIn('/',name);self.assertNotIn('\\',name)
            self.assertTrue(name and name not in ('.','..') and not name.endswith(('.', ' ')))
        self.assertEqual(len(sanitize_artist_name('猫'*80)),50)
        self.assertEqual(sanitize_artist_name('日本語 中文'),'日本語 中文')

    def test_artist_folder_is_unicode_child_and_all_probe_files_are_removed(self):
        result=create_destination(self.root,PROFILE,True,'秋宫白晔 / ねこ')
        directory=Path(result['folder'])
        self.assertEqual(directory.parent,self.root)
        self.assertEqual(directory.name,'秋宫白晔 _ ねこ')
        self.assertEqual(list(directory.iterdir()),[])
        self.assertEqual(create_destination(self.root,PROFILE,False)['folder'],str(self.root))

    def test_deep_paths_and_unwritable_roots_report_errors(self):
        with patch('download_settings.writable_directory',return_value=False):
            with self.assertRaisesRegex(OSError,'unavailable'):create_destination(self.root,PROFILE,True,'Name')
        deep=self.root/('a'*80)/('b'*80);deep.mkdir(parents=True)
        with self.assertRaisesRegex(OSError,'too_long'):create_destination(deep,PROFILE,True,'猫'*50)

    def test_artist_junction_cannot_redirect_downloads(self):
        destination=self.root/'Artist';destination.mkdir()
        with patch.object(Path,'is_symlink',return_value=True):
            with self.assertRaisesRegex(OSError,'unavailable'):create_destination(self.root,PROFILE,True,'Artist')

    def test_history_walks_missing_parents_skips_unwritable_directory(self):
        settings=self.settings();parent=self.root/'存在';parent.mkdir()
        settings.setValue('last_download_path',str(parent/'删除的目录'/'更深'))
        self.assertEqual(browse_directory(settings,str(self.root)),str(parent))
        real_access=os.access
        with patch('download_settings.os.access',side_effect=lambda path,mode:False if Path(path)==parent else real_access(path,mode)):
            self.assertEqual(browse_directory(settings,str(self.root)),str(self.root))

    def test_invalid_history_falls_back_without_raising_or_looping(self):
        settings=self.settings();settings.setValue('last_download_path','bad\x00path')
        self.assertEqual(browse_directory(settings,str(self.root)),str(self.root))

    def test_restart_restores_every_checkbox_order_and_content_but_not_start_number(self):
        first=self.window()
        self.assertTrue(first.artist_folder_check.isChecked())
        first.default_path_check.setChecked(True)
        first.title_check.setChecked(False);first.artist_folder_check.setChecked(False)
        first.select_posts_check.setChecked(False);first.rename_check.setChecked(False)
        first.order_combo.setCurrentIndex(1);first.content_combo.setCurrentIndex(2)
        first.settings.setValue('start_number',999);first.settings.sync()
        second=self.window()
        for attribute in CHECKBOX_SETTINGS:
            self.assertEqual(getattr(first,attribute).isChecked(),getattr(second,attribute).isChecked())
        self.assertEqual(second.order_combo.currentIndex(),1)
        self.assertEqual(second.content_combo.currentData(),'attachments')
        self.assertEqual(second.start_number.value(),1)

    def test_history_is_independent_from_default_path_and_remembers_only_success(self):
        window=self.window();window.default_path_check.setChecked(False)
        result=create_destination(self.root,PROFILE,True,'艺术家')
        window._active_download_folder=result['folder']
        self.assertFalse(window.settings.contains('last_download_path'))
        window._remember_download_directory()
        self.assertEqual(window.settings.value('last_download_path'),result['folder'])
        self.assertFalse(window.settings.contains('default_save_path'))
        with patch.object(desktop.QFileDialog,'getExistingDirectory',return_value='') as dialog:
            window.pick()
        self.assertEqual(dialog.call_args.args[2],result['folder'])
        self.assertFalse(self.window().default_path_check.isChecked())

    def test_async_destination_uses_scanned_name_without_another_request(self):
        window=self.window()
        window.scanner=type('Scanned',(),{'profile':PROFILE,'artist_name':'画师の名前'})()
        ready=[];self.assertTrue(window._prepare_download_destination(PROFILE,ready.append))
        self.wait_until(lambda:bool(ready))
        self.assertEqual(Path(ready[0]),self.root/'画师の名前')
        self.assertFalse(window.busy)
        self.assertFalse(window.settings.contains('last_download_path'))

    def test_direct_download_passes_resolved_folder_and_remembers_only_after_file_success(self):
        window=self.window();window.scanner=type('Scanned',(),{'profile':PROFILE,'artist_name':'日本・中文'})()
        with patch.object(desktop,'Worker') as factory:
            worker=factory.return_value;worker.folder=self.root/'日本・中文';worker.isRunning.return_value=False
            window.launch_worker(PROFILE,None)
            self.wait_until(lambda:worker.start.called)
            self.assertEqual(Path(factory.call_args.args[0]),self.root/'日本・中文')
            self.assertTrue(factory.call_args.kwargs['destination_options']['artist_folder'])
            self.assertFalse(window.settings.contains('last_download_path'))
            window.update_transfer('✓ file.jpg',1,1)
            self.assertEqual(window.settings.value('last_download_path'),str(self.root/'日本・中文'))
        window.set_busy_state(False)

    def test_destination_failure_restores_idle_without_creating_a_download(self):
        window=self.window();window.path.setText(str(self.root/'deleted'))
        ready=[];window._prepare_download_destination(PROFILE,ready.append,'Artist')
        self.wait_until(lambda:window._destination_worker is None)
        self.assertFalse(window.busy);self.assertEqual(window.workflow_state,'error')
        self.assertFalse(ready);self.assertFalse(window.settings.contains('last_download_path'))

    def test_queue_file_success_uses_current_root_instead_of_stale_direct_worker(self):
        window=self.window();old=self.root/'old';old.mkdir();current=self.root/'current';current.mkdir()
        window.worker=type('PreviousWorker',(),{'folder':old,'isRunning':lambda self:False})()
        window._active_download_folder=str(current)
        window.update_transfer('✓ queue image.jpg',1,1)
        self.assertEqual(window.settings.value('last_download_path'),str(current))

    def test_cancel_during_folder_preparation_does_not_start_download_or_leave_busy(self):
        window=self.window();started=threading.Event();ready=[]
        def preparation(*args,interrupted):
            started.set()
            while not interrupted():time.sleep(.003)
            raise OSError('cancelled')
        with patch('download_settings.create_destination',side_effect=preparation):
            window._prepare_download_destination(PROFILE,ready.append,'Artist')
            self.wait_until(started.is_set);window.stop_download()
            self.wait_until(lambda:window._destination_worker is None)
        self.assertFalse(ready);self.assertFalse(window.busy)
        self.assertEqual(window.workflow_state,'stopped')

    def test_queue_checkpoint_contains_final_artist_root_and_recovery_keeps_it(self):
        window=self.window(services=True)
        destination=create_destination(self.root,PROFILE,True,'秋宫 白晔')
        window._destination_options=destination
        entries=[dict(url=PROFILE+'/post/1',title='Album',formats=['.jpg'],file_count=1,
            assets=[dict(url='https://file.pawchive.pw/data/aa/bb/'+('a'*64)+'.jpg',name='1.jpg',direct=False,index=0)])]
        with patch.object(window,'_queue_insert_batch',return_value=None):
            self.assertTrue(window._start_queue(entries,PROFILE,_destination=destination['folder']))
            self.wait_until(lambda:window._queue_preparation is None)
        entry=window._queue_scan_entries[0];job=window.services.journal.load(entry['metadata']['job_id'])
        self.assertEqual(job['config']['folder'],destination['folder'])
        self.assertTrue(job['config']['artist_folder'])
        self.assertEqual(job['config']['base_folder'],str(self.root))
        window.set_busy_state(False);window.path.setText(destination['folder'])
        with (patch.object(window,'_prepare_download_destination',side_effect=AssertionError('Do not nest artist folders on resume')),
              patch.object(window,'_queue_insert_batch',return_value=None)):
            self.assertTrue(window._start_queue([entry],PROFILE))
            self.wait_until(lambda:window._queue_preparation is None)
        saved=window.services.journal.load(entry['metadata']['job_id'])
        self.assertEqual(saved['config']['folder'],destination['folder'])

    def test_recycle_restore_keeps_payload_until_destination_and_checkpoint_succeed(self):
        window=self.window(services=True)
        original=self.root/'prior.jpg';original.write_bytes(b'preserved partial payload')
        entry=window.recycle_bin.add(title='Album',profile_url=PROFILE,
            post_url=PROFILE+'/post/1',payload_path=original)
        payload=Path(entry.payload_path)
        with patch('download_settings.fetch_artist_name',side_effect=OSError('offline')),patch('static_download_plan.ManifestFetchWorker.run',lambda worker:worker.result.emit([{**item,'assets':[]} for item in worker.entries])):
            self.assertTrue(window.restore_recycle_entry(entry.id))
            self.wait_until(lambda:not window.busy and window._destination_worker is None and window._manifest_worker is None)
        self.assertIsNotNone(window.recycle_bin.get(entry.id))
        self.assertTrue(payload.exists())
        self.assertFalse(window.services.journal.list_recoverable())
        window.scanner=type('Scanned',(),{'profile':PROFILE,'artist_name':'Recovered Artist'})()
        with patch.object(window,'_queue_insert_batch',return_value=None),patch('static_download_plan.ManifestFetchWorker.run',lambda worker:worker.result.emit([{**item,'assets':[]} for item in worker.entries])):
            self.assertTrue(window.restore_recycle_entry(entry.id))
            self.wait_until(lambda:bool(window._queue_scan_entries) and window._queue_preparation is None)
        self.assertIsNone(window.recycle_bin.get(entry.id))
        self.assertTrue(window.services.journal.list_recoverable())

    def test_profile_name_and_schedule_option_are_preserved(self):
        html='<a class="user-header__profile"><span itemprop="name">画师・ねこ</span></a>'
        self.assertEqual(artist_name_from_html(html),'画师・ねこ')
        config=normalize_snapshot(dict(profile=PROFILE,folder=str(self.root),artist_folder=True))
        self.assertTrue(config['artist_folder'])
        with self.assertRaises(ValueError):normalize_snapshot(dict(profile=PROFILE,folder=str(self.root),artist_folder='yes'))
