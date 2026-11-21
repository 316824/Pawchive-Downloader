"""Offline invariants across immutable plans, durable queues and Qt teardown."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from app_state import AppState
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal
from download_scheduler import DownloadScheduler, DownloadTask
import download_pawchive_images as desktop
from queue_operations import QueuePreparationWorker
from static_download_plan import assign_plans
from subscriptions import SubscriptionStore, parse_site_datetime


PROFILE = 'https://pawchive.pw/fanbox/user/core-integration'


class StartFailureWorker(QObject):
    finished_ok = Signal(int)
    failed = Signal(str)
    finished = Signal()

    def start(self):
        raise RuntimeError('native worker could not start')

    def isRunning(self):
        return False


class DormantScanner(QObject):
    result = Signal(object)
    failed = Signal(str)
    counts = Signal(int, int)

    def __init__(self, *args, **kwargs):
        super().__init__()

    def start(self):
        pass

    def isRunning(self):
        return False

    def requestInterruption(self):
        pass


class CoreRefactorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_core_integration_')
        self.root = Path(self.temp.name)
        self.folder = self.root / 'downloads'
        self.journal = DownloadJournal(self.root / 'journal')
        self.window = None
        self.schedulers = []
        self.resources = []
        self.config = dict(folder=str(self.folder), profile=PROFILE, by_title=True,
                           order='newest', queue_task=True, queue_rename_order=True,
                           rename_order=False, start_number=1, content_mode='all')

    def tearDown(self):
        for scheduler in self.schedulers:
            scheduler.stop()
            for worker in list(scheduler._workers.values()):
                if isinstance(worker, QThread):
                    self.assertTrue(worker.wait(3000), 'native test worker did not stop')
            self.qt.processEvents()
            scheduler.deleteLater()
        if self.window is not None:
            self.window.busy = False
            self.window.close()
            self.window.deleteLater()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for resource in self.resources:
            resource.close()
        self.journal.close()
        self.temp.cleanup()

    def wait_until(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(.001)
        self.assertTrue(condition(), 'Qt integration condition did not settle')

    def entries(self, titles):
        return [dict(url=PROFILE + '/post/' + str(index), title=title,
                     date=f'2026-11-{index + 1:02d}',
                     assets=[dict(url='https://file.pawchive.pw/data/' + str(index) + '.png',
                                  name='image.png', direct=False, index=0)])
                for index, title in enumerate(titles)]

    def scheduler(self, factory):
        scheduler = DownloadScheduler(factory)
        scheduler.AUTO_RETRY_LIMIT = 0
        self.schedulers.append(scheduler)
        return scheduler

    def test_real_suffixed_and_sanitized_duplicate_titles_get_separate_albums(self):
        titles = ['Album', 'ALBUM', 'Album (2)', 'a/b', 'a\\b', 'a_b (2)']
        plans = assign_plans(self.entries(titles), self.config)
        directories = [entry['static_plan'][0]['relative_dir'] for entry in plans]
        self.assertEqual(len({value.casefold() for value in directories}), len(titles))
        self.assertEqual([entry['static_plan'][0]['save_name'] for entry in plans], ['1.png'] * len(titles))

    def test_global_numbers_follow_numeric_dates_and_image_indexes_across_formats(self):
        entries = self.entries(['New', 'Old'])
        entries[0]['date'] = '2026-10-19'
        entries[1]['date'] = '2026-10-9'
        entries[1]['assets'] = [
            dict(url='https://file.pawchive.pw/data/old.webp', name='old.webp', direct=False, index=8),
            dict(url='https://file.pawchive.pw/data/old.jpg', name='old.jpg', direct=False, index=2),
            dict(url='https://file.pawchive.pw/data/old.zip', name='old.zip', direct=True, index=4)]
        config = {**self.config, 'by_title': False}
        plans = assign_plans(entries, config)
        self.assertEqual(plans[0]['static_plan'][0]['save_name'], '3.png')
        self.assertEqual([asset['save_name'] for asset in plans[1]['static_plan']], ['1.jpg', 'old.zip', '2.webp'])
        self.assertEqual([asset['number'] for asset in plans[1]['static_plan']], [1, None, 2])
        self.assertFalse(self.folder.exists(), 'reservation must not create filesystem payload')

    def test_forged_manifest_cannot_escape_root_or_open_windows_streams_and_devices(self):
        asset = assign_plans(self.entries(['Safe']), self.config)[0]['static_plan'][0]
        for changes in [
                dict(relative_dir='../outside'), dict(relative_dir=str(self.root / 'outside')),
                dict(save_name='../outside.png'), dict(save_name='existing.png:stream'),
                dict(save_name='NUL.png'), dict(save_name='trailing.png.'),
                dict(relative_dir='existing:stream'), dict(relative_dir='CON'),
                dict(url='https://outside.invalid/data/image.png')]:
            with self.subTest(changes=changes):
                plan = {**asset, **changes}
                with self.assertRaises(ValueError):
                    self.journal.create({**self.config, 'static_plan': [plan]})
        duplicate = {**asset, 'save_name': asset['save_name'].upper()}
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.journal.create({**self.config, 'static_plan': [asset, duplicate]})
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.journal.list_recoverable(), [])

    def test_recovery_keeps_saved_static_targets_and_numbering_choices(self):
        entry = assign_plans(self.entries(['Saved']), self.config)[0]
        results = []
        first = QueuePreparationWorker(self.journal, [entry], self.config)
        first.result.connect(results.append)
        first.run()
        job_id = results[0][0]['metadata']['job_id']
        saved = copy.deepcopy(self.journal.load(job_id)['config']['static_plan'])
        recovered = {**entry, 'metadata': {'job_id': job_id}}
        results.clear()
        second = QueuePreparationWorker(self.journal, [recovered],
                                        {**self.config, 'by_title': False, 'queue_rename_order': False})
        second.result.connect(results.append)
        second.run()
        config = results[0][0]['metadata']['_worker_config']
        self.assertEqual(config['static_plan'], saved)
        self.assertTrue(config['static_numbering'])
        self.assertTrue(config['by_title'])
        self.assertFalse(config['queue_rename_order'])

    def test_resume_ui_restores_static_numbering_before_network_scan_completes(self):
        self.window = desktop.App(QSettings(str(self.root / 'settings.ini'), QSettings.Format.IniFormat))
        config = {**self.config, 'selected_post_urls': [PROFILE + '/post/0'],
                  'static_numbering': True, 'queue_rename_order': False}
        with patch.object(desktop, 'ProfileScanWorker', DormantScanner), \
                patch.object(self.window, '_start_queue', return_value=True):
            self.assertTrue(self.window.resume_recoverable_jobs([dict(id='fixture', config=config)]))
            self.assertTrue(self.window.rename_check.isChecked())
            self.window._resume_scan_worker.result.emit([])

    def test_success_acknowledges_only_actual_completed_post_url(self):
        state = AppState(self.root / 'state')
        self.resources.append(state)
        store = SubscriptionStore(state)
        store.add(PROFILE, baseline_ts=parse_site_datetime('2026-10-01'))
        old_url, new_url = PROFILE + '/post/old', PROFILE + '/post/new'
        posts = [dict(url=old_url, title='Old', date='2026-11-01'),
                 dict(url=new_url, title='New', date='2026-11-02')]
        store.set_baseline(PROFILE, parse_site_datetime('2026-11-02'), posts=posts)
        owner = SimpleNamespace(_queue_completed_files=0, subscription_store=store, _queue_profile=PROFILE,
                                refresh_subscription_badge=lambda: None, _queue_view=lambda: None,
                                _update_queue_aggregate_progress=lambda: None)
        task = DownloadTask(id='new', title='New', url=new_url, state='completed', completed_files=1,
                            metadata={'_worker_config': dict(profile=PROFILE, static_plan=[posts[1]])})
        desktop.App._queue_task_finished(owner, task)
        self.assertEqual([post['url'] for post in store.get(PROFILE)['pending_posts']], [old_url])
        self.assertEqual(store.get(PROFILE)['downloaded_post_urls'], [new_url])
        task = DownloadTask(id='old', title='Old', url=old_url, state='failed', completed_files=1,
                            metadata={'_worker_config': dict(profile=PROFILE, static_plan=[posts[0]])})
        desktop.App._queue_task_finished(owner, task)
        self.assertEqual([post['url'] for post in store.get(PROFILE)['pending_posts']], [old_url])

    def test_new_scheduled_work_enters_scan_and_static_queue_route(self):
        window = Mock(busy=False)
        window.select_posts_check.isChecked.return_value = True
        runtime = SimpleNamespace(window=window, _old_selection=None)
        DesktopRuntime.run_snapshot(runtime, self.config)
        window.download.assert_called_once_with()
        window.launch_worker.assert_not_called()
        self.assertIsNone(runtime.resume_job_id)

    def test_large_queue_factory_errors_yield_to_gui_without_recursive_crash(self):
        def fail(task):
            raise ValueError('invalid checkpoint')
        scheduler = self.scheduler(fail)
        scheduler.add_tasks(dict(title=str(index), url=str(index), formats=['.png']) for index in range(600))
        idle = []
        scheduler.became_idle.connect(lambda: idle.append(True))
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        scheduler.start()
        self.assertTrue(any(task.state == 'queued' for task in scheduler.tasks))
        self.wait_until(lambda: bool(idle))
        self.assertTrue(heartbeat)
        self.assertTrue(all(task.state == 'failed' and task.error == 'invalid checkpoint' for task in scheduler.tasks))
        self.assertFalse(scheduler._workers)
        self.assertEqual(len(idle), 1)

    def test_start_failure_without_finished_signal_releases_native_pool_slot(self):
        scheduler = self.scheduler(lambda task: StartFailureWorker())
        scheduler.add_tasks(dict(title=str(index), url=str(index), formats=['.png']) for index in range(9))
        scheduler.start()
        self.wait_until(lambda: all(task.state == 'failed' for task in scheduler.tasks))
        self.assertFalse(scheduler._workers)
        self.assertFalse(scheduler._finished_signals)

    def test_hundreds_of_real_qthreads_stop_before_slot_release_and_deferred_deletion(self):
        lock = threading.Lock()
        running = [0, 0]
        class NativeWorker(QThread):
            progress = Signal(str, object, object)
            finished_ok = Signal(int)
            failed = Signal(str)

            def run(self):
                with lock:
                    running[0] += 1
                    running[1] = max(running)
                try:
                    self.progress.emit('image.png', 2 ** 34, 2 ** 35)
                    self.finished_ok.emit(1)
                    time.sleep(.002)  # Terminal result precedes native exit.
                finally:
                    with lock:
                        running[0] -= 1
        scheduler = self.scheduler(lambda task: NativeWorker())
        scheduler.add_tasks(dict(title=str(index), url=str(index), formats=['.png']) for index in range(200))
        updated_threads = []
        scheduler.task_updated.connect(lambda task: updated_threads.append(QThread.currentThread()))
        scheduler.start()
        self.wait_until(lambda: not scheduler._workers and all(task.state == 'completed' for task in scheduler.tasks), 10)
        self.assertEqual(running[0], 0)
        self.assertLessEqual(running[1], scheduler.MAX_IMAGE_WORKERS)
        self.assertTrue(all(thread == scheduler.thread() for thread in updated_threads))
        self.assertFalse(scheduler._relays)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
