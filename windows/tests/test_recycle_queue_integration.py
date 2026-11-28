"""Real App cancellation/recycle ownership, isolated SQLite, disk and offline HTTP.

The scheduler, preparation thread, journal, recycle bin and UI slots are the
production objects. Only network responses and retry clocks are substituted.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from download_scheduler import DownloadScheduler
from queue_operations import QueuePreparationWorker
from static_download_plan import assign_plans
from test_artist_workflow_soak import OfflineSite, PNG, WorkflowHarness, drain
import download_pawchive_images as desktop


class RecycleQueueIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_recycle_integration_')
        self.root = Path(self.temp.name)
        self.site = OfflineSite()
        self.profile = self.site.add(955001, 3, 2)
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Real network forbidden'))
        self.network_mock = self.network.start()
        self.session = patch('requests.Session', side_effect=self.site.session)
        self.session.start()
        original_wait = desktop.Worker.wait_interruptibly
        self.backoff = patch.object(desktop.Worker, 'wait_interruptibly',
                                   lambda worker, seconds: original_wait(worker, min(seconds, .001)))
        self.backoff.start()
        self.h = WorkflowHarness(self.root, self.app, self.site)
        self.w = self.h.window
        self.w.show()
        self.folder = self.root/'downloads'/'静态画师'
        self.folder.mkdir()
        self.config = dict(folder=str(self.folder), profile=self.profile, order='newest',
            by_title=False, rename_order=False, queue_task=True, queue_rename_order=True,
            content_mode='images', record_artist=False, batch_id='recycle-integration-batch')
        self.preparations = []

    def tearDown(self):
        gate = self.site.pause_gate
        if gate:
            gate[1].set()
        try:
            self.h.close()
            for worker in self.preparations:
                if worker.isRunning():
                    worker.requestInterruption()
                    self.assertTrue(worker.wait(5000))
                worker.deleteLater()
            drain(self.app)
        finally:
            self.backoff.stop()
            self.session.stop()
            self.network.stop()
            self.temp.cleanup()

    def until(self, condition, label='recycle ownership', timeout=20):
        self.h.pump_until(condition, timeout=timeout, label=label)

    def entries(self, albums=2, images=2):
        return [dict(id=f'task-{album}', url=self.profile+'/post/'+str(album),
            title='图集 '+str(album), date=f'2026-11-{album+1:02d}T12:00:00Z', formats=['.png'],
            assets=[dict(url=self.site.file_url('955001', album, image), name=f'original-{image}.png',
                         direct=False, index=image) for image in range(images)]) for album in range(albums)]

    def prepare(self, entries, config=None):
        worker = QueuePreparationWorker(self.h.journal, entries, config or self.config)
        self.preparations.append(worker)
        results, failures = [], []
        worker.result.connect(results.append)
        worker.failed.connect(failures.append)
        worker.start()
        self.until(lambda:not worker.isRunning(), 'native queue preparation')
        self.assertFalse(failures, failures)
        self.assertTrue(results, 'Preparation exited without result')
        return results[0]

    def bind(self, entries):
        prepared = self.prepare(assign_plans(entries, self.config))
        scheduler = DownloadScheduler(self.w._queue_worker_factory, self.w)
        scheduler.AUTO_RETRY_LIMIT = 0
        self.w.queue_scheduler = scheduler
        self.w._queue_profile = self.profile
        self.w._active_download_folder = str(self.folder)
        self.w.path.setText(str(self.folder))
        self.w._queue_scan_entries = list(prepared)
        self.w._queue_pending_entries = []
        self.w._queue_selected_ids = [entry['id'] for entry in prepared]
        self.w._queue_rewarded_ids = set(self.w._queue_selected_ids)
        tasks = scheduler.add_tasks(prepared)
        self.w._queue_job_ids = {task.id:task.metadata['job_id'] for task in tasks}
        for signal, slot in ((scheduler.task_updated, self.w._queue_task_updated),
                             (scheduler.task_finished, self.w._queue_task_finished),
                             (scheduler.task_recycled, self.w._queue_task_recycled),
                             (scheduler.queue_changed, self.w._queue_view),
                             (scheduler.became_idle, self.w._queue_worker_idle)):
            signal.connect(slot)
        return prepared, tasks

    def recycled(self, scheduler, task_id):
        self.until(lambda:scheduler.task(task_id) is None, 'durable recycle acknowledgement')
        self.until(lambda:not getattr(self.w, '_has_queue_recycle_work', lambda:False)(),
                   'recycle persistence batch cleanup')
        self.until(lambda:not self.w._workspace_has_live_threads(), 'recycle native worker cleanup')
        entries = self.w.recycle_bin.list_entries()
        matches = [entry for entry in entries if entry.metadata.get('task_id') == task_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_fresh_workspace_can_refresh_empty_recycle_bin_before_first_download(self):
        self.w.refresh_recycle_bin()
        self.assertFalse(self.w.recycle_bin.list_entries())

    def test_refresh_retained_heavy_checkpoint_reads_only_ids_without_manifest_or_path_stats(self):
        _, tasks = self.bind(self.entries(1, 300))
        task = tasks[0]
        job_id = task.metadata['job_id']
        self.w.queue_scheduler.cancel(task.id)
        entry = self.recycled(self.w.queue_scheduler, task.id)
        with (patch.object(self.w.recycle_bin, 'list_entries', return_value=[entry]),
              patch.object(self.h.journal, 'load', side_effect=AssertionError('Retained manifest must not load')),
              patch.object(self.h.journal, '_validate', side_effect=AssertionError('Retained manifest must not validate')),
              patch.object(Path, 'lstat', side_effect=AssertionError('Retained paths must not stat'))):
            self.assertEqual(self.h.journal.list_recycled_ids(), [job_id])
            self.w.refresh_recycle_bin()
        self.assertEqual(self.h.journal.load(job_id)['status'], 'recycled')

    def test_cancel_commits_before_ack_and_releases_scheduler_and_root_manifests(self):
        prepared, tasks = self.bind(self.entries())
        scheduler = self.w.queue_scheduler
        task = tasks[0]
        job_id = task.metadata['job_id']
        observed = []
        add = self.w.recycle_bin.add
        def inspect_add(**kwargs):
            observed.append((scheduler.task(task.id) is task,
                             bool(task.metadata.get('_worker_config')),
                             self.h.journal.load(job_id)['status']))
            return add(**kwargs)
        with patch.object(self.w.recycle_bin, 'add', side_effect=inspect_add):
            self.assertTrue(scheduler.cancel(task.id))
            entry = self.recycled(scheduler, task.id)
        self.assertEqual(observed, [(True, True, 'paused')])
        self.assertEqual(entry.metadata['job_id'], job_id)
        self.assertEqual(self.h.journal.load(job_id)['status'], 'recycled')
        self.assertNotIn(job_id, {job['id'] for job in self.h.journal.list_recoverable()})
        self.assertEqual(task.metadata, {})
        self.assertNotIn(task.id, self.w._queue_job_ids)
        self.assertNotIn(task.id, self.w._queue_selected_ids)
        self.assertNotIn(task.id, self.w._queue_rewarded_ids)
        self.assertNotIn(task.url, {entry['url'] for entry in self.w._queue_scan_entries})
        self.assertIs(scheduler.task(tasks[1].id), tasks[1])
        self.assertTrue(tasks[1].metadata)

    def test_sqlite_add_failure_keeps_manifest_and_can_retry_cancellation(self):
        prepared, tasks = self.bind(self.entries(1))
        task = tasks[0]
        scheduler = self.w.queue_scheduler
        job_id = task.metadata['job_id']
        with patch.object(self.w.recycle_bin, 'add', side_effect=sqlite3.OperationalError('integration-add-failure')):
            self.assertTrue(scheduler.cancel(task.id))
            self.until(lambda:'integration-add-failure' in self.w.log.toPlainText(), 'recycle error delivery')
            self.until(lambda:not getattr(self.w, '_has_queue_recycle_work', lambda:False)(),
                       'failed recycle persistence batch cleanup')
            self.until(lambda:not self.w._workspace_has_live_threads(), 'failed recycle worker cleanup')
        self.assertIs(scheduler.task(task.id), task)
        self.assertEqual(task.state, 'cancelled')
        self.assertEqual(task.metadata['job_id'], job_id)
        self.assertIn(task.id, self.w._queue_job_ids)
        self.assertFalse(self.w.recycle_bin.list_entries())
        self.assertEqual(self.h.journal.load(job_id)['status'], 'paused')
        self.assertTrue(scheduler.cancel(task.id))
        self.recycled(scheduler, task.id)

    def test_real_bulk_delete_controls_recycle_only_selected_tasks_and_reset_ui_mode(self):
        _, tasks = self.bind(self.entries(24, 1))
        scheduler = self.w.queue_scheduler
        self.w._queue_view()
        self.w._flush_queue_view()
        self.until(lambda:len(self.w.download_queue._rows) == len(tasks), 'real queue card construction')
        self.w.queue_cancel_selected_button.click()
        self.assertTrue(self.w.download_queue.bulk_mode)
        selected = tasks[::2]
        for task in selected:
            self.w.download_queue._rows[task.id].selection_check.click()
        self.assertEqual(set(self.w._queue_selected_ids), {task.id for task in selected})
        self.w.queue_delete_button.click()
        self.assertFalse(self.w.download_queue.bulk_mode)
        self.assertTrue(self.w.queue_delete_button.isHidden())
        self.until(lambda:len(scheduler.tasks) == len(tasks)-len(selected), 'bulk durable recycle acknowledgements')
        self.until(lambda:not getattr(self.w, '_has_queue_recycle_work', lambda:False)(), 'bulk recycle batch cleanup')
        self.w._flush_queue_view()
        self.until(lambda:len(self.w.download_queue._rows) == len(scheduler.tasks), 'removed queue rows released')
        self.assertEqual({task.id for task in scheduler.tasks}, {task.id for task in tasks[1::2]})
        self.assertEqual(len(self.w.recycle_bin.list_entries()), len(selected))
        self.assertFalse(self.w._queue_selected_ids)
        self.assertTrue(all(task.metadata == {} for task in selected))
        self.assertTrue(all(task.metadata for task in tasks[1::2]))

    def test_journal_failure_rolls_back_bin_record_without_losing_original_task(self):
        _, tasks = self.bind(self.entries(1))
        task = tasks[0]
        job_id = task.metadata['job_id']
        with patch.object(self.h.journal, 'save', side_effect=sqlite3.OperationalError('integration-journal-failure')):
            self.assertTrue(self.w.queue_scheduler.cancel(task.id))
            self.until(lambda:'integration-journal-failure' in self.w.log.toPlainText(), 'journal failure delivery')
            self.until(lambda:not getattr(self.w, '_has_queue_recycle_work', lambda:False)(),
                       'failed journal persistence batch cleanup')
            self.until(lambda:not self.w._workspace_has_live_threads(), 'failed journal worker cleanup')
        self.assertIs(self.w.queue_scheduler.task(task.id), task)
        self.assertTrue(task.metadata.get('_worker_config'))
        self.assertFalse(self.w.recycle_bin.list_entries())
        self.assertEqual(self.h.journal.load(job_id)['status'], 'paused')
        self.assertTrue(self.w.queue_scheduler.cancel(task.id))
        self.recycled(self.w.queue_scheduler, task.id)

    def test_completed_and_existing_user_files_stay_in_place_after_cancel_and_delete(self):
        existing = self.folder/'1.png'
        existing.write_bytes(b'user-owned-original')
        unrelated = self.folder/'private.psd'
        unrelated.write_bytes(b'unrelated-user-document')
        _, tasks = self.bind(self.entries(1))
        task = tasks[0]
        job_id = task.metadata['job_id']
        job = self.h.journal.load(job_id)
        final = Path(job['files'][1]['target'])
        final.write_bytes(PNG)
        part = Path(job['files'][1]['part'])
        part.write_bytes(PNG[:24])
        job['status'] = 'completed'
        self.h.journal.save(job)
        task.state = 'completed'
        task.completed_files = 2
        task.metadata['_counted_completed_files'] = 2
        before = {path:path.read_bytes() for path in (existing, final, unrelated)}
        self.assertTrue(self.w.queue_scheduler.cancel(task.id))
        entry = self.recycled(self.w.queue_scheduler, task.id)
        self.assertIsNone(entry.payload_path)
        self.assertTrue(part.exists())
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertTrue(self.w.remove_recycle_entry(entry.id))
        self.assertFalse(part.exists())
        self.assertIsNone(self.w.recycle_bin.get(entry.id))
        with self.assertRaises(KeyError):
            self.h.journal.load(job_id)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_batch_restore_preserves_job_identity_and_static_names_despite_changed_ui(self):
        prepared, tasks = self.bind(self.entries())
        original = {task.metadata['job_id']:copy.deepcopy(self.h.journal.load(task.metadata['job_id'])['config'])
                    for task in tasks}
        self.w.queue_scheduler.cancel_many([task.id for task in tasks])
        entries = [self.recycled(self.w.queue_scheduler, task.id) for task in tasks]
        self.w.title_check.setChecked(True)
        self.w.rename_check.setChecked(False)
        self.w.path.setText(str(self.root/'other-destination'))
        with patch.object(self.w, '_start_queue', return_value=False):
            self.assertFalse(self.w.restore_recycle_entries([entry.id for entry in entries]))
        self.assertEqual(len(self.w.recycle_bin.list_entries()), 2, 'Failed queue start must keep every recovery record')
        with patch.object(self.w, '_start_queue', return_value=True) as start:
            self.assertTrue(self.w.restore_recycle_entries([entry.id for entry in entries]))
        start.assert_called_once()
        restored, profile = start.call_args.args
        self.assertEqual(profile, self.profile)
        self.assertEqual(start.call_args.kwargs['_destination'], str(self.folder))
        self.assertEqual({item['metadata']['job_id'] for item in restored}, set(original))
        self.assertEqual({item['metadata']['recycled_from'] for item in restored}, {entry.id for entry in entries})
        self.assertEqual(len(self.w.recycle_bin.list_entries()), 2, 'No record is taken before queue acceptance')
        prepared = self.prepare(restored, {**self.config, 'by_title':True, 'queue_rename_order':False})
        for item in prepared:
            job = self.h.journal.load(item['metadata']['job_id'])
            previous = original[job['id']]
            self.assertEqual(job['config']['static_plan'], previous['static_plan'])
            self.assertEqual(job['config']['folder'], previous['folder'])
            self.assertFalse(job['config']['by_title'])
            self.assertTrue(job['config']['static_numbering'])
            self.assertEqual(job['status'], 'recycled')
        self.assertFalse(self.h.journal.list_recoverable())
        self.assertTrue(self.w.restore_recycle_entries([entry.id for entry in entries]))
        self.until(self.h.settled, 'two real restored jobs share the original static naming plan')
        self.assertFalse(self.w.recycle_bin.list_entries())
        self.assertEqual({path.name for path in self.folder.glob('*.png')}, {'1.png', '2.png', '3.png', '4.png'})
        for job_id, previous in original.items():
            job = self.h.journal.load(job_id)
            self.assertEqual(job['status'], 'completed')
            self.assertEqual(job['config']['static_plan'], previous['static_plan'])

    def test_expiry_refresh_keeps_restored_jobs_while_they_wait_for_queue_insertion(self):
        _, tasks = self.bind(self.entries(1))
        task = tasks[0]
        job_id = task.metadata['job_id']
        self.w.queue_scheduler.cancel(task.id)
        entry = self.recycled(self.w.queue_scheduler, task.id)
        restored = self.w._recycle_task_from_entry(entry)
        self.w._queue_pending_entries = [restored]
        self.w.recycle_bin.take(entry.id)
        self.w.refresh_recycle_bin()
        self.assertEqual(self.h.journal.load(job_id)['status'], 'recycled')
        self.assertFalse(self.h.journal.list_recoverable())
        self.w._queue_pending_entries = []
        self.w.refresh_recycle_bin()
        with self.assertRaises(KeyError):
            self.h.journal.load(job_id)

    def test_actual_app_acceptance_persists_resume_before_queue_insertion_or_transfer(self):
        """Exit between accepting a restore and starting workers cannot orphan it."""
        from download_journal import DownloadJournal
        _, tasks = self.bind(self.entries(4, 1))
        original = {task.metadata['job_id']:copy.deepcopy(self.h.journal.load(task.metadata['job_id'])['config'])
                    for task in tasks}
        self.w.queue_scheduler.cancel_many([task.id for task in tasks])
        recycled = [self.recycled(self.w.queue_scheduler, task.id) for task in tasks]
        try:
            # Model the exact crash boundary: preparation and GUI acceptance
            # finish, but the first 16-item insertion callback has not run.
            with patch.object(self.w, '_queue_insert_batch') as insert:
                self.assertTrue(self.w.restore_recycle_entries([entry.id for entry in recycled]))
                self.until(lambda:self.w._queue_preparation is None and
                           len(self.w._queue_pending_entries) == len(tasks),
                           'accepted restore before any scheduler insertion')
                insert.assert_called_once()
                self.assertFalse(self.w.queue_scheduler.tasks)
                self.assertFalse(self.w.recycle_bin.list_entries())
                self.w.refresh_recycle_bin()
                reopened = DownloadJournal(self.h.journal.data_dir)
                try:
                    recovered = reopened.list_recoverable()
                    self.assertEqual({job['id'] for job in recovered}, set(original))
                    for job in recovered:
                        self.assertEqual(job['status'], 'paused')
                        self.assertEqual(job['config']['static_plan'], original[job['id']]['static_plan'])
                        self.assertEqual(job['config']['folder'], str(self.folder))
                finally:
                    reopened.close()
        finally:
            # The insertion callback was intentionally replaced; let ordinary
            # fixture shutdown close without manufacturing a download worker.
            self.w._queue_pending_entries = []
            self.w.set_busy_state(False)

    def test_expiration_discards_owned_partial_but_never_final_or_unrelated_file(self):
        _, tasks = self.bind(self.entries(1))
        task = tasks[0]
        job_id = task.metadata['job_id']
        job = self.h.journal.load(job_id)
        final = Path(job['files'][0]['target'])
        final.write_bytes(PNG)
        part = Path(job['files'][1]['part'])
        part.write_bytes(PNG[:24])
        unrelated = self.folder/'another-download.part'
        unrelated.write_bytes(b'not-owned-by-this-journal')
        self.w.queue_scheduler.cancel(task.id)
        entry = self.recycled(self.w.queue_scheduler, task.id)
        with patch.object(self.w.recycle_bin, '_clock', return_value=entry.expires_at+1):
            self.w.refresh_recycle_bin()
            self.assertFalse(self.w.recycle_bin.list_entries())
        self.assertFalse(part.exists())
        self.assertEqual(final.read_bytes(), PNG)
        self.assertEqual(unrelated.read_bytes(), b'not-owned-by-this-journal')
        with self.assertRaises(KeyError):
            self.h.journal.load(job_id)

    def test_active_cancel_then_real_offline_restore_resumes_same_numbered_files(self):
        self.w.artist_folder_check.setChecked(False)
        self.w.title_check.setChecked(False)
        self.w.rename_check.setChecked(True)
        self.w.path.setText(str(self.folder))
        self.w.profile.setText(self.profile)
        self.site.pause_url = self.site.file_url('955001', 0, 0)
        self.site.pause_gate = (threading.Event(), threading.Event())
        self.w.download()
        self.until(self.site.pause_gate[0].is_set, 'offline original paused after first bytes')
        scheduler = self.w.queue_scheduler
        task = next(task for task in scheduler.tasks if task.url.endswith('/post/0'))
        job_id = task.metadata['job_id']
        original_plan = copy.deepcopy(self.h.journal.load(job_id)['config']['static_plan'])
        self.assertTrue(scheduler.cancel(task.id))
        self.assertIsNotNone(scheduler.task(task.id), 'Native writer must stop before acknowledgement')
        self.site.pause_gate[1].set()
        entry = self.recycled(scheduler, task.id)
        self.until(self.h.settled, 'other albums finish before restore')
        job = self.h.journal.load(job_id)
        part = Path(job['files'][0]['part'])
        self.assertEqual(part.read_bytes(), PNG[:24])
        self.assertEqual(job['status'], 'recycled')
        self.assertTrue(self.w.restore_recycle_entries([entry.id]))
        self.until(self.h.settled, 'real restore download and finalization')
        restored = self.h.journal.load(job_id)
        self.assertEqual(restored['status'], 'completed')
        self.assertEqual(restored['config']['static_plan'], original_plan)
        self.assertIsNone(self.w.recycle_bin.get(entry.id))
        self.assertFalse(part.exists())
        self.assertGreater(self.site.events['valid_ranges'], 0)
        for asset in original_plan:
            target = Path(restored['config']['folder'])/asset['relative_dir']/asset['save_name']
            self.assertEqual(target.read_bytes(), PNG)
        self.assertEqual(self.network_mock.call_count, 0)


if __name__ == '__main__':
    unittest.main()
