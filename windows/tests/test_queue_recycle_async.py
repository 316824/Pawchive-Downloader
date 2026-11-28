"""Bulk cancellation durability never performs SQLite/disk work on the UI thread."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
from download_scheduler import DownloadScheduler
import download_pawchive_images as desktop


class SlowBin:
    def __init__(self, delay=.003, gate=None):
        self.delay = delay
        self.gate = gate
        self.entries = {}
        self.threads = []
        self.removed = []
        self.closed = False
        self.entered = threading.Event()

    def add(self, **values):
        self.threads.append(threading.get_ident())
        self.entered.set()
        if self.gate is not None and not self.gate.wait(5):
            raise TimeoutError('Offline persistence gate was not released')
        time.sleep(self.delay)
        entry = SimpleNamespace(id=str(len(self.threads)), **values)
        self.entries[entry.id] = entry
        return entry

    def remove(self, identifier):
        self.removed.append(identifier)
        return self.entries.pop(identifier, None)

    def close(self):
        self.closed = True


class Journal:
    def __init__(self, fail=False):
        self.fail = fail
        self.saved = []
        self.threads = []

    def load(self, job_id):
        self.threads.append(threading.get_ident())
        return dict(id=job_id, status='paused', config=dict(static_numbering=True,
            by_title=False, static_plan=[dict(name='1.jpg', url='fixed-url')]), files=[])

    def save(self, job, dirty_records=None):
        self.threads.append(threading.get_ident())
        if self.fail:
            raise OSError('Offline durable journal failure')
        self.saved.append(copy.deepcopy(job))


class QueueRecycleAsyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pawchive_async_recycle_')
        self.root = Path(self.temporary.name)
        self.gate = threading.Event()
        self.network = patch('requests.sessions.Session.request',
            side_effect=AssertionError('No live HTTP in recycle regressions'))
        self.network.start()
        self.window = desktop.App(QSettings(str(self.root / 'settings.ini'), QSettings.Format.IniFormat))
        self.window.path.setText(str(self.root))
        self.window.show()
        self.scheduler = DownloadScheduler(lambda task: None, self.window)
        self.window.queue_scheduler = self.scheduler
        self.scheduler.task_recycled.connect(self.window._queue_task_recycled)
        self.scheduler.queue_changed.connect(self.window._queue_view)
        # This suite isolates persistence; finalization/reward side effects are
        # covered by the real SQLite queue/recycle integration suite.
        self.idle = patch.object(self.window, '_queue_worker_idle')
        self.idle.start()
        self.bin = SlowBin()
        self.journal = Journal()
        self.window.recycle_bin = self.bin
        self.window.services = SimpleNamespace(journal=self.journal,
            request_close=lambda: True, finish_exit=lambda: None)
        self.qt.processEvents()

    def tearDown(self):
        self.gate.set()
        self.wait_until(lambda: not self.window._has_queue_recycle_work())
        self.window.services = None
        if isValid(self.window):
            self.window.busy = False
            self.window.close()
            self.window.deleteLater()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.idle.stop()
        self.network.stop()
        self.temporary.cleanup()

    def wait_until(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(.001)
        self.assertTrue(predicate(), 'Timed out waiting for serialized recycle commits')

    def add(self, number):
        # The live task has a deliberately heavy plan. Its worker request must
        # contain no reference to this or to any widget/task/thread object.
        metadata = dict(job_id='job-' + str(number), _counted_completed_files=1,
            _worker_config=dict(profile='https://pawchive.pw/fanbox/user/1',
                folder=str(self.root), static_plan=[dict(url='x' * 4096)] * 200))
        task = self.scheduler.add_task(dict(id='task-' + str(number), title='Album ' + str(number),
            url='https://pawchive.pw/fanbox/user/1/post/' + str(number),
            file_count=1, formats=['.jpg'], metadata=metadata))
        self.window._queue_job_ids[task.id] = metadata['job_id']
        self.window._queue_scan_entries.append(dict(url=task.url, assets=metadata['_worker_config']['static_plan']))
        self.window._queue_selected_ids.append(task.id)
        self.window._queue_rewarded_ids.add(task.id)
        self.window._queue_completed_files += 1
        return task

    def test_three_hundred_cancellations_coalesce_and_leave_event_loop_responsive(self):
        tasks = [self.add(number) for number in range(300)]
        beats = []
        timer = QTimer(self.window)
        timer.setInterval(3)
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start()
        started = time.monotonic()
        self.assertEqual(self.scheduler.cancel_many([task.id for task in tasks]), 300)
        self.assertLess(time.monotonic() - started, .4)
        self.assertFalse(self.bin.entries, 'Persistence must be deferred off the cancel stack')
        self.assertEqual(len(self.window._queue_recycle_pending), 300)
        self.assertLess(len(json.dumps(list(self.window._queue_recycle_pending.values()))), 300 * 1024)
        self.qt.processEvents()
        worker = self.window._queue_recycle_worker
        self.assertIsNotNone(worker)
        self.wait_until(lambda: not self.window._has_queue_recycle_work())
        timer.stop()
        self.assertGreater(len(beats), 30)
        self.assertLess(max(b - a for a, b in zip(beats, beats[1:])), .4)
        self.assertEqual(len(self.bin.entries), 300)
        self.assertEqual(len(self.journal.saved), 300)
        self.assertTrue(all(job['status'] == 'recycled' for job in self.journal.saved))
        self.assertTrue(all(job['config']['static_plan'][0]['name'] == '1.jpg' for job in self.journal.saved))
        self.assertTrue(all(identifier != threading.get_ident() for identifier in self.bin.threads + self.journal.threads))
        self.assertEqual(len(set(self.bin.threads)), 1, 'All coalesced commits use one serial worker')
        self.assertEqual(self.scheduler.tasks, ())
        self.assertTrue(all(not task.metadata for task in tasks))
        self.assertEqual(self.window._queue_scan_entries, [])
        self.assertEqual(self.window._queue_selected_ids, [])
        self.assertEqual(self.window._queue_rewarded_ids, set())
        self.assertEqual(self.window._queue_completed_files, 0)

    def test_pending_writes_protect_workspace_and_new_downloads_until_ack(self):
        self.bin.gate = self.gate
        task = self.add(1)
        self.scheduler.cancel(task.id)
        self.wait_until(self.bin.entered.is_set)
        self.assertIs(self.scheduler.task(task.id), task)
        self.assertFalse(self.window._can_reset_workspace())
        self.assertFalse(self.window._batch_artist_work_settled())
        self.assertFalse(self.window._retire_finished_queue())
        self.assertFalse(self.window._start_queue([dict(url=task.url)], 'profile'))
        with patch.object(self.window, '_dispose_finished_scan') as scan:
            self.window.download()
        scan.assert_not_called()
        self.gate.set()
        self.wait_until(lambda: not self.window._has_queue_recycle_work())
        self.assertIsNone(self.scheduler.task(task.id))
        self.assertTrue(self.window._can_reset_workspace())

    def test_journal_failure_rolls_back_bin_and_allows_cancel_retry(self):
        self.journal.fail = True
        task = self.add(1)
        self.scheduler.cancel(task.id)
        self.wait_until(lambda: not self.window._has_queue_recycle_work())
        self.assertEqual(self.bin.entries, {})
        self.assertEqual(self.bin.removed, ['1'])
        self.assertEqual(task.state, 'cancelled')
        self.assertTrue(task.metadata)
        self.assertIs(self.scheduler.task(task.id), task)
        self.assertNotIn(task.id, self.window._queue_recycle_ids)
        self.assertIn('Offline durable journal failure', self.window.log.toPlainText())
        self.journal.fail = False
        self.scheduler.cancel(task.id)
        self.wait_until(lambda: not self.window._has_queue_recycle_work())
        self.assertIsNone(self.scheduler.task(task.id))
        self.assertEqual(len(self.bin.entries), 1)
        self.assertEqual(len(self.journal.saved), 1)

    def test_close_waits_for_accepted_commit_before_closing_storage(self):
        self.bin.gate = self.gate
        task = self.add(1)
        self.scheduler.cancel(task.id)
        self.wait_until(self.bin.entered.is_set)
        self.assertFalse(self.window.close())
        self.assertTrue(self.window._close_when_worker_stops)
        self.assertFalse(self.bin.closed)
        self.assertTrue(self.window.isVisible())
        self.gate.set()
        self.wait_until(lambda: not self.window.isVisible())
        self.assertFalse(self.window._has_queue_recycle_work())
        self.assertEqual(len(self.journal.saved), 1)
        self.assertEqual(len(self.bin.entries), 1)
        self.assertTrue(self.bin.closed)


if __name__ == '__main__':
    unittest.main()
